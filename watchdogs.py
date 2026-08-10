"""
4つの独立したタイムアウト監視ループ。

指示書の核心的な要件：既存main.pyでは無音タイムアウト判定がVAD状態の
elifチェーンに巻き込まれて動かなくなるバグがあった。ここでは
silence_watchdog / max_duration_watchdog / response_watchdog /
media_starvation_watchdog を互いに状態を参照しない独立した asyncio タスクと
して実装し、構造的に再発を防ぐ。

いずれかが `CallEnded` を送出すると、call_session.py の TaskGroup が
他の全タスクを道連れにキャンセルする（`except* CallEnded` で正常終了扱い）。

切電時の案内はすべて事前録音クリップ（_play_goodbye_clip）で再生する。
以前はOpenAIに喋らせていたが、故障時の最後の一言が「故障しているかも
しれない部品」に依存する構成だったため、2026-08-07障害対応で外部依存の
ないクリップ方式へ統一した。
"""
import asyncio
import logging
import time
from datetime import timedelta

import call_logger
import config
import salesforce_case
import twilio_client
from vad import VadState

logger = logging.getLogger("watchdogs")


class CallEnded(Exception):
    """いずれかの監視ループ/イベントハンドラが通話終了を決定したときに送出する。
    自分自身のhangup処理は送出前に完了させておくこと。"""


async def _play_goodbye_clip(session, clip_path: str):
    """事前録音クリップで切電案内を再生し、再生完了(mark折り返し)を待つ。
    inbound途絶時はmarkの折り返し自体が届かないため、クリップ長+2秒で
    必ずタイムアウトして先へ進む（outbound送信とTwilio側の再生は
    inboundと独立に機能する可能性が高い）。循環import回避のため
    call_sessionは関数内でimportすること。

    markを待つのは、送信完了と電話口での実際の再生完了が最大5秒以上ズレる
    ため（旧_say_and_wait_for_goodbyeと同じ理由）。"""
    import call_session as cs  # 循環import回避（トップレベルでimportしない）
    session.is_goodbye = True
    clip = cs._load_static_clip(clip_path)
    if not clip:
        # クリップ未配置。_play_clip_with_markも何も送らずに戻るため、
        # markは永久に返らない。無駄な待機をせず即座に切電へ進む。
        logger.warning("[WATCHDOG] 案内クリップが無いため再生を省略します path=%s", clip_path)
        return
    wait_sec = max(4.0, len(clip) / 8000.0 + 2.0)
    try:
        await cs._play_clip_with_mark(session, session.twilio_ws, clip_path, mark_prefix="goodbye")
    except Exception as e:
        logger.warning("[WATCHDOG] クリップ再生に失敗しました（続行します）: %s", e)
        return
    try:
        await asyncio.wait_for(session.goodbye_event.wait(), timeout=wait_sec)
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
        await _play_goodbye_clip(session, config.SILENCE_GOODBYE_AUDIO_PATH)
        twilio_client.hangup_call(session.call_sid)
        call_logger.log_event(session.call_sid, session.caller_number, 'silence_timeout', 'SUCCESS')
        raise CallEnded("silence_timeout")


async def max_duration_watchdog(session):
    """MAX_CALL_DURATION_SEC 秒で状態に関わらず強制的に切断する。
    他のどの状態にも依存しない、最も単純な独立ループ。"""
    await asyncio.sleep(config.MAX_CALL_DURATION_SEC)

    logger.info("[MAX DURATION] 最大通話時間 %s秒 に達したため切断します", config.MAX_CALL_DURATION_SEC)
    session.transcript_lines.append(f"(最大通話時間 {config.MAX_CALL_DURATION_SEC}秒 に達したため切断しました)")
    await _play_goodbye_clip(session, config.SILENCE_GOODBYE_AUDIO_PATH)
    twilio_client.hangup_call(session.call_sid)
    call_logger.log_event(session.call_sid, session.caller_number, 'max_duration', 'SUCCESS')
    raise CallEnded("max_duration")


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

        # 宣言→即マーキング＆通知→案内→切電→ケース、の順に実行する。
        # 途中で発信者切電によりこのタスクがキャンセルされても、FAILURE通知は
        # 送信済みで、finallyが escalation_pending=True を見てケースを作る
        # （case_createdガードにより二重作成はない）。以前は log_event が
        # 末尾にあり、切電レースに負けると障害が正常ケースに擬態していた。
        logger.error("[WATCHDOG] 応答ウォッチドッグ縮退運転を開始します（OpenAI応答なし）")
        session.escalation_pending = True
        call_logger.log_event(
            session.call_sid, session.caller_number,
            'response_watchdog_escalation', 'FAILURE', 'OpenAIから応答が届きませんでした',
        )
        session.transcript_lines.append("(応答が届かないため縮退運転に切り替えました)")
        await _play_goodbye_clip(session, config.ESCALATION_AUDIO_PATH)
        twilio_client.hangup_call(session.call_sid)

        await asyncio.to_thread(
            salesforce_case.create_salesforce_case,
            session.transcript_lines,
            session.call_sid,
            session.caller_number,
            True,
        )
        session.case_created = True
        raise CallEnded("response_watchdog_escalation")


