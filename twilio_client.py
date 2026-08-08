"""
Twilio REST API 操作 + Media Streams の outbound フレーム構築。

既存プロジェクトはOpenAI Realtime "Call API" 経由でhangupしていたが
（`POST /v1/realtime/calls/{call_id}/hangup`）、本プロジェクトのトランスポート
には call_id が存在しないため、Twilio REST API 経由の通話終了に置き換える。
"""
import base64
import json
import logging
import math
import threading
import time

from twilio.rest import Client as TwilioClient

import config

logger = logging.getLogger("twilio_client")

_client: TwilioClient | None = None

# Twilio Media Streamsが前提とする1フレームのサイズ（20ms分のμ-law 8kHz = 160byte）。
# OpenAIから届くdeltaは可変長のため、この単位に詰め直してから送信する
# （改善指示書2-a: deltaとフレームの境界がズレることによる音声頭切れ対策）。
FRAME_BYTES = 160
SILENCE_BYTE = b"\xff"  # μ-lawの無音相当バイト


class FrameAligner:
    """OpenAIから届く可変長の音声deltaを、20ms(160byte)単位のフレームに詰め直す。

    deltaの境界とフレームの境界がズレて音声の一部が失われる問題（改善指示書2-a）
    を避けるため、端数は内部バッファに保持して次のdeltaへ跨がせる。応答（response）
    ごとに新しいインスタンスを使うこと（呼び出し側でresponse.created時に作り直す）。
    """

    def __init__(self, frame_bytes: int = FRAME_BYTES):
        self.frame_bytes = frame_bytes
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        """dataを取り込み、確定した160byteフレームのリストを返す。端数は次回に持ち越す。"""
        self._buf.extend(data)
        frames = []
        while len(self._buf) >= self.frame_bytes:
            frames.append(bytes(self._buf[:self.frame_bytes]))
            del self._buf[:self.frame_bytes]
        return frames

    def flush(self) -> tuple[bytes, int] | None:
        """応答終端で残った端数を無音パディングして返す。

        戻り値は (パディング済みフレーム, 実データのバイト数)。端数がなければNone。
        実データ長を分けて返すのは、呼び出し側がbytes_out統計にパディング分を
        含めずに集計できるようにするため（改善指示書2-c）。
        """
        if not self._buf:
            return None
        real_len = len(self._buf)
        frame = bytes(self._buf) + SILENCE_BYTE * (self.frame_bytes - real_len)
        self._buf.clear()
        return frame, real_len


def _get_client() -> TwilioClient:
    global _client
    if _client is None:
        _client = TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


def start_recording(call_sid: str, retries: int = 3, retry_delay_sec: float = 1.0):
    """/voice webhook受信時に呼ぶ。Media Streams通話は自動録音されないため
    明示的に開始する（Whisperパイプラインが録音URLを前提とするため必須）。

    初回は呼び出し元のスレッドで即時実行し（挨拶より前に録音を開始できている
    実績を維持する）、失敗時のみdaemonスレッドでリトライする。21220
    (Call is not in-progress) はwebhook直後の状態レースで起こるため、1秒後の
    再試行でほぼ解消する。リトライはwebhook応答・メディア処理を一切
    ブロックしない。"""
    def _attempt(n: int) -> bool:
        try:
            _get_client().calls(call_sid).recordings.create()
            logger.info("[RECORDING] started call_sid=%s (attempt %d)", call_sid, n)
            return True
        except Exception as e:
            logger.warning("[RECORDING] start failed call_sid=%s attempt=%d: %s", call_sid, n, e)
            return False

    if _attempt(1):
        return

    def _retry_loop():
        for n in range(2, retries + 1):
            time.sleep(retry_delay_sec)
            if _attempt(n):
                return
        logger.error("[RECORDING] 全リトライ失敗 call_sid=%s（録音なしで続行）", call_sid)

    threading.Thread(target=_retry_loop, daemon=True).start()


def hangup_call(call_sid: str):
    try:
        _get_client().calls(call_sid).update(status="completed")
        logger.info("[HANGUP] call_sid=%s", call_sid)
    except Exception as e:
        logger.warning("[HANGUP] failed call_sid=%s: %s", call_sid, e)


async def send_media(ws, stream_sid: str, payload_b64: str):
    await ws.send_text(json.dumps({
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload_b64},
    }))


async def send_media_bytes(ws, stream_sid: str, raw: bytes):
    """生のμ-lawバイト列をbase64化してTwilioへ送る（send_mediaの生バイト版）。"""
    await send_media(ws, stream_sid, base64.b64encode(raw).decode("ascii"))


async def send_lead_silence(ws, stream_sid: str, duration_ms: int):
    """応答音声冒頭の頭切れ対策（改善指示書2-b）。

    各応答の最初の実音声フレームを送る前に、指定ms分のμ-law無音フレームを
    送っておく。出力ストリームが立ち上がる瞬間の頭切れを、無音部分に吸収させる
    のが狙い。この無音分はmark待ち時間・再生時間の計算（bytes_out集計）には
    含めない（呼び出し側で別カウントする）。
    """
    if duration_ms <= 0:
        return
    frame_ms = 20
    # 切り上げる（切り捨て/四捨五入だと指定msを下回る可能性があり、
    # 頭切れ対策という目的上「最低でも指定ms」を保証したいため）。
    n_frames = max(1, math.ceil(duration_ms / frame_ms))
    silence = SILENCE_BYTE * FRAME_BYTES
    for _ in range(n_frames):
        await send_media_bytes(ws, stream_sid, silence)


async def send_clear(ws, stream_sid: str):
    await ws.send_text(json.dumps({
        "event": "clear",
        "streamSid": stream_sid,
    }))


async def send_mark(ws, stream_sid: str, name: str):
    """指定した名前のmarkイベントを送る。Twilioは実際にこの位置まで再生し
    終えた時点で、同じ名前のmarkイベントを送り返してくる。応答の最後の
    音声フレーム送信直後に呼ぶことで、電話口での再生完了を正確に検知できる
    （response.doneはサーバー側の生成完了でしかなく、電話口の再生完了より
    大きく先行するため、これでは代用できない）。"""
    await ws.send_text(json.dumps({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": name},
    }))
