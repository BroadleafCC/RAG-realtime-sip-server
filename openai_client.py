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
import json
import logging

import websockets

import config
from agent_search import SEARCH_FAQ_TOOL

logger = logging.getLogger("openai_client")

REALTIME_WS_URL = "wss://api.openai.com/v1/realtime?model={model}"

# 高頻度で意味のないイベントはタイプ名のみログする（指示書の要件）
HIGH_FREQUENCY_EVENT_TYPES = {
    "response.audio_transcript.delta",
    "response.output_audio_transcript.delta",
    "response.output_audio.delta",
    "response.text.delta",
    "input_audio_buffer.append",
}


# FAX番号案内用のfunction calling定義（call_session.py参照）。数字の読み上げ
# 速度をRealtime APIの音声出力では制御できないため、モデルには喋らせず
# 事前録音クリップの再生をトリガーさせるだけの引数なし関数にしている。
FAX_NUMBER_TOOL = {
    "type": "function",
    "name": "play_fax_number",
    "description": (
        "お客様が印字ズレ対応のFAX送付案内に同意した場合、またはFAX番号の"
        "聞き直しを求められた場合に呼び出す。呼び出すとFAX番号のみが事前録音"
        "音声で自動再生される（「専任の担当者から〜」等の案内文は含まれない）。"
        "この関数を呼ぶときはFAX番号を自分の声で話してはいけない。何も話さず"
        "この関数だけを呼び出すこと。番号の案内文（「専任の担当者からご連絡"
        "いたします」等）はこの関数の実行後、あなた自身の声で続けて話すこと。"
        "聞き直しを求められた場合は、案内文を繰り返さずこの関数だけを"
        "再度呼び出せばよい。"
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def build_session_update(instructions: str) -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "tools": [FAX_NUMBER_TOOL, SEARCH_FAQ_TOOL],
            "tool_choice": "auto",
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


def build_function_call_output_item(call_id: str, output: str) -> dict:
    """function_call完了後、実行結果をモデルに返すconversation.item.create。
    これを送るだけでは応答は生成されないため、続けてresponse.createが必要
    （呼び出し側のsend_function_call_outputは行わない。呼び出し順を明示するため
    呼び出し元＝call_session.pyで個別にresponse_create()を呼ぶ）。"""
    return {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": call_id,
            "output": output,
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
        await self._ws.send(json.dumps(payload))

    async def send_session_update(self, instructions: str):
        await self._send(build_session_update(instructions))

    async def inject_greeting_said(self):
        await self._send(build_greeting_said_item())

    async def send_text_turn(self, text: str):
        for item in build_text_item(text):
            await self._send(item)

    async def send_function_call_output(self, call_id: str, output: str):
        await self._send(build_function_call_output_item(call_id, output))

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
