"""切断主体の推定記録（修正指示書パートA）のテスト。

分類は「Twilioが切断主体を返さない」前提の状況証拠の合成なので、ここで
検証するのは『どの観測状態の組み合わせがどのカテゴリになるか』の一点。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session
from vad import VadEvent, VadState, VadTransition


class _Patch:
    """属性を一時的に差し替えるヘルパー（他のテストファイルと同じ実装）。"""

    _MISSING = object()

    def __init__(self, obj, name, value):
        self.obj, self.name, self.value = obj, name, value

    def __enter__(self):
        self.old = getattr(self.obj, self.name, self._MISSING)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        if self.old is self._MISSING:
            delattr(self.obj, self.name)
        else:
            setattr(self.obj, self.name, self.old)


def _session(**overrides) -> call_session.CallSession:
    session = call_session.CallSession()
    session.call_sid = "CA_test"
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def test_pre_connect_hangup_when_oa_session_never_established():
    """今回の1本目（Duration=0秒・[OA-CONNECT] session確立 が出ないまま終了）の
    再現。OA接続確立前に終わった通話は接続前切れに分類される。"""
    category, reason = call_session.classify_disconnect(_session(
        end_reason="twilio_stop",
        call_status_meta={"CallDuration": "0"},
    ))
    assert category == "PRE_CONNECT_HANGUP"
    assert "接続" in reason


def test_hangup_during_greeting():
    """OA接続はしたが挨拶クリップの再生完了markが返る前に終わった通話。"""
    category, _ = call_session.classify_disconnect(_session(
        oa_session_established=True,
        greeting_mark_done=False,
        end_reason="twilio_disconnected",
    ))
    assert category == "HANGUP_DURING_GREETING"


def test_hangup_no_speech():
    """挨拶は流れたが、VADが一度もSPEECH_STARTEDを出さないまま終わった通話。"""
    category, _ = call_session.classify_disconnect(_session(
        oa_session_established=True,
        greeting_mark_done=True,
        any_speech_detected=False,
        last_completed_mark="greeting_1",
    ))
    assert category == "HANGUP_NO_SPEECH"


def test_short_call_after_speech_uses_duration_when_available():
    """発話ありでもDurationが極端に短ければ途中切れ寄りと注記する。"""
    category, reason = call_session.classify_disconnect(_session(
        oa_session_established=True,
        greeting_mark_done=True,
        any_speech_detected=True,
        response_count=1,
        last_completed_mark="resp_2",
        call_status_meta={"CallDuration": "3"},
    ))
    assert category == "SHORT_CALL_AFTER_SPEECH"
    assert "3秒" in reason


def test_normal_completion_without_status_callback():
    """StatusCallbackが取れなくても（CallDuration不明でも）分類は成立する。
    完了条件A-4-5: duration=? でも分類ログ自体は必ず出せること。"""
    category, reason = call_session.classify_disconnect(_session(
        oa_session_established=True,
        greeting_mark_done=True,
        any_speech_detected=True,
        response_count=3,
        last_completed_mark="resp_4",
        call_status_meta={},
    ))
    assert category == "NORMAL_COMPLETION"
    assert "response_count=3" in reason


def test_malformed_duration_does_not_break_classification():
    """Twilioから想定外の値が来ても分類を落とさない。"""
    category, _ = call_session.classify_disconnect(_session(
        oa_session_established=True,
        greeting_mark_done=True,
        any_speech_detected=True,
        call_status_meta={"CallDuration": "not-a-number"},
    ))
    assert category == "NORMAL_COMPLETION"


def test_server_initiated_hangup_is_separated_from_caller_hangup():
    """無音タイムアウト等でサーバー側から切った通話を、発信側の切断として
    誤分類しないこと（分類データを汚さないための最優先の切り分け）。"""
    for reason_code in ("silence_timeout", "max_duration",
                        "response_watchdog_escalation", "openai_connect_failure"):
        category, text = call_session.classify_disconnect(_session(
            end_reason=reason_code,
            oa_session_established=False,
        ))
        assert category == "SERVER_INITIATED_HANGUP", reason_code
        assert reason_code in text


def test_record_call_status_attaches_meta_to_live_session():
    session = _session()
    with _Patch(call_session, "_active_sessions", {"CA_test": session}):
        call_session.record_call_status("CA_test", {"CallDuration": "42"})
    assert session.call_status_meta == {"CallDuration": "42"}
    category, _ = call_session.classify_disconnect(_session(
        oa_session_established=True, greeting_mark_done=True,
        any_speech_detected=True, call_status_meta=session.call_status_meta,
    ))
    assert category == "NORMAL_COMPLETION"


def test_record_call_status_after_state_gone_logs_only():
    """分類ログより後にStatusCallbackが届いた場合、単独ログだけ残して
    落ちないこと（通話後処理をブロックしない設計の裏返し）。"""
    logged = []

    def fake_info(msg, *args):
        logged.append(msg % args)

    with _Patch(call_session, "_active_sessions", {}), \
         _Patch(call_session.logger, "info", fake_info):
        call_session.record_call_status("CA_gone", {"CallDuration": "12"})

    assert len(logged) == 1
    assert "late_status" in logged[0]
    assert "分類には未反映" in logged[0]


def test_oa_established_before_session_registration_is_claimed_later():
    """プリウォーム接続がMedia Streamの`start`より先に完了した場合でも、
    session確立の事実が取りこぼされないこと（PRE_CONNECT_HANGUPの誤検知防止）。"""
    with _Patch(call_session, "_active_sessions", {}), \
         _Patch(call_session, "_deferred_facts", {}):
        # CallSession未登録の状態で確立 → 預かり所へ
        call_session._mark_oa_established("CA_early")
        session = _session(call_sid="CA_early")
        assert session.oa_session_established is False

        call_session._claim_deferred_facts(session)
        assert session.oa_session_established is True

    category, _ = call_session.classify_disconnect(session)
    assert category != "PRE_CONNECT_HANGUP"


def test_recording_sid_is_recorded_on_live_session():
    session = _session(call_sid="CA_rec")
    with _Patch(call_session, "_active_sessions", {"CA_rec": session}), \
         _Patch(call_session, "_deferred_facts", {}):
        call_session._remember_recording_sid("CA_rec", "RE123")
    assert session.recording_sid == "RE123"


def test_recording_sid_before_registration_is_claimed_later():
    """録音開始RESTが`start`より先に完了しても Recording SID を落とさないこと。"""
    with _Patch(call_session, "_active_sessions", {}), \
         _Patch(call_session, "_deferred_facts", {}):
        call_session._remember_recording_sid("CA_early", "RE999")
        session = _session(call_sid="CA_early")
        call_session._claim_deferred_facts(session)
    assert session.recording_sid == "RE999"


def test_speech_started_sets_any_speech_detected():
    """VADのSPEECH_STARTED検知点でフラグが立つこと（状態機械には触らない）。"""

    async def scenario():
        session = _session()
        transition = VadTransition(VadEvent.SPEECH_STARTED, VadState.SPEAKING, 0.9, 250.0)
        await call_session._handle_vad_event(session, None, transition)
        assert session.any_speech_detected is True
        # 無音タイマーの起点は従来どおりリセットされる（デグレしていない）
        assert session.silence_anchor is not None

    asyncio.run(scenario())


class _FakeTwilioWs:
    """指定のイベント列を順に返すMedia Streams WebSocketの偽物。"""

    def __init__(self, events):
        self._events = list(events)

    async def receive_text(self):
        if not self._events:
            raise AssertionError("テストが用意したイベントを使い切りました")
        return json.dumps(self._events.pop(0))


def test_mark_events_record_playback_progress():
    """再生進捗の根拠はmarkのみ（response.doneは使わない）。挨拶markで
    greeting_mark_doneが立ち、以降のmarkでlast_completed_markが更新されること。"""

    async def scenario():
        session = _session()
        session._pending_mark_name = "greeting_1"
        ws = _FakeTwilioWs([
            {"event": "mark", "mark": {"name": "greeting_1"}},
            {"event": "stop"},
        ])
        try:
            await call_session.pump_twilio_to_openai(session, ws)
        except call_session.watchdogs.CallEnded:
            pass

        assert session.greeting_mark_done is True
        assert session.last_completed_mark == "greeting_1"
        # 既存の再生完了トラッキングを壊していないこと
        assert session.ai_is_speaking is False
        assert session._pending_mark_name is None

        session._pending_mark_name = "resp_2"
        ws = _FakeTwilioWs([
            {"event": "mark", "mark": {"name": "resp_2"}},
            {"event": "stop"},
        ])
        try:
            await call_session.pump_twilio_to_openai(session, ws)
        except call_session.watchdogs.CallEnded:
            pass
        assert session.last_completed_mark == "resp_2"
        assert session.greeting_mark_done is True

    asyncio.run(scenario())


def test_disconnect_log_is_single_line_and_marked_as_estimate():
    """[DISCONNECT] は1行で出し、断定ではなく推定であることを文面に残すこと。"""
    logged = []

    with _Patch(call_session.logger, "info", lambda msg, *args: logged.append(msg % args)):
        call_session._log_disconnect(_session(
            oa_session_established=True, greeting_mark_done=True,
            any_speech_detected=True, response_count=2, last_completed_mark="resp_3",
        ))

    assert len(logged) == 1
    line = logged[0]
    assert line.startswith("[DISCONNECT] ")
    assert "category=NORMAL_COMPLETION" in line
    assert "duration=?s" in line  # StatusCallback未着でも出る
    assert "推定" in line


if __name__ == "__main__":
    import inspect
    failures = 0
    tests = {name: fn for name, fn in list(globals().items()) if name.startswith("test_") and inspect.isfunction(fn)}
    for name, fn in tests.items():
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)
