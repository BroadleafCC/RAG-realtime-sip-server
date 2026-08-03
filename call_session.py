"""
1通話ぶんの状態を保持し、5つの独立タスクを束ねるオーケストレーター。

既存main.pyの websocket_task/silence_monitor の再設計版。最大の違いは
「無音タイムアウト・最大通話時間・応答ウォッチドッグ・音声中継×2」が
すべて独立した asyncio タスクとして動く点（watchdogs.pyのdocstring参照）。

どのタスクが終了理由であっても、Salesforceケース作成は必ず一度だけ
`finally` ブロックで保証される（応答ウォッチドッグの縮退運転経路だけは
例外的に自分自身でケースを作り `case_created=True` を立てるため、二重
作成を避けられる）。
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass

from starlette.websockets import WebSocketDisconnect

import call_logger
import config
import salesforce_case
import twilio_client
import watchdogs
from audio_convert import ulaw_to_pcm16
from openai_client import OpenAiRealtimeSocket, log_session_echo
from vad import TurnDetector, VadEvent, VadState
from vad_model import SileroVad

logger = logging.getLogger("call_session")

# 相槌（ENABLE_FILLER=true時）の直後にモデルが同じ相槌を言い直して二重発声する
# のを防ぐための追加ルール。プロンプトDBの内容に関わらず、フィラー機能が有効な
# 場合のみ instructions の末尾に付け足す（改善指示書1-b）。
FILLER_ANTI_DOUBLE_SPEECH_NOTICE = (
    "\n\n【システム注記】ユーザーの発話終了直後に短い相槌（「かしこまりました」）が"
    "自動再生されています。あなたの応答をこの相槌の繰り返しから始めないでください。"
    "相槌は言い終えたものとして扱い、続きの内容から話し始めてください。"
)

_filler_audio_cache: bytes | None = None


def _load_filler_audio() -> bytes:
    """フィラー音声（μ-law 8kHz生データ）をファイルから読み込み、プロセス内で
    キャッシュする。ファイルが無い/読めない場合は空バイト列を返し、呼び出し側は
    フィラー再生を静かにスキップする（通話を止めない）。"""
    global _filler_audio_cache
    if _filler_audio_cache is not None:
        return _filler_audio_cache
    try:
        with open(config.FILLER_AUDIO_PATH, "rb") as f:
            _filler_audio_cache = f.read()
        logger.info(
            "[FILLER] 音声ファイルを読み込みました path=%s bytes=%d",
            config.FILLER_AUDIO_PATH, len(_filler_audio_cache),
        )
    except OSError as e:
        logger.warning(
            "[FILLER] 音声ファイルを読み込めませんでした path=%s err=%s（フィラー再生をスキップします）",
            config.FILLER_AUDIO_PATH, e,
        )
        _filler_audio_cache = b""
    return _filler_audio_cache


if config.ENABLE_FILLER:
    _load_filler_audio()  # 起動時に一度読み込み、通話中の初回再生での遅延を避ける


@dataclass
class _AudioStats:
    """1応答ぶんの音声中継の診断カウンタ（改善指示書2-c）。response.created時に
    作り直し、電話口での再生完了（mark受信）時に一度だけログ出力する。"""
    resp_id: str | None = None
    deltas: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    first_delta_bytes: int | None = None
    first_frame_sent_at: float | None = None


class CallSession:
    def __init__(self):
        self.call_sid = ""
        self.stream_sid = ""
        self.caller_number = ""
        self.transcript_lines: list[str] = []

        self.greeting_done = False
        self.ai_is_speaking = False
        self.is_goodbye = False
        self.case_created = False
        self.goodbye_event = asyncio.Event()

        self.turn_detector = TurnDetector(
            threshold=config.VAD_THRESHOLD,
            speech_start_ms=config.VAD_SPEECH_START_MS,
            speech_end_ms=config.VAD_SPEECH_END_MS,
            barge_in_min_ms=config.BARGE_IN_MIN_MS,
        )
        self.silero = SileroVad()
        self.openai: OpenAiRealtimeSocket | None = None
        self._response_id: str | None = None

        # 応答音声のフレーム整形・頭切れ対策・診断ログ用の状態（改善指示書2章）。
        # response.created を受けるたびに作り直す（pump_openai_to_twilio参照）。
        self._frame_aligner = twilio_client.FrameAligner()
        self._lead_silence_sent = False
        self._audio_stats = _AudioStats()

        # Twilio markによる「電話口での実際の再生完了」トラッキング用。
        # response.doneはサーバー側の音声生成完了でしかなく、Realtime APIは
        # 音声を実時間より速く生成するため、電話口での再生完了は
        # response.doneより最大5秒以上遅れうる（実測）。そのズレを無音
        # タイマーに混入させないため、markの往復でしか ai_is_speaking を
        # Falseにしない。
        self._pending_mark_name: str | None = None
        self._mark_seq = 0

        # 無音タイマーの起点。「適格条件（VAD状態==IDLE かつ audio_playing==False）」
        # が不適格→適格に遷移した瞬間、またはmark受信/SPEECH_STARTED発生時（保険）に
        # のみ now へリセットされる。_refresh_silence_eligibility() が一元管理する。
        self.silence_anchor: float | None = None
        self._silence_eligible = False
        self.response_deadline: float | None = None
        self.response_watchdog_stage = 0


async def run(twilio_ws):
    session = CallSession()

    try:
        start_event = await _wait_for_start(twilio_ws)
    except Exception as e:
        logger.error("[CALL] start フレームを受信できませんでした: %s", e)
        return

    session.stream_sid = start_event.get("streamSid", "")
    start_block = start_event.get("start", {}) or {}
    session.call_sid = start_block.get("callSid", "")
    custom_params = start_block.get("customParameters", {}) or {}
    session.caller_number = custom_params.get("caller", "")

    call_logger.log_event(session.call_sid, session.caller_number, 'to_arrived', 'SUCCESS')
    twilio_client.start_recording(session.call_sid)

    try:
        session.openai = await OpenAiRealtimeSocket().connect()
        instructions = call_logger.get_prompt(call_logger.DEFAULT_INSTRUCTIONS)
        if config.ENABLE_FILLER:
            instructions += FILLER_ANTI_DOUBLE_SPEECH_NOTICE
        await session.openai.send_session_update(instructions)
        await session.openai.send_greeting()
        call_logger.log_event(session.call_sid, session.caller_number, 'ws_connected', 'SUCCESS')

        async with asyncio.TaskGroup() as tg:
            tg.create_task(pump_twilio_to_openai(session, twilio_ws))
            tg.create_task(pump_openai_to_twilio(session, twilio_ws))
            tg.create_task(watchdogs.silence_watchdog(session))
            tg.create_task(watchdogs.max_duration_watchdog(session))
            tg.create_task(watchdogs.response_watchdog(session))
    except* watchdogs.CallEnded:
        pass
    except* Exception as eg:
        for exc in eg.exceptions:
            logger.error("[CALL] 予期しないエラーで終了しました: %s", exc)
        call_logger.log_event(session.call_sid, session.caller_number, 'ws_connected', 'FAILURE', str(eg))
    finally:
        if session.openai is not None:
            try:
                await session.openai.close()
            except Exception:
                pass
        # どんな終了経路でも発信元番号を落とさずケースを作る、という
        # 指示書の最優先要件をここで構造的に保証する。応答ウォッチドッグの
        # 縮退運転経路だけは既に自分でケースを作っているのでスキップする。
        if not session.case_created:
            await asyncio.to_thread(
                salesforce_case.create_salesforce_case,
                session.transcript_lines, session.call_sid, session.caller_number,
                False,
            )


async def _wait_for_start(twilio_ws) -> dict:
    """Twilioは`connected`イベントを先に送ってくることがあるため、
    実際の`start`イベントが来るまで読み飛ばす。"""
    while True:
        raw = await twilio_ws.receive_text()
        data = json.loads(raw)
        if data.get("event") == "start":
            return data
        if data.get("event") != "connected":
            logger.warning("[CALL] start前に想定外のイベント: %s", data.get("event"))


async def pump_twilio_to_openai(session: CallSession, twilio_ws):
    while True:
        try:
            raw = await twilio_ws.receive_text()
        except WebSocketDisconnect:
            raise watchdogs.CallEnded("twilio_disconnected")

        data = json.loads(raw)
        event = data.get("event")

        if event == "media":
            payload_b64 = data["media"]["payload"]
            # VAD状態に関わらず常時OpenAIへ転送する（発話開始直後の音の
            # 頭切れを防ぐため。既存main.pyの設計思想を踏襲）
            await session.openai.append_audio(payload_b64)

            pcm16 = ulaw_to_pcm16(base64.b64decode(payload_b64))
            for prob, chunk_sec in session.silero.feed(pcm16):
                transition = session.turn_detector.update(
                    prob, chunk_sec * 1000, session.ai_is_speaking
                )
                if transition.event is not None:
                    logger.info(
                        "[VAD] event=%s state=%s prob=%.3f elapsed_ms=%.0f",
                        transition.event.name, transition.state.name, prob, transition.elapsed_ms,
                    )
                    await _handle_vad_event(session, twilio_ws, transition.event)

        elif event == "mark":
            mark_name = (data.get("mark") or {}).get("name", "")
            if mark_name and mark_name == session._pending_mark_name:
                # 電話口での本当の再生完了。ここが無音タイマーの起点になる
                # （もう1つの起点はVADのSPEECH_STARTED）。
                session._pending_mark_name = None
                session.ai_is_speaking = False
                _log_audio_stats(session, mark_name)
                _refresh_silence_eligibility(session, reason="mark")
                logger.info("[MARK] name=%s 応答完了", mark_name)
                if session.is_goodbye and mark_name.startswith("goodbye_"):
                    session.goodbye_event.set()
            else:
                # バージインでresponse.cancel済みのmarkが遅れて届いた等、
                # 既に無効化されたmark。二重処理しない。
                logger.info("[MARK] name=%s (無効化済みのため無視)", mark_name)

        elif event == "stop":
            raise watchdogs.CallEnded("twilio_stop")
        # 'connected'（再送されうる）や未知イベントは無視する


def _log_audio_stats(session: CallSession, mark_name: str) -> None:
    """応答1件ぶんの音声中継の診断ログ（改善指示書2-c）。電話口での再生完了
    （mark受信）時に一度だけ出力する。bytes_in != bytes_out は自サーバー内で
    音声が欠落したことを、playback_sec が expected_sec から大きくズレることは
    Twilio側での欠落・遅延を示す。"""
    stats = session._audio_stats
    if stats.deltas == 0:
        return  # このmarkに対応する応答でOpenAI音声を受信していない（相槌のみ等）
    expected_sec = stats.bytes_out / 8000.0
    if stats.first_frame_sent_at is not None:
        playback_sec = f"{time.monotonic() - stats.first_frame_sent_at:.3f}"
    else:
        playback_sec = "n/a"
    logger.info(
        "[AUDIO-STATS] resp_id=%s mark=%s deltas=%d bytes_in=%d bytes_out=%d "
        "first_delta_bytes=%s playback_sec=%s expected_sec=%.3f",
        stats.resp_id, mark_name, stats.deltas, stats.bytes_in, stats.bytes_out,
        stats.first_delta_bytes, playback_sec, expected_sec,
    )


def _refresh_silence_eligibility(session: CallSession, reason: str | None = None) -> None:
    """無音タイマーの起点(silence_anchor)を一元管理する。

    適格条件 eligible = (turn_detector.state == IDLE) and (ai_is_speaking == False)。
    reason が指定されたイベント（mark受信/SPEECH_STARTED）では適格性に関わらず
    無条件にリセットする（保険）。reason が None の場合は不適格→適格への遷移を
    検出したときにのみリセットする（これが無音タイマー本来の起点）。
    """
    eligible = (not session.ai_is_speaking) and (session.turn_detector.state == VadState.IDLE)
    became_eligible = eligible and not session._silence_eligible
    session._silence_eligible = eligible

    if reason is not None:
        session.silence_anchor = time.monotonic()
        logger.info("[SILENCE-TIMER] リセット (要因: %s)", reason)
    elif became_eligible:
        session.silence_anchor = time.monotonic()
        logger.info("[SILENCE-TIMER] 計測開始")


async def _handle_vad_event(session: CallSession, twilio_ws, event: VadEvent):
    if event == VadEvent.SPEECH_STARTED:
        _refresh_silence_eligibility(session, reason="speech")

    elif event == VadEvent.END_OF_SPEECH:
        await session.openai.commit()
        await session.openai.response_create()
        session.response_deadline = time.monotonic() + config.RESPONSE_WATCHDOG_FIRST_SEC
        session.response_watchdog_stage = 0
        if config.ENABLE_FILLER:
            # モデルの応答音声が生成されるまでの無音区間を埋める即時相槌
            # （改善指示書1-b）。AWAITING_RESPONSE中はVAD状態機械が確率で
            # 遷移しない（vad.py参照）ため、この再生はVAD/バージイン判定に
            # 一切影響しない。
            await _play_filler(session, twilio_ws)

    elif event == VadEvent.BARGE_IN:
        await session.openai.response_cancel(session._response_id)
        await twilio_client.send_clear(twilio_ws, session.stream_sid)
        # cancel/clear を送った時点でAIの発話は止める意思決定が済んでいる。
        # markの往復を待つとその間 ai_is_speaking=True のままローカル状態
        # 機械が進行を止め続けてしまうため、ここで即座に折り返す。
        session.ai_is_speaking = False
        # 破棄されたキューぶんのmarkがTwilioから遅れて返ってきても
        # 二重処理しないよう、待機中のmark名を無効化しておく。
        session._pending_mark_name = None
        # 中断された応答は正常なmark経路を通らないため、ここで診断ログを
        # 出しておく（改善指示書2-c）。
        stats = session._audio_stats
        if stats.deltas:
            logger.info(
                "[AUDIO-STATS] resp_id=%s barge_in=True deltas=%d bytes_in=%d bytes_out=%d",
                stats.resp_id, stats.deltas, stats.bytes_in, stats.bytes_out,
            )
        _refresh_silence_eligibility(session)


async def _play_filler(session: CallSession, twilio_ws) -> None:
    """事前録音の短い相槌音声をTwilioへ即座に送る（改善指示書1-b）。
    OpenAIの音声パス（_audio_stats/_frame_aligner）とは完全に独立させる
    （フィラーは診断対象の「OpenAI→Twilio中継」には含めない）。"""
    data = _load_filler_audio()
    if not data:
        return
    for i in range(0, len(data), twilio_client.FRAME_BYTES):
        chunk = data[i:i + twilio_client.FRAME_BYTES]
        if len(chunk) < twilio_client.FRAME_BYTES:
            chunk = chunk + twilio_client.SILENCE_BYTE * (twilio_client.FRAME_BYTES - len(chunk))
        await twilio_client.send_media_bytes(twilio_ws, session.stream_sid, chunk)
    logger.info("[FILLER] 相槌音声を再生しました bytes=%d", len(data))


async def pump_openai_to_twilio(session: CallSession, twilio_ws):
    async for event in session.openai.recv_events():
        event_type = event.get("type")

        if event_type in ("session.created", "session.updated"):
            log_session_echo(event)

        elif event_type == "response.created":
            response_obj = event.get("response") or {}
            session._response_id = (
                response_obj.get("id") if isinstance(response_obj, dict) else None
            ) or event.get("response_id") or event.get("id")
            # 応答ごとにフレーム整形バッファ・頭切れ対策フラグ・診断カウンタを
            # 作り直す（改善指示書2章）。
            session._frame_aligner = twilio_client.FrameAligner()
            session._lead_silence_sent = False
            session._audio_stats = _AudioStats(resp_id=session._response_id)

        elif event_type == "response.done":
            session.turn_detector.force_idle()
            session.response_deadline = None
            session.response_watchdog_stage = 0
            session.greeting_done = True
            # force_idleでVAD状態がIDLEに戻るが、audio_playing(ai_is_speaking)は
            # markの往復でしか False にならない。ここでは適格性を再評価するのみで、
            # まだ再生中なら計測は始まらない（_refresh_silence_eligibility内で判定）。
            _refresh_silence_eligibility(session)
            _log_response_usage(session, event)

        elif event_type == "output_audio_buffer.started":
            session.ai_is_speaking = True
            _refresh_silence_eligibility(session)

        elif event_type == "response.output_audio.delta":
            audio_b64 = event.get("delta", "")
            if audio_b64:
                raw = base64.b64decode(audio_b64)
                stats = session._audio_stats
                stats.deltas += 1
                stats.bytes_in += len(raw)
                if stats.first_delta_bytes is None:
                    stats.first_delta_bytes = len(raw)

                if not session._lead_silence_sent:
                    # この応答で最初の音声が来た瞬間、実フレームの前に無音を
                    # 挟んで頭切れを吸収する（改善指示書2-b）。
                    await twilio_client.send_lead_silence(
                        twilio_ws, session.stream_sid, config.RESPONSE_LEAD_SILENCE_MS
                    )
                    session._lead_silence_sent = True

                # deltaの境界とTwilioフレーム境界のズレによる欠落を防ぐため、
                # 20ms(160byte)単位に詰め直してから送信する（改善指示書2-a）。
                for frame in session._frame_aligner.push(raw):
                    if stats.first_frame_sent_at is None:
                        stats.first_frame_sent_at = time.monotonic()
                    stats.bytes_out += len(frame)
                    await twilio_client.send_media_bytes(twilio_ws, session.stream_sid, frame)

        elif event_type == "response.output_audio.done":
            # フレーム整形バッファに残った端数（160byte未満）を無音パディング
            # して送り切る。ここで捨てると応答末尾が欠落するため、必ずflushする
            # （改善指示書2-a）。パディング分はbytes_out統計に含めない。
            tail = session._frame_aligner.flush()
            if tail is not None:
                tail_frame, real_len = tail
                stats = session._audio_stats
                if stats.first_frame_sent_at is None:
                    stats.first_frame_sent_at = time.monotonic()
                stats.bytes_out += real_len
                await twilio_client.send_media_bytes(twilio_ws, session.stream_sid, tail_frame)

            # この応答の音声フレームはすべてTwilioへ送信済み。ただし
            # Twilioの電話口での再生はまだ完了していない可能性が高い
            # （Realtime APIは実時間より速く音声を生成するため）。
            # markを送り、実際の再生完了はTwilioからのmark折り返しで判定する
            # （pump_twilio_to_openaiのmarkハンドラ側）。
            session._mark_seq += 1
            prefix = "goodbye" if session.is_goodbye else "resp"
            mark_name = f"{prefix}_{session._mark_seq}"
            session._pending_mark_name = mark_name
            await twilio_client.send_mark(twilio_ws, session.stream_sid, mark_name)

        elif event_type == "response.output_audio_transcript.done":
            text = event.get("transcript", "")
            if text:
                session.transcript_lines.append(f"AI: {text}")

        elif event_type == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "")
            if text:
                session.transcript_lines.append(f"お客様: {text}")

        elif event_type == "conversation.item.done":
            item = event.get("item", {})
            if item.get("role") == "user":
                for c in item.get("content", []):
                    if c.get("type") == "input_audio":
                        text = c.get("transcript", "")
                        if text:
                            session.transcript_lines.append(f"お客様: {text}")

        elif event_type == "error":
            logger.error("[OA ERROR] %s", event.get("error"))

    # サーバー側がclose frameを送って正常終了した場合のフォールバック。
    # 応答ウォッチドッグが「無応答スタック」を検知する主経路であり、
    # これは「綺麗に切れた場合」への速い反応にすぎない。
    raise watchdogs.CallEnded("openai_disconnected")


def _log_response_usage(session: CallSession, event: dict):
    try:
        response_obj = event.get("response") or {}
        usage = response_obj.get("usage", {}) if isinstance(response_obj, dict) else {}
        if not usage or not usage.get("total_tokens"):
            return
        settings = call_logger.get_cost_settings()
        details_in = usage.get("input_token_details", {})
        details_out = usage.get("output_token_details", {})
        audio_in = details_in.get("audio_tokens", 0)
        audio_out = details_out.get("audio_tokens", 0)
        text_in = details_in.get("text_tokens", 0)
        text_out = details_out.get("text_tokens", 0)
        cost = (
            audio_in / 1_000_000 * float(settings.get('cost_openai_realtime_audio_in_per_1m', '100.0')) +
            audio_out / 1_000_000 * float(settings.get('cost_openai_realtime_audio_out_per_1m', '200.0')) +
            text_in / 1_000_000 * float(settings.get('cost_openai_realtime_text_in_per_1m', '5.0')) +
            text_out / 1_000_000 * float(settings.get('cost_openai_realtime_text_out_per_1m', '20.0'))
        )
        call_logger.log_usage(
            session.call_sid, 'openai_realtime', config.OPENAI_REALTIME_MODEL,
            input_tokens=text_in, output_tokens=text_out,
            audio_input_tokens=audio_in, audio_output_tokens=audio_out,
            cost_usd=cost,
        )
    except Exception as ue:
        logger.warning("[WARN] Realtime usage log error: %s", ue)
