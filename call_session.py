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
from openai_client import GREETING_TEXT, OpenAiRealtimeSocket, log_session_echo
from vad import TurnDetector, VadEvent, VadState, VadTransition
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

_clip_cache: dict[str, bytes] = {}


def _load_static_clip(path: str) -> bytes:
    """事前生成済みの音声クリップ（μ-law 8kHz生データ）をパスごとにキャッシュ
    して読み込む。ファイルが無い/読めない場合は空バイト列を返し、呼び出し側は
    再生を静かにスキップする（通話を止めない）。"""
    if path in _clip_cache:
        return _clip_cache[path]
    try:
        with open(path, "rb") as f:
            data = f.read()
        logger.info("[CLIP] 音声ファイルを読み込みました path=%s bytes=%d", path, len(data))
    except OSError as e:
        logger.warning(
            "[CLIP] 音声ファイルを読み込めませんでした path=%s err=%s（再生をスキップします）",
            path, e,
        )
        data = b""
    _clip_cache[path] = data
    return data


def preload_static_clips() -> None:
    """起動時に静的音声クリップ（挨拶・縮退運転案内・相槌）を読み込み、
    通話中の初回再生での遅延を避ける（main.pyのlifespanから呼ぶ）。"""
    _load_static_clip(config.GREETING_AUDIO_PATH)
    _load_static_clip(config.DEGRADED_AUDIO_PATH)
    if config.ENABLE_FILLER:
        _load_static_clip(config.FILLER_AUDIO_PATH)


# 挨拶を「言い終えたもの」として扱わせるための追加ルール。プロンプトDBの
# 内容に関わらず常に付け足す（改善指示書「挨拶即時再生」2章）。
GREETING_ALREADY_SAID_NOTICE = (
    f"\n\n【システム注記】挨拶「{GREETING_TEXT}」は既に音声で再生済みで、"
    "会話履歴にもあなたの発言として記録されています。この後の最初の応答で"
    "挨拶を繰り返さないでください。"
)


@dataclass
class _PendingConnection:
    """/voice webhook受信時点で開始したOpenAI接続の前倒し（プリウォーム）状態。
    call_sidをキーに _pending_connections で管理し、Media Streamの`start`
    受信時にrun()側が回収する（改善指示書「挨拶即時再生」3章）。"""
    task: asyncio.Task
    webhook_at: float


