"""
3つの独立したタイムアウト監視ループ。

指示書の核心的な要件：既存main.pyでは無音タイムアウト判定がVAD状態の
elifチェーンに巻き込まれて動かなくなるバグがあった。ここでは
silence_watchdog / max_duration_watchdog / response_watchdog を互いに
状態を参照しない独立した asyncio タスクとして実装し、構造的に再発を防ぐ。

いずれかが `CallEnded` を送出すると、call_session.py の TaskGroup が
他の全タスクを道連れにキャンセルする（`except* CallEnded` で正常終了扱い）。
"""
import asyncio
import logging
import time
import traceback

import call_logger
import config
import salesforce_case
import twilio_client
from vad import VadState

logger = logging.getLogger("watchdogs")

SILENCE_TIMEOUT_MESSAGE = "【無音タイムアウト】"
MAX_DURATION_MESSAGE = "【最大通話時間超過】"
ESCALATION_PHRASE = (
    "お電話が遠いようで、うまく聞き取れませんでした。"
    "この番号に最も近い担当者から改めてご連絡いたしますので、恐れ入りますが一度お電話をお切りください。"
)


class CallEnded(Exception):
    """いずれかの監視ループ/イベントハンドラが通話終了を決定したときに送出する。
    自分自身のhangup処理は送出前に完了させておくこと。"""


async def _say_and_wait_for_goodbye(session, text: str, wait_timeout: float = 15.0):
    """conversation.item.create + response.create でフレーズを言わせ、
    Twilioのmark折り返し（=goodbye_event、call_session.pyのmarkハンドラが
    セットする）を待つ。response.doneではなくmarkを待つのは、response.done
    はサーバー側の音声生成完了でしかなく、電話口での実際の再生完了より
    大きく先行するため（実測で5秒以上）。OpenAI接続が死んでいる場合でも
    例外を握りつぶして先に進む（hangupは必ず行う）。"""
    session.is_goodbye = True
    try:
        await session.openai.send_text_turn(text)
    except Exception as e:
        logger.warning("[WATCHDOG] フレーズ送信に失敗しました（続行します）: %s", e)
        return
    try:
        await asyncio.wait_for(session.goodbye_event.wait(), timeout=wait_timeout)
        # markはTwilioへの送信完了通知であり、電話網を通じて実際に耳に
        # 届くまでのわずかな遅延を見込んで一呼吸置く。
        await asyncio.sleep(1.0)
    except asyncio.TimeoutError:
        logger.warning("[WATCHDOG] goodbye再生の完了を待てませんでした（続行します）")


async def silence_watchdog(session):
    """挨拶完了後・AI非発話中・IDLE状態で SILENCE_TIMEOUT_SEC 秒経過したら切断する。
    他のどの状態にも依存しない独立ループ。"""
    while True:
        await asyncio.sleep(1)

        if not session.greeting_done or session.ai_is_speaking:
            continue
        if session.turn_detector.state != VadState.IDLE:
            continue
        if session.silence_anchor is None:
            continue

        elapsed = time.monotonic() - session.silence_anchor
        if elapsed < config.SILENCE_TIMEOUT_SEC:
            continue

        logger.info(
            "[SILENCE] %s秒間無言のため通話を切断します "
            "(経過=%.1fs audio_playing=%s vad_state=%s)",
            config.SILENCE_TIMEOUT_SEC, elapsed,
            session.ai_is_speaking, session.turn_detector.state.name,
        )
        session.transcript_lines.append("(無言タイムアウトのため通話を切断しました)")
        await _say_and_wait_for_goodbye(session, SILENCE_TIMEOUT_MESSAGE)
        twilio_client.hangup_call(session.call_sid)
        call_logger.log_event(session.call_sid, session.caller_number, 'silence_timeout', 'SUCCESS')
        raise CallEnded("silence_timeout")


async def max_duration_watchdog(session):
    """MAX_CALL_DURATION_SEC 秒で状態に関わらず強制的に切断する。
    他のどの状態にも依存しない、最も単純な独立ループ。"""
    await asyncio.sleep(config.MAX_CALL_DURATION_SEC)

    logger.info("[MAX DURATION] 最大通話時間 %s秒 に達したため切断します", config.MAX_CALL_DURATION_SEC)
    session.transcript_lines.append(f"(最大通話時間 {config.MAX_CALL_DURATION_SEC}秒 に達したため切断しました)")
    await _say_and_wait_for_goodbye(session, MAX_DURATION_MESSAGE)
    twilio_client.hangup_call(session.call_sid)
    call_logger.log_event(session.call_sid, session.caller_number, 'max_duration', 'SUCCESS')
    raise CallEnded("max_duration")


