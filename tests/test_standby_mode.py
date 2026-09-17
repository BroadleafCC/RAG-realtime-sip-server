import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session
import config
import watchdogs
from vad import VadEvent, VadState, VadTransition


class FakeOpenAiSocket:
    """OpenAiRealtimeSocketの偽物。送信内容を記録するだけで、ネットワークには
    一切触れない（tests/test_barge_in.py, tests/test_call_session_prewarm.py
    と同じパターン）。"""

    def __init__(self):
        self.sent = []

    async def commit(self):
        self.sent.append(("commit", None))

    async def response_create(self):
        self.sent.append(("response_create", None))

    async def send_function_call_output(self, call_id, output):
        self.sent.append(("function_call_output", call_id, output))

    async def send_text_turn(self, text):
        self.sent.append(("send_text_turn", text))


class _Patch:
    """属性を一時的に差し替えるヘルパー（tests/test_call_session_prewarm.py
    と同じもの。フィクスチャ非依存にするため各テストファイルで個別定義する）。"""

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


def _make_ready_session():
    session = call_session.CallSession()
    session.call_sid = "CA_standby_test"
    session.stream_sid = "MZ_standby_test"
    session.openai = FakeOpenAiSocket()
    session._openai_ready.set()
    session.greeting_done = True
    session.turn_detector.state = VadState.IDLE
    return session


def test_enter_standby_mode_limit_is_enforced_across_reentries():
    """指示書4-1章: enter_standby_modeを繰り返し呼び出すと、上限
    (STANDBY_MAX_ENTRIES)に達した回はstatus=limit_reachedになり、
    standby_activeは変化しないこと。

    実運用ではチェックイン発話に対しお客様が「まだかかります」と再度言う
    (=END_OF_SPEECHでstandby_activeが一旦Falseに戻る)たびにモデルが
    enter_standby_modeを呼び直すため、ここでもEND_OF_SPEECHを間に挟む。
    既定のSTANDBY_MAX_ENTRIES=3だと3回とも成功してしまうため、上限に
    達する境界を検証できるようこのテストだけ2に差し替える。"""

    async def scenario():
        with _Patch(config, "STANDBY_MAX_ENTRIES", 2):
            session = _make_ready_session()
            ws = None

            async def call_enter_standby(call_id):
                item = {"call_id": call_id, "name": "enter_standby_mode", "arguments": "{}"}
                await call_session._handle_function_call(session, ws, item)
                outputs = [s for s in session.openai.sent if s[0] == "function_call_output" and s[1] == call_id]
                assert len(outputs) == 1
                return outputs[0][2]

            def end_of_speech():
                transition = VadTransition(VadEvent.END_OF_SPEECH, VadState.AWAITING_RESPONSE, 0.0, 0.0)
                return call_session._handle_vad_event(session, ws, transition)

            output1 = await call_enter_standby("call_1")
            assert output1 == '{"status": "ok"}'
            assert session.standby_active is True
            assert session.standby_entries_used == 1

            await end_of_speech()
            assert session.standby_active is False

            output2 = await call_enter_standby("call_2")
            assert output2 == '{"status": "ok"}'
            assert session.standby_active is True
            assert session.standby_entries_used == 2

            await end_of_speech()
            assert session.standby_active is False

            output3 = await call_enter_standby("call_3")
            assert output3 == '{"status": "limit_reached"}'
            assert session.standby_entries_used == 2, "上限到達後はカウントを増やさない"
            assert session.standby_active is False, "上限到達時にstandby_activeをTrueにしてはいけない"

    asyncio.run(scenario())


