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


# タイミング起因で「まだ録音できない」ことを示すTwilioエラーコード。
# 21220 = Requested resource is not eligible for recording。通話が
# in-progress へ完全に遷移する前に recordings.create() を叩くと返る。
# 待てば成功する見込みがあるのでリトライ対象（修正指示書パートB）。
_RETRYABLE_RECORDING_ERROR_CODES = {21220}


def start_recording(call_sid: str) -> str | None:
    """通話の録音を開始し、成功したら Recording SID を返す（失敗時は None）。

    /voice webhook 受信直後に呼ばれるため、通話が録音可能な状態になる前に
    到達することがある（21220）。録音開始が数百ms遅れて挨拶クリップの頭が
    録れないことより、録音そのものが存在しないことの方が実害が大きいので、
    短いバックオフでリトライして確実に開始することを優先する。

    【重要】リトライ待機に time.sleep を使うため、必ずワーカースレッド
    （asyncio.to_thread 経由）から呼ぶこと。Media Streams のイベントループ上で
    直接呼ぶと音声の送受信が止まる。
    """
    max_attempts = config.RECORDING_MAX_RETRIES if config.RECORDING_RETRY_ENABLED else 1
    last_err = None
    for attempt in range(max_attempts):
        try:
            rec = _get_client().calls(call_sid).recordings.create()
            logger.info(
                "[RECORDING] started call_sid=%s recording_sid=%s attempt=%d",
                call_sid, rec.sid, attempt + 1,
            )
            return rec.sid
        except Exception as e:
            last_err = e
            code = getattr(e, "code", None)  # TwilioRestException.code
            if code in _RETRYABLE_RECORDING_ERROR_CODES and attempt < max_attempts - 1:
                backoff = config.RECORDING_RETRY_BACKOFF_MS * (attempt + 1) / 1000.0
                logger.info(
                    "[RECORDING] retry call_sid=%s attempt=%d code=%s backoff=%.1fs",
                    call_sid, attempt + 1, code, backoff,
                )
                time.sleep(backoff)
                continue
            # リトライ対象外のエラー、またはリトライを尽くした
            break
    logger.warning(
        "[RECORDING] failed to start call_sid=%s after %d attempts: %s",
        call_sid, max_attempts, last_err,
    )
    return None


def fetch_recording_for_call(call_sid: str):
    """当該通話に紐づく録音のみを返す（無ければ None）。

    時間窓（DateCreated>）＋直近N件で録音を探す実装は、並行通話時や録音開始
    失敗時に**他通話の録音を掴む**。実際にそれが起きて、別通話の文字起こしが
    ケースに入った事故があるため、録音の特定は Call SID 直引きに固定する
    （修正指示書パートB）。時間窓での探索を今後書かないこと。
    """
    if not call_sid:
        return None
    recordings = _get_client().calls(call_sid).recordings.list(limit=1)
    if not recordings:
        return None
    return recordings[0]


def fetch_recording_by_sid(recording_sid: str):
    """start_recording が返した Recording SID を直接引く（無ければ None）。"""
    if not recording_sid:
        return None
    return _get_client().recordings(recording_sid).fetch()


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