_pending_connections: dict[str, _PendingConnection] = {}
_PENDING_CONNECTION_TTL_SEC = 30


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

        # バージイン時のconversation.item.truncateに使う状態（改善指示書
        # 「バージイン実装」修正1）。_current_item_idは再生中のassistant
        # メッセージアイテムのid（response.output_item.addedで取得）。
        # response.createを経由しないクリップ再生（挨拶・縮退運転案内）では
        # Noneのままとなり、truncateが不要なことを示す。
        self._current_item_id: str | None = None
        self._current_response_done = False
        # [BARGE-IN] 抑止ログ（500ms未満で終わった短い割り込み）の検知用。
        self._last_barge_in_run_ms = 0.0

        # OpenAI接続の前倒し（改善指示書「挨拶即時再生」3章）用の状態。
        # 接続完了までに届いたユーザー音声はここにキューし、完了後にまとめて
        # 送る。キュー中に発話終了(END_OF_SPEECH)を検知した場合は
        # _deferred_end_of_speech を立て、接続完了後のflush直後にcommitする。
        self._openai_ready = asyncio.Event()
        self._pending_audio_queue: list[str] = []
        self._deferred_end_of_speech = False

        # 応答音声のフレーム整形・頭切れ対策・診断ログ用の状態（改善指示書2章）。
        # response.created を受けるたびに作り直す（pump_openai_to_twilio参照）。
        self._frame_aligner = twilio_client.FrameAligner()
        self._lead_silence_sent = False
        self._audio_stats = _AudioStats()
        # mark名 -> そのmarkが対応する応答のAudioStats。mark確定（送信）時点で
        # スナップショットを保持し、mark受信時にそれを引く（改善指示書
        # 「バージイン実装」修正4）。session._audio_statsは次の応答が始まると
        # 上書きされてしまうため、これを直接参照するとresp_idを取り違える。
        self._audio_stats_by_mark: dict[str, _AudioStats] = {}

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

    start_received_at = time.monotonic()
    session.stream_sid = start_event.get("streamSid", "")
    start_block = start_event.get("start", {}) or {}
    session.call_sid = start_block.get("callSid", "")
    custom_params = start_block.get("customParameters", {}) or {}
    session.caller_number = custom_params.get("caller", "")

    call_logger.log_event(session.call_sid, session.caller_number, 'to_arrived', 'SUCCESS')

    # 挨拶はOpenAI接続を待たず、事前生成クリップを即座に再生する
    # （改善指示書「挨拶即時再生」1章）。OpenAI接続自体は /voice webhook
    # 受信時点で既に裏で開始されている（main.py参照）。
    await _play_clip_with_mark(session, twilio_ws, config.GREETING_AUDIO_PATH, mark_prefix="greeting")
    session.greeting_done = True
    logger.info(
        "[GREETING] start受信からクリップ送信まで=%.0fms call_sid=%s",
        (time.monotonic() - start_received_at) * 1000, session.call_sid,
    )

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(openai_connection_task(session, twilio_ws, start_received_at))
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
            # VAD状態に関わらず常時転送する（発話開始直後の音の頭切れを
            # 防ぐため。既存main.pyの設計思想を踏襲）。OpenAI接続がまだ
            # 準備できていない間（改善指示書「挨拶即時再生」3章）はローカルに
            # キューし、接続完了後にopenai_connection_taskがまとめて送る。
            if session._openai_ready.is_set():
                await session.openai.append_audio(payload_b64)
            else:
                session._pending_audio_queue.append(payload_b64)

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
                    await _handle_vad_event(session, twilio_ws, transition)
                elif session.ai_is_speaking and session._last_barge_in_run_ms > 0 and transition.elapsed_ms == 0:
                    # バージイン閾値(500ms)に届かず途切れた短い音（咳・相槌等）。
                    # デバッグ用に抑止ログを出す（改善指示書「バージイン実装」）。
                    logger.info(
                        "[BARGE-IN] 抑止 speech_ms=%.0f (<%dms)",
                        session._last_barge_in_run_ms, config.BARGE_IN_MIN_MS,
                    )
                session._last_barge_in_run_ms = transition.elapsed_ms if session.ai_is_speaking else 0.0

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
    Twilio側での欠落・遅延を示す。

    mark名に紐付けて確定時点で保持したスナップショットを引く
    （session._audio_statsを直接見ると、次の応答が既に始まっている場合に
    別応答のresp_idを記録してしまうバグがあったため。改善指示書
    「バージイン実装」修正4）。
    """
    stats = session._audio_stats_by_mark.pop(mark_name, None)
    if stats is None or stats.deltas == 0:
        return  # このmarkに対応する応答でOpenAI音声を受信していない（相槌等）
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


async def _handle_vad_event(session: CallSession, twilio_ws, transition: VadTransition) -> None:
    event = transition.event
    if event == VadEvent.SPEECH_STARTED:
        _refresh_silence_eligibility(session, reason="speech")

    elif event == VadEvent.END_OF_SPEECH:
        if not session._openai_ready.is_set():
            # OpenAI接続がまだ準備できていない（改善指示書「挨拶即時再生」
            # 3章）。接続完了後、openai_connection_taskがキューflush直後に
            # まとめてcommit+response.createする。
            session._deferred_end_of_speech = True
            logger.info("[QUEUE] 発話終了を検知しましたが接続待ちのためcommitを保留します")
        else:
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
        await _handle_barge_in(session, twilio_ws, transition.elapsed_ms)


async def _handle_barge_in(session: CallSession, twilio_ws, speech_ms: float) -> None:
    """バージイン発動時の処理（改善指示書「バージイン実装」修正1）。指示書
    どおりの順序で実行する:
    1) response.cancel（当該応答がまだ生成中の場合のみ）
    2) Twilioへclearを送り再生キューを破棄
    3) audio_playingを即時Falseにし、以後のmarkを無効化
    4) conversation.item.truncateでモデルの履歴を実際に聞こえたところまで
       切り詰める（response.createを経由した実応答の場合のみ。挨拶/縮退運転
       クリップの再生中はitem_idが無いため対象外＝clearのみでよい）。
    """
    item_id = session._current_item_id
    mark_name = session._pending_mark_name
    has_active_response = item_id is not None and session.openai is not None

    if has_active_response and not session._current_response_done:
        await session.openai.response_cancel(session._response_id)

    await twilio_client.send_clear(twilio_ws, session.stream_sid)

    session.ai_is_speaking = False
    session._pending_mark_name = None
    if mark_name is not None:
        session._audio_stats_by_mark.pop(mark_name, None)

    if has_active_response:
        stats = session._audio_stats
        elapsed_ms = 0.0
        if stats.first_frame_sent_at is not None:
            elapsed_ms = (time.monotonic() - stats.first_frame_sent_at) * 1000
        expected_ms = stats.bytes_out / 8000.0 * 1000
        # Twilio側の実再生はサーバー送信より遅延するため、この見積もりは
        # 実際より数百ms多めになりうる。切り詰めすぎて既に聞こえた内容を
        # 消してしまうより安全側なので、この近似でよい（改善指示書に同旨）。
        audio_end_ms = max(0, round(min(elapsed_ms, expected_ms) if expected_ms > 0 else elapsed_ms))
        await session.openai.truncate_item(item_id, audio_end_ms)
        logger.info(
            "[BARGE-IN] 発動 speech_ms=%.0f 対象=%s truncate audio_end_ms=%d",
            speech_ms, mark_name or "?", audio_end_ms,
        )
        if stats.deltas:
            logger.info(
                "[AUDIO-STATS] resp_id=%s barge_in=True deltas=%d bytes_in=%d bytes_out=%d",
                stats.resp_id, stats.deltas, stats.bytes_in, stats.bytes_out,
            )
    else:
        # OpenAI接続確立前、または挨拶/縮退運転クリップの再生中のバージイン。
        # キャンセル・切り詰めるべきOpenAI側の応答は存在しない
        # （改善指示書「挨拶即時再生」3章／「バージイン実装」）。
        logger.info(
            "[BARGE-IN] 発動 speech_ms=%.0f 対象=%s (クリップ再生のためtruncate不要)",
            speech_ms, mark_name or "?",
        )

    session._current_item_id = None
    _refresh_silence_eligibility(session)


async def _play_filler(session: CallSession, twilio_ws) -> None:
    """事前録音の短い相槌音声をTwilioへ即座に送る（改善指示書1-b）。
    OpenAIの音声パス（_audio_stats/_frame_aligner）とは完全に独立させる
    （フィラーは診断対象の「OpenAI→Twilio中継」には含めない）。markも
    送らない（AWAITING_RESPONSE中はVADが確率で遷移しないため不要）。"""
    data = _load_static_clip(config.FILLER_AUDIO_PATH)
    if not data:
        return
    for i in range(0, len(data), twilio_client.FRAME_BYTES):
        chunk = data[i:i + twilio_client.FRAME_BYTES]
        if len(chunk) < twilio_client.FRAME_BYTES:
            chunk = chunk + twilio_client.SILENCE_BYTE * (twilio_client.FRAME_BYTES - len(chunk))
        await twilio_client.send_media_bytes(twilio_ws, session.stream_sid, chunk)
    logger.info("[FILLER] 相槌音声を再生しました bytes=%d", len(data))


async def _play_clip_with_mark(session: CallSession, twilio_ws, path: str, mark_prefix: str) -> None:
    """事前録音クリップ（挨拶・縮退運転案内）を、通常のOpenAI応答と同じ
    mark/ai_is_speaking管理で再生する（改善指示書「挨拶即時再生」1章）。
    OpenAIのイベントを経由しないため_audio_stats/_frame_alignerの対象には
    しない。"""
    data = _load_static_clip(path)
    if not data:
        return
    session.ai_is_speaking = True
    await twilio_client.send_lead_silence(twilio_ws, session.stream_sid, config.RESPONSE_LEAD_SILENCE_MS)
    for i in range(0, len(data), twilio_client.FRAME_BYTES):
        chunk = data[i:i + twilio_client.FRAME_BYTES]
        if len(chunk) < twilio_client.FRAME_BYTES:
            chunk = chunk + twilio_client.SILENCE_BYTE * (twilio_client.FRAME_BYTES - len(chunk))
        await twilio_client.send_media_bytes(twilio_ws, session.stream_sid, chunk)
    session._mark_seq += 1
    mark_name = f"{mark_prefix}_{session._mark_seq}"
    session._pending_mark_name = mark_name
    await twilio_client.send_mark(twilio_ws, session.stream_sid, mark_name)


def prewarm_openai_connection(call_sid: str) -> None:
    """/voice webhook受信時点で呼ぶ。OpenAI WebSocket接続・session確立・
    挨拶の会話履歴注入を、Media Streamの`start`受信を待たずに裏で開始して
    おく（改善指示書「挨拶即時再生」3章）。録音開始RESTも非ブロッキングで
    並行実行する（同4章）。
    """
    if not call_sid:
        return
    if call_sid in _pending_connections:
        # Twilioがwebhookを再送した場合等の保険。二重に接続を開始すると
        # 片方が使われないまま残ってしまうため無視する。
        logger.info("[OA-CONNECT] 既にプリウォーム済みのためスキップします call_sid=%s", call_sid)
        return
    webhook_at = time.monotonic()
    task = asyncio.create_task(_connect_openai_for_call(call_sid))
    _pending_connections[call_sid] = _PendingConnection(task=task, webhook_at=webhook_at)
    asyncio.create_task(_cleanup_stale_pending_connection(call_sid))
    asyncio.create_task(asyncio.to_thread(twilio_client.start_recording, call_sid))


async def _cleanup_stale_pending_connection(call_sid: str) -> None:
    """Media Streamの`start`が届かないまま放置されたプリウォーム接続を掃除
    する（着信直後に切れた等のレアケース対策）。通常経路ではrun()側の
    openai_connection_taskが_pending_connectionsから即座に取り出すため、
    ここには到達しない。"""
    await asyncio.sleep(_PENDING_CONNECTION_TTL_SEC)
    pending = _pending_connections.pop(call_sid, None)
    if pending is None:
        return
    logger.warning(
        "[OA-CONNECT] startイベントが届かないため未使用の接続を破棄します call_sid=%s",
        call_sid,
    )
    pending.task.cancel()
    try:
        socket, _ = await pending.task
        await socket.close()
    except (asyncio.CancelledError, Exception):
        pass


async def _wait_for_session_updated(socket: OpenAiRealtimeSocket) -> None:
    """session.update後、session.updatedイベントが返るまで待つ。GA版APIの
    確認事項（openai_client.pyのdocstring参照）を踏まえ、session.created/
    session.updated双方をエコーログに残す。"""
    async for event in socket.recv_events():
        event_type = event.get("type")
        if event_type in ("session.created", "session.updated"):
            log_session_echo(event)
        if event_type == "session.updated":
            return
        if event_type == "error":
            raise RuntimeError(f"OpenAI session更新でエラー: {event.get('error')}")


async def _connect_openai_for_call(call_sid: str) -> tuple[OpenAiRealtimeSocket, float]:
    """OpenAI WebSocketへ接続し、session.updateと挨拶の会話履歴注入まで
    完了させる。戻り値は (接続済みソケット, 準備完了時刻)。"""
    socket = await OpenAiRealtimeSocket().connect()
    instructions = call_logger.get_prompt(call_logger.DEFAULT_INSTRUCTIONS) + GREETING_ALREADY_SAID_NOTICE
    if config.ENABLE_FILLER:
        instructions += FILLER_ANTI_DOUBLE_SPEECH_NOTICE
    await socket.send_session_update(instructions)
    await _wait_for_session_updated(socket)
    await socket.inject_greeting_said()
    logger.info("[OA-CONNECT] session確立 call_sid=%s", call_sid)
    return socket, time.monotonic()


async def openai_connection_task(session: CallSession, twilio_ws, start_received_at: float) -> None:
    """挨拶クリップ再生と並行して走るタスク。プリウォームされたOpenAI接続を
    回収し、待機中に溜まったユーザー音声をflushする。接続が間に合わなかった/
    失敗した場合は縮退運転に切り替えてCallEndedを送出する（改善指示書
    「挨拶即時再生」3章）。"""
    pending = _pending_connections.pop(session.call_sid, None)
    try:
        if pending is not None:
            socket, ready_at = await asyncio.wait_for(
                pending.task, timeout=config.OPENAI_CONNECT_TIMEOUT_SEC
            )
            logger.info(
                "[OA-CONNECT] webhook起点=%.0fms start起点=%.0fms (session.updated完了まで) call_sid=%s",
                (ready_at - pending.webhook_at) * 1000,
                max(0.0, ready_at - start_received_at) * 1000,
                session.call_sid,
            )
        else:
            # プリウォームされていない場合のフォールバック（何らかの理由で
            # /voiceハンドラでの前倒し登録に失敗した場合の保険）。
            logger.warning(
                "[OA-CONNECT] プリウォームされた接続が見つかりません。ここで新規接続します call_sid=%s",
                session.call_sid,
            )
            asyncio.create_task(asyncio.to_thread(twilio_client.start_recording, session.call_sid))
            socket, _ = await asyncio.wait_for(
                _connect_openai_for_call(session.call_sid), timeout=config.OPENAI_CONNECT_TIMEOUT_SEC
            )
    except Exception as e:
        logger.error(
            "[OA-CONNECT] 接続に失敗またはタイムアウトしました call_sid=%s err=%s",
            session.call_sid, e,
        )
        call_logger.log_event(
            session.call_sid, session.caller_number,
            'ws_connected', 'FAILURE', f'OpenAI接続がタイムアウトまたは失敗しました: {e}',
        )
        await _degraded_connection_failure(session, twilio_ws)
        raise watchdogs.CallEnded("openai_connect_failure")

    session.openai = socket
    call_logger.log_event(session.call_sid, session.caller_number, 'ws_connected', 'SUCCESS')

    queued = session._pending_audio_queue
    session._pending_audio_queue = []
    for payload_b64 in queued:
        await session.openai.append_audio(payload_b64)
    logger.info(
        "[QUEUE] flush frames=%d (%dms分) call_sid=%s",
        len(queued), len(queued) * 20, session.call_sid,
    )

    session._openai_ready.set()

    if session._deferred_end_of_speech:
        session._deferred_end_of_speech = False
        logger.info("[QUEUE] 保留していた発話終了検知でcommitを送信します call_sid=%s", session.call_sid)
        await session.openai.commit()
        await session.openai.response_create()
        session.response_deadline = time.monotonic() + config.RESPONSE_WATCHDOG_FIRST_SEC
        session.response_watchdog_stage = 0
        if config.ENABLE_FILLER:
            await _play_filler(session, twilio_ws)


async def _degraded_connection_failure(session: CallSession, twilio_ws) -> None:
    """OpenAI接続がタイムアウト/失敗した場合の縮退運転（改善指示書
    「挨拶即時再生」3章）。案内クリップ再生→切電→要折り返しケース作成。
    Google Chat通知はlog_eventがFAILUREステータスで自動送信する
    （openai_connection_task側で既に呼び出し済み）。既存のgoodbye_event
    による再生完了待ちの仕組み（watchdogs.pyの_say_and_wait_for_goodbyeと
    同じパターン）を流用する。"""
    session.transcript_lines.append("(OpenAI接続に失敗したため縮退運転に切り替えました)")
    session.is_goodbye = True
    await _play_clip_with_mark(session, twilio_ws, config.DEGRADED_AUDIO_PATH, mark_prefix="goodbye")
    try:
        await asyncio.wait_for(session.goodbye_event.wait(), timeout=8.0)
        await asyncio.sleep(1.0)
    except asyncio.TimeoutError:
        logger.warning("[OA-CONNECT] 縮退運転クリップの再生完了を待てませんでした（続行します）")
    twilio_client.hangup_call(session.call_sid)
    await asyncio.to_thread(
        salesforce_case.create_salesforce_case,
        session.transcript_lines, session.call_sid, session.caller_number,
        True,
    )
    session.case_created = True


async def pump_openai_to_twilio(session: CallSession, twilio_ws):
    # OpenAI接続の前倒し（改善指示書「挨拶即時再生」3章）により、このタスクは
    # openai_connection_taskが接続を確立するまでsession.openaiがNoneのまま
    # 起動しうる。準備完了まで待つ。
    await session._openai_ready.wait()
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
            # 作り直す（改善指示書2章）。バージイン用のitem追跡もリセットする
            # （改善指示書「バージイン実装」修正1）。
            session._frame_aligner = twilio_client.FrameAligner()
            session._lead_silence_sent = False
            session._audio_stats = _AudioStats(resp_id=session._response_id)
            session._current_item_id = None
            session._current_response_done = False

        elif event_type == "response.output_item.added":
            # バージイン時のconversation.item.truncateに必要なitem_idを取得する
            # （改善指示書「バージイン実装」修正1）。
            item = event.get("item") or {}
            if isinstance(item, dict):
                session._current_item_id = item.get("id")

        elif event_type == "response.done":
            session.turn_detector.force_idle()
            session.response_deadline = None
            session.response_watchdog_stage = 0
            session._current_response_done = True
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
                    # ai_is_speakingもここで確実にTrueにする（改善指示書
                    # 「バージイン実装」で判明: output_audio_buffer.startedに
                    # だけ頼るとバージイン判定が効かない実測があったため、
                    # 実際に音声送信を開始した事実そのものをトリガーにする）。
                    await twilio_client.send_lead_silence(
                        twilio_ws, session.stream_sid, config.RESPONSE_LEAD_SILENCE_MS
                    )
                    session._lead_silence_sent = True
                    session.ai_is_speaking = True
                    _refresh_silence_eligibility(session)

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
            # mark名とAudioStatsの対応をこの時点（フレーム送信完了時点）で
            # 確定しておく。session._audio_statsは次の応答が始まると
            # 上書きされるため、mark受信時に直接参照すると別応答のresp_idを
            # 誤って記録してしまう（改善指示書「バージイン実装」修正4）。
            session._audio_stats_by_mark[mark_name] = session._audio_stats
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
