"""
Twilio REST API 操作 + Media Streams の outbound フレーム構築。

既存プロジェクトはOpenAI Realtime "Call API" 経由でhangupしていたが
（`POST /v1/realtime/calls/{call_id}/hangup`）、本プロジェクトのトランスポート
には call_id が存在しないため、Twilio REST API 経由の通話終了に置き換える。
"""
import json
import logging

from twilio.rest import Client as TwilioClient

import config

logger = logging.getLogger("twilio_client")

_client: TwilioClient | None = None


def _get_client() -> TwilioClient:
    global _client
    if _client is None:
        _client = TwilioClient(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    return _client


def start_recording(call_sid: str):
    """start イベント受信時に呼ぶ。Media Streams通話は自動録音されないため
    明示的に開始する（既存main.py/Whisperパイプラインが録音URLを前提とする
    ため必須）。"""
    try:
        _get_client().calls(call_sid).recordings.create()
        logger.info("[RECORDING] started call_sid=%s", call_sid)
    except Exception as e:
        logger.warning("[RECORDING] failed to start call_sid=%s: %s", call_sid, e)


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
