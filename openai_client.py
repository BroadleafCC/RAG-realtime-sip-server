"""
OpenAI Realtime API (GA) への WebSocket 接続ラッパー。

既存プロジェクト（Twilio SIPトランク + Realtime "Call API"）とは異なり、
call_id を使わないプレーンな Realtime WebSocket 接続。turn_detection は
常に null（サーバー側VADを完全に無効化）にし、発話終了判定は自前の
vad.py が行う。

音声フォーマットの注意: GA版APIでは beta版のフラットな文字列
`input_audio_format: "g711_ulaw"` は拒否される。ネスト形式の
`{"type": "audio/pcmu"}` を仮説として採用しているが、これは実機未検証。
Stage 1のngrokスモークテストで session.updated イベントの
session.audio.input.format エコーバックを必ずログで確認すること
（本ファイルの _log_session_echo がそれを行う）。
"""
import asyncio
import json
import logging

import websockets

import config

logger = logging.getLogger("openai_client")


class OpenAiSendTimeout(Exception):
    """OpenAIソケットへの送信がタイムアウトした（バックプレッシャー等で
    相手が読んでいない疑い）。呼び出し側はOpenAI故障として縮退運転に入ること。"""

REALTIME_WS_URL = "wss://api.openai.com/v1/realtime?model={model}"

# 高頻度で意味のないイベントはタイプ名のみログする（指示書の要件）
HIGH_FREQUENCY_EVENT_TYPES = {
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
    "response.output_audio.delta",
    "response.text.delta",
    "input_audio_buffer.append",
}


def build_session_update(instructions: str) -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "noise_reduction": {"type": "far_field"},
                    "turn_detection": None,
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": "coral",
                },
            },
        },
    }


# 挨拶は事前生成済みのμ-lawクリップとして即時再生する（call_session.py参照）。
# ここではモデルに「言わせる」のではなく、既に言い終えたものとして会話履歴に
# 注入するためのテキストを定義する（改善指示書「挨拶即時再生」2章）。
GREETING_TEXT = "お待たせしました。ご用件を伺います。"


def build_greeting_said_item() -> dict:
    """挨拶を会話履歴にassistant発言として注入するconversation.item.create。
    response.createは送らない（次のresponseはユーザー発話のcommit後に発生する）。

    contentタイプはGA版APIでは"output_text"が正しい（"text"を指定すると
    `invalid_request_error: Invalid value: 'text'. Value must be 'output_text'`
    でreject される。実機ログで確認済み）。
    """
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": GREETING_TEXT}],
        },
    }


def build_text_item(text: str) -> list[dict]:
    """既存main.pyの timeout_item/goodbye_item/retry_item パターンを一般化した
    ヘルパー。system扱いのuserメッセージを1件挿入し、応答を生成させる。"""
    return [
        {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
        {"type": "response.create"},
    ]


class OpenAiRealtimeSocket:
    def __init__(self, model: str | None = None):
        self.model = model or config.OPENAI_REALTIME_MODEL
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def connect(self):
        url = REALTIME_WS_URL.format(model=self.model)
        # ping_interval/ping_timeout はkeepalive（半死接続の自動検知）。
        # 送信側は _send のタイムアウトが先に効くが、受信側（recv_events）が
        # 無言のまま固まるケースはこのkeepaliveが例外化して救う。無効化
        # （ping_interval=None）にしないこと。
        self._ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            ping_interval=10,
            ping_timeout=5,
        )
        return self

    async def close(self):
        if self._ws is not None:
            await self._ws.close()

    async def _send(self, payload: dict):
        """全送信の共通経路。ハング（相手が読まない）を速い失敗に変換する。

        2026-08-05障害：OpenAIサーバーが音声を消費しなくなると、WebSocket送信は
        フロー制御（TCPバックプレッシャー）で永久ブロックする。例外は出ないため
        try/exceptでは捕らえられず、awaitしたタスクごとハングする。タイムアウトで
        例外化することで、既存の例外ハンドリング（縮退運転・ウォッチドッグの
        try/except）がそのまま機能するようになる。

        送信メソッドは append_audio / commit / response_create / send_text_turn /
        response_cancel / truncate_item / inject_greeting_said /
        send_session_update と散らばっているため、個別に wait_for を書くのではなく
        ここ1箇所で一括して適用する（個別対応は必ず漏れる。実際に
        _say_and_wait_for_goodbye の送信が漏れて安全網ごとハングした）。
        """
        try:
            await asyncio.wait_for(
                self._ws.send(json.dumps(payload)),
                timeout=config.OPENAI_SEND_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            raise OpenAiSendTimeout(
                f"OpenAI送信が{config.OPENAI_SEND_TIMEOUT_SEC}秒以内に完了しませんでした"
                f"（サーバーが読んでいない疑い） type={payload.get('type')}"
            ) from None

    async def send_session_update(self, instructions: str):
        await self._send(build_session_update(instructions))

    async def inject_greeting_said(self):
        await self._send(build_greeting_said_item())

    async def send_text_turn(self, text: str):
        for item in build_text_item(text):
            await self._send(item)

    async def append_audio(self, payload_b64: str):
        await self._send({"type": "input_audio_buffer.append", "audio": payload_b64})

    async def commit(self):
        await self._send({"type": "input_audio_buffer.commit"})

    async def response_create(self):
        await self._send({"type": "response.create"})

    async def response_cancel(self, response_id: str | None = None):
        payload = {"type": "response.cancel"}
        if response_id:
            payload["response_id"] = response_id
        await self._send(payload)

    async def truncate_item(self, item_id: str, audio_end_ms: int, content_index: int = 0):
        """バージイン時、モデルの会話履歴を実際に聞こえたところまで切り詰める
        （改善指示書「バージイン実装」修正1-4）。response.cancel/clearだけでは
        モデルは「どこまで聞こえたか」を知らないため、これと併用する。"""
        await self._send({
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": content_index,
            "audio_end_ms": audio_end_ms,
        })

    async def recv_events(self):
        async for message in self._ws:
            event = json.loads(message)
            event_type = event.get("type", "")
            if event_type in HIGH_FREQUENCY_EVENT_TYPES:
                logger.debug("[OA EVENT] %s", event_type)
            else:
                logger.info("[OA EVENT] %s", event_type)
            yield event


def log_session_echo(event: dict):
    """session.created / session.updated 受信時に呼ぶ。GA版フォーマット仮説
    (audio/pcmu, turn_detection=null) が実機でそのまま通っているかを確認する。"""
    session = event.get("session", {}) or {}
    audio = session.get("audio", {}) or {}
    input_audio = audio.get("input", {}) or {}
    logger.info(
        "[SESSION ECHO] type=%s format=%s turn_detection=%s",
        event.get("type"),
        input_audio.get("format"),
        input_audio.get("turn_detection"),
    )