async def media_starvation_watchdog(session):
    """Twilio→サーバーのメディアフレームが途絶した場合の縮退運転
    （2026-08-07障害: 通話4.8〜9.8秒でinboundが停止し、VADがSPEAKING固着
    →全監視が沈黙した）。他のどの状態にも依存しない独立ループ。
    フレームが届かない限りVADは遷移しないため、既存の無音ウォッチドッグの
    「SPEAKING固着では発火できない」盲点もこれが構造的にカバーする。

    根本原因（Twilio/経路側のストリーム停止）はサーバーからは治せないため、
    目的は被害の最小化と可視化に絞る。"""
    if not config.ENABLE_MEDIA_STARVATION_WATCHDOG:
        return
    while True:
        await asyncio.sleep(1)
        if not session.greeting_done or session.last_media_at is None:
            continue
        gap = time.monotonic() - session.last_media_at
        if gap < config.MEDIA_STARVATION_TIMEOUT_SEC:
            continue

        # Twilioへの報告（「ストリーム開始から何秒後にmediaが止まったか」）を
        # ログから直読できるよう、最終フレームの位置を経過秒とUTC時刻の両方で
        # 出す。stream_started_* が未設定の場合でもログは必ず出す。
        if session.stream_started_mono is not None and session.stream_started_utc is not None:
            last_offset = session.last_media_at - session.stream_started_mono
            last_utc = session.stream_started_utc + timedelta(seconds=last_offset)
            last_utc_str = last_utc.strftime("%H:%M:%S.%f")[:-3]
        else:
            last_offset = float("nan")
            last_utc_str = "n/a"
        logger.error(
            "[MEDIA-STARVATION] メディア入力が%.1f秒途絶。縮退運転に切り替えます "
            "(最終フレーム=ストリーム開始+%.1fs / %s UTC / 総受信=%dframes, "
            "vad_state=%s ai_is_speaking=%s)",
            gap, last_offset, last_utc_str, session.media_frame_count,
            session.turn_detector.state.name, session.ai_is_speaking,
        )
        # 宣言時点で先にマーキングと通知を確定させる（切電に先を越されても
        # 失われない）。ケース作成はcall_session.runのfinallyが
        # escalation_pending を引き継いで行う。
        session.escalation_pending = True
        call_logger.log_event(
            session.call_sid, session.caller_number,
            'media_starvation', 'FAILURE',
            f'{gap:.1f}秒間inboundフレームなし (vad={session.turn_detector.state.name})',
        )
        session.transcript_lines.append("(メディア入力が途絶したため縮退運転に切り替えました)")
        await _play_goodbye_clip(session, config.ESCALATION_AUDIO_PATH)
        twilio_client.hangup_call(session.call_sid)
        raise CallEnded("media_starvation")


async def media_rate_reporter(session):
    """受信mediaフレーム数をinterval秒ごとにログする観測専用ループ。
    他のどの状態も参照・変更しない。Twilio調査用の一次証拠
    （正常時は毎秒~50、途絶時は50→0の推移がそのまま残る）。"""
    interval = config.MEDIA_RATE_LOG_INTERVAL_SEC
    if interval <= 0:
        return
    prev = 0
    while True:
        await asyncio.sleep(interval)
        if session.stream_started_mono is None:
            continue
        total = session.media_frame_count
        t = time.monotonic() - session.stream_started_mono
        logger.info("[MEDIA-RATE] t=+%.0fs frames_last=%d total=%d", t, total - prev, total)
        prev = total
