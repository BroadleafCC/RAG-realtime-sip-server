import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import PlainTextResponse
from starlette.websockets import WebSocketDisconnect

import call_logger
import call_session
import config
import loop_heartbeat

# Windows上のローカル開発では既定のstdout/stderrエンコーディングがUTF-8で
# ないことがあり、日本語ログ（通話記録・エラーメッセージ）が文字化けする。
# Railway(Linux)では通常UTF-8だが、ローカルngrok検証でも文字化けなく
# 確認できるよう明示的に固定する。
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    call_logger.init_db()
    call_logger.cleanup_old_logs()
    call_session.preload_static_clips()

    # イベントループのブロック監視（最終安全網）。ループ上のウォッチドッグは
    # ループが固まれば一緒に死ぬため、監視だけはループ外の別スレッドに置く。
    heartbeat_task = None
    if config.LOOP_HEARTBEAT_ENABLED:
        loop_heartbeat.start_monitor(config.LOOP_HEARTBEAT_STALL_SEC)
        heartbeat_task = asyncio.create_task(loop_heartbeat.heartbeat_loop())

    yield

    # シャットダウン時はハートビートを止める。止めないと、終了処理中に
    # ビートが途絶えたことをブロックと誤認してしまう。
    if heartbeat_task is not None:
        loop_heartbeat.stop()
        heartbeat_task.cancel()


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

    # 切断検知ON時のみステータスコールバックを仕込む（修正指示書パートA）。
    # <Connect>にはstatusCallback属性が無く、TwiML返却時点ではCallが既に進行中で
    # REST APIでの後付け設定はタイミング的に不安定なため、<Stream>のstatusCallbackを
    # 使う。これで拾えるのはstream系イベント（stream-started/stopped/error）が中心で
    # CallDurationは通常含まれないが、切断分類の本体はサーバー内部状態だけで成立する
    # 設計にしてある（call_session.classify_disconnect参照）。ここはあくまで裏取り用。
    status_attr = ""
    if config.DISCONNECT_TRACKING_ENABLED:
        status_cb_url = f"https://{host}/call-status"
        status_attr = (
            f' statusCallback="{status_cb_url}"'
            f' statusCallbackMethod="POST"'
        )

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}"{status_attr}>
      <Parameter name="caller" value="{caller}" />
    </Stream>
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


@app.post("/call-status")
async def call_status(request: Request):
    """Twilioのステータスコールバック受信（修正指示書パートA）。切断分類のための
    通話メタ情報（Duration/Status/SipResponseCode/StreamEvent）を記録する。

    注意：Twilioは「発信側が切った」ことを直接は返さない。ここで受けるのは
    あくまで終了時のメタ情報で、切断主体は call_session 側の内部状態
    （OA接続到達・VAD発話有無・mark進捗）と合成して『推定』する。

    分類ログの出力はこのコールバックを待たない（Media Streamの終了と前後する
    ため）。ここが分類ログより後に届いた場合は duration が `?` になるだけで、
    分類そのものは成立する。
    """
    if not config.DISCONNECT_TRACKING_ENABLED:
        return Response(status_code=204)
    form = await request.form()
    call_sid = form.get("CallSid", "")
    payload = {
        "CallStatus": form.get("CallStatus", ""),
        "CallDuration": form.get("CallDuration", ""),
        "SipResponseCode": form.get("SipResponseCode", ""),
        "StreamEvent": form.get("StreamEvent", ""),
    }
    call_session.record_call_status(call_sid, payload)
    return Response(status_code=204)


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    try:
        await call_session.run(websocket)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("[MEDIA STREAM] unexpected error: %s", e)


@app.get("/recording/{recording_sid}")
async def recording_proxy(recording_sid: str, token: str = ""):
    if token != config.RECORDING_ACCESS_TOKEN:
        return PlainTextResponse("Unauthorized", status_code=401)
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
