import asyncio
import logging
import socket
import sys
import time
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import PlainTextResponse
from itsdangerous import BadSignature, SignatureExpired
from starlette.websockets import WebSocketDisconnect

import call_logger
import call_session
import config
from salesforce_case import link_serializer

# OpenAI Realtime APIのWebSocketエンドポイント（openai_client.REALTIME_WS_URL
# と同じホスト）。リージョン移設の前後でこの数値を比較すれば経路改善の効果が
# 分かる（改善指示書「レイテンシ回収」修正3-b）。
OPENAI_RTT_HOST = "api.openai.com"
OPENAI_RTT_PORT = 443

# Windows上のローカル開発では既定のstdout/stderrエンコーディングがUTF-8で
# ないことがあり、日本語ログ（通話記録・エラーメッセージ）が文字化けする。
# Railway(Linux)では通常UTF-8だが、ローカルngrok検証でも文字化けなく
# 確認できるよう明示的に固定する。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


def _measure_rtt_once(host: str, port: int, timeout: float = 5.0) -> float | None:
    """TCP接続確立にかかった時間(ms)を1回計測する。HTTPリクエストは投げない
    軽量な経路調査（改善指示書「レイテンシ回収」修正3-b）。失敗時はNone。"""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return (time.monotonic() - start) * 1000
    except OSError as e:
        logger.warning("[RTT] %s:%d への接続に失敗しました: %s", host, port, e)
        return None


def _measure_rtt_avg(host: str, port: int, attempts: int = 3) -> None:
    samples = [t for t in (_measure_rtt_once(host, port) for _ in range(attempts)) if t is not None]
    if not samples:
        logger.warning("[RTT] %s 全%d回の接続に失敗したため計測できませんでした", host, attempts)
        return
    logger.info(
        "[RTT] openai_endpoint avg=%.0fms (%d回計測, samples=%s)",
        sum(samples) / len(samples), len(samples),
        ", ".join(f"{s:.0f}ms" for s in samples),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    call_logger.init_db()
    call_logger.cleanup_old_logs()
    call_session.preload_static_clips()
    logger.info(
        "[CONFIG] AUDIO_PRIMING_MS=%d PRIMING_IDLE_THRESHOLD_MS=%d",
        config.AUDIO_PRIMING_MS, config.PRIMING_IDLE_THRESHOLD_MS,
    )
    await asyncio.to_thread(_measure_rtt_avg, OPENAI_RTT_HOST, OPENAI_RTT_PORT)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/voice")
async def voice(request: Request):
    """Twilioの着信Voice webhook。Media Streamsへ接続させるTwiMLを返す。

    OpenAI WebSocket接続の確立はここで前倒しして裏で開始する（Media Stream
    の`start`受信を待たない）。TwiML返却はブロックせず即座に行う（改善指示書
    「挨拶即時再生」3・4章）。
    """
    host = request.headers.get("host", config.RAILWAY_PUBLIC_DOMAIN)
    form = await request.form()
    caller = form.get("From", "")
    call_session.prewarm_openai_connection(form.get("CallSid", ""))
    stream_url = f"wss://{host}/media-stream"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}">
      <Parameter name="caller" value="{caller}" />
    </Stream>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        await call_session.run(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("[MEDIA STREAM] unexpected error: %s", e)


@app.get("/recording/{token}")
async def recording_proxy(token: str):
    try:
        recording_sid = link_serializer.loads(token, max_age=config.RECORDING_LINK_MAX_AGE_SEC)
    except SignatureExpired:
        return PlainTextResponse("このリンクの有効期限（発行から7日間）が切れています。", status_code=410)
    except BadSignature:
        return PlainTextResponse("不正なリクエストです。", status_code=403)
    twilio_url = (
        f"https://api.twilio.com/2010-04-01/Accounts"
        f"/{config.TWILIO_ACCOUNT_SID}/Recordings/{recording_sid}.mp3"
    )
    resp = requests.get(
        twilio_url,
        auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN),
        stream=True,
        timeout=30,
    )
    return Response(
        content=resp.content,
        media_type=resp.headers.get("Content-Type", "audio/mpeg"),
        status_code=resp.status_code,
    )