async def pump_stall_watchdog(session, twilio_ws):
    """mediaフレーム処理の「進捗」を直接監視する（2026-08-05 pump停止障害）。

    既存の安全網には構造的な盲点があった：`silence_watchdog` はVAD状態が
    IDLEでないと発火できない。pumpがawaitハングで止まると `TurnDetector.update()`
    が呼ばれなくなり、状態はSPEECH_STARTED直後のSPEAKINGに固着する。結果、
    無音ウォッチドッグは永久にcontinueし続け、発火できない。実際にこれで
    「無反応のまま切電もされない」通話が発生した。

    このウォッチドッグは状態を一切見ず、「最後にフレームを処理し終えた時刻」
    だけを見る。**状態が固着しても進捗の停止は必ず観測できる**ため、ハングの
    原因・場所を問わず捕捉できる。

    発火時は (1)pumpタスクのスタックをダンプして止まったawaitを名指しし、
    (2)OpenAIを経由しない縮退運転（クリップ→切電→ケース作成）で通話を必ず畳む。
    """
    import call_session  # 循環import回避のための遅延import（既にロード済み）

    while True:
        await asyncio.sleep(1)

        if session.last_frame_processed_at is None:
            continue  # まだ1フレームも処理していない（開始直後）
        if session.degrade_started:
            # 別経路が既に縮退運転中。ここで割り込むとケース作成を中断させて
            # しまうため、進捗が止まって見えても手を出さない。
            continue

        stalled_sec = time.monotonic() - session.last_frame_processed_at
        if stalled_sec < config.PUMP_STALL_SEC:
            continue

        logger.error(
            "[PUMP-STALL] mediaフレーム処理が %.1f秒 進んでいません。"
            "pumpタスクのスタックを出力し、縮退運転で通話を終了します。 call_sid=%s",
            stalled_sec, session.call_sid,
        )
        _dump_pump_task_stack(session)
        await call_session.degrade_and_end(session, twilio_ws, reason="pump_stall")


def _dump_pump_task_stack(session) -> None:
    """pumpタスクが今どのawaitで止まっているかをログに吐く（原因特定器）。

    このスタックの最深フレームが、ハングしているawaitの行を名指しする。
    候補は append_audio（OpenAI送信のバックプレッシャー）/ VAD推論のexecutor /
    receive_text（Twilio側の半死）の3つで、ログからは区別できなかった。
    """
    task = getattr(session, "pump_task", None)
    if task is None:
        logger.error("[PUMP-STALL] pump_task参照がありません（スタック取得不可）")
        return
    try:
        frames = task.get_stack()
    except Exception as e:
        logger.error("[PUMP-STALL] スタック取得に失敗しました: %s", e)
        return
    if not frames:
        logger.error("[PUMP-STALL] pumpタスクのスタックが空です done=%s", task.done())
        return
    for frame in frames:
        logger.error("[PUMP-STALL] pump stack:\n%s", "".join(traceback.format_stack(frame)))


async def response_watchdog(session):
    """commit + response.create 送信後、応答が始まらない場合の縮退運転。
    5秒でresponse.createのみ再送（commitは再送しない＝空バッファエラー回避）、
    さらに5秒（計10秒）で縮退運転フレーズ→hangup→即座にSalesforceケース作成
    （案内なしで通話が終わっても発信元番号を絶対に落とさないための最優先経路）。
    """
    while True:
        await asyncio.sleep(1)

        if session.response_deadline is None:
            continue
        if time.monotonic() < session.response_deadline:
            continue

        if session.response_watchdog_stage == 0:
            logger.warning("[WATCHDOG] 5秒応答なし。response.createを再送します")
            session.response_watchdog_stage = 1
            session.response_deadline = time.monotonic() + config.RESPONSE_WATCHDOG_SECOND_SEC
            try:
                await session.openai.response_create()
            except Exception as e:
                logger.warning("[WATCHDOG] response.create再送に失敗: %s", e)
            continue

        logger.error("[WATCHDOG] 応答ウォッチドッグ縮退運転を開始します（OpenAI応答なし）")
        session.transcript_lines.append("(応答が届かないため縮退運転に切り替えました)")
        await _say_and_wait_for_goodbye(session, ESCALATION_PHRASE, wait_timeout=8.0)
        twilio_client.hangup_call(session.call_sid)

        await asyncio.to_thread(
            salesforce_case.create_salesforce_case,
            session.transcript_lines,
            session.call_sid,
            session.caller_number,
            True,
            session.recording_sid,
        )
        session.case_created = True
        call_logger.log_event(
            session.call_sid, session.caller_number,
            'response_watchdog_escalation', 'FAILURE', 'OpenAIから応答が届きませんでした',
        )
        raise CallEnded("response_watchdog_escalation")
