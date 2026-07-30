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