def test_end_of_speech_clears_standby_state():
    """指示書3-3(c): 待機モード中にお客様が発話を終えた(END_OF_SPEECH)時点で
    standby_active/standby_checkpoint_doneが即座にFalseへ戻ること。"""

    async def scenario():
        session = _make_ready_session()
        session.standby_active = True
        session.standby_checkpoint_done = True
        session.standby_checkpoint_at = time.monotonic() + 100
        session.standby_deadline = time.monotonic() + 100

        transition = VadTransition(VadEvent.END_OF_SPEECH, VadState.AWAITING_RESPONSE, 0.0, 0.0)
        await call_session._handle_vad_event(session, None, transition)

        assert session.standby_active is False
        assert session.standby_checkpoint_done is False
        # END_OF_SPEECHの通常処理(commit+response.create)はそのまま動くこと
        assert ("commit", None) in session.openai.sent
        assert ("response_create", None) in session.openai.sent

    asyncio.run(scenario())


def test_silence_watchdog_is_suppressed_during_standby():
    """指示書3-4(a): standby_active中はsilence_watchdogが早期continueし、
    既定の無音タイムアウトでは切断されないこと。"""

    async def scenario():
        with _Patch(config, "SILENCE_TIMEOUT_SEC", 1):
            session = _make_ready_session()
            session.standby_active = True
            session.ai_is_speaking = False
            session.silence_anchor = time.monotonic() - 100  # とっくに無音タイムアウト超過

            try:
                await asyncio.wait_for(watchdogs.silence_watchdog(session), timeout=2.5)
                raised = False
            except asyncio.TimeoutError:
                raised = False
            except watchdogs.CallEnded:
                raised = True

            assert raised is False, "standby_active中はsilence_watchdogが発火してはいけない"

    asyncio.run(scenario())


def test_standby_watchdog_fires_checkpoint_once():
    """指示書3-4(b) stage1: standby_checkpoint_atに達したらsend_text_turnが
    1回だけ呼ばれ、standby_checkpoint_doneがTrueになること
    （次のポーリングで再度呼ばれないこと）。"""

    async def scenario():
        session = _make_ready_session()
        session.standby_active = True
        session.standby_checkpoint_done = False
        session.standby_checkpoint_at = time.monotonic() - 0.1  # 直ちに到達済み
        session.standby_deadline = time.monotonic() + 100  # まだ到達しない

        try:
            await asyncio.wait_for(watchdogs.standby_watchdog(session), timeout=2.5)
        except asyncio.TimeoutError:
            pass

        checkpoint_calls = [s for s in session.openai.sent if s[0] == "send_text_turn"]
        assert len(checkpoint_calls) == 1
        assert checkpoint_calls[0][1] == config.STANDBY_CHECKPOINT_TRIGGER_TEXT
        assert session.standby_checkpoint_done is True

    asyncio.run(scenario())


def test_standby_watchdog_hangs_up_at_deadline():
    """指示書3-4(b) stage2: standby_deadline(絶対時刻)に達したら、チェック
    インの成否に関わらず事前録音クリップ再生→切電→CallEnded("standby_timeout")
    となること。"""

    async def scenario():
        goodbye_calls = []

        async def fake_goodbye_clip(session, clip_path):
            goodbye_calls.append(clip_path)

        hangup_calls = []
        log_calls = []

        with _Patch(watchdogs, "_play_goodbye_clip", fake_goodbye_clip), \
             _Patch(watchdogs.twilio_client, "hangup_call", lambda call_sid: hangup_calls.append(call_sid)), \
             _Patch(watchdogs.call_logger, "log_event", lambda *a, **k: log_calls.append((a, k))):
            session = _make_ready_session()
            session.standby_active = True
            session.standby_checkpoint_done = True  # チェックイン済みの体
            session.standby_checkpoint_at = time.monotonic() - 100
            session.standby_deadline = time.monotonic() - 0.1  # 直ちに到達済み

            raised = None
            try:
                await watchdogs.standby_watchdog(session)
            except watchdogs.CallEnded as e:
                raised = str(e)

            assert raised == "standby_timeout"
            assert goodbye_calls == [config.SILENCE_GOODBYE_AUDIO_PATH]
            assert hangup_calls == [session.call_sid]
            assert log_calls and log_calls[0][0][2] == "standby_timeout"

    asyncio.run(scenario())


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
