import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session
import config
import watchdogs
from openai_client import ENTER_STANDBY_TOOL
from vad import VadEvent, VadState, VadTransition


class FakeOpenAiSocket:
    """enter_standby_mode / standby_watchdog のテストに必要な最小限の
    OpenAiRealtimeSocket偽物。送信内容を記録するだけでネットワークには
    触れない（tests/test_call_session_prewarm.pyのFakeOpenAiSocketと同趣旨）。"""

    def __init__(self):
        self.sent = []

    async def send_function_call_output(self, call_id, output):
        self.sent.append(("function_call_output", call_id, output))

    async def response_create(self):
        self.sent.append(("response_create", None))

    async def commit(self):
        self.sent.append(("commit", None))

    async def send_text_turn(self, text):
        self.sent.append(("send_text_turn", text))


class _Patch:
    """属性を一時的に差し替えるヘルパー（tests/test_call_session_prewarm.pyと同趣旨。
    フィクスチャに依存させず単体実行(`python tests/test_x.py`)とも互換にするため
    このファイル内に複製している）。"""

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


def _new_session():
    session = call_session.CallSession()
    session.call_sid = "CA_standby_test"
    session.openai = FakeOpenAiSocket()
    session.greeting_done = True
    return session


def test_enter_standby_mode_allows_up_to_max_entries_then_limits():
    """初回+延長2回=合計3回まではstatus=ok、4回目はlimit_reachedとなり、
    standby_activeはFalseのままであること。"""

    async def call_once(session, call_id):
        item = {"call_id": call_id, "name": ENTER_STANDBY_TOOL["name"], "arguments": "{}"}
        await call_session._handle_function_call(session, twilio_ws=None, item=item)
        outputs = [s for s in session.openai.sent if s[0] == "function_call_output" and s[1] == call_id]
        return json.loads(outputs[-1][2])

    async def scenario():
        session = _new_session()
        for i in range(config.STANDBY_MAX_ENTRIES):
            result = await call_once(session, f"call_{i}")
            assert result["status"] == "ok"
            assert session.standby_active is True
            assert session.standby_entries_used == i + 1

        # 上限到達後のもう1回はlimit_reachedとなり、状態は変化しない
        session.standby_active = False  # 直前のENDまでに解除された状態を模す
        result = await call_once(session, "call_over_limit")
        assert result["status"] == "limit_reached"
        assert session.standby_active is False
        assert session.standby_entries_used == config.STANDBY_MAX_ENTRIES

    asyncio.run(scenario())


def test_end_of_speech_clears_standby_mode():
    """待機モード中にEND_OF_SPEECHを検知すると即座にstandby_activeが
    解除されること（顧客が話し始めた時点で待機モードを終わらせる仕様）。"""

    async def scenario():
        session = _new_session()
        session.standby_active = True
        session.standby_checkpoint_done = True
        session._openai_ready.set()

        transition = VadTransition(event=VadEvent.END_OF_SPEECH, state=VadState.AWAITING_RESPONSE, prob=0.1, elapsed_ms=0)
        await call_session._handle_vad_event(session, twilio_ws=None, transition=transition)

        assert session.standby_active is False
        assert session.standby_checkpoint_done is False

    asyncio.run(scenario())


def test_silence_watchdog_skips_while_standby_active():
    """待機モード中はsilence_watchdogが早期continueし、既定の
    SILENCE_TIMEOUT_SEC(10秒)では切断されないこと。"""

    async def scenario():
        session = _new_session()
        session.standby_active = True
        session.turn_detector.state = VadState.IDLE
        session.silence_anchor = time.monotonic() - (config.SILENCE_TIMEOUT_SEC + 5)

        task = asyncio.create_task(watchdogs.silence_watchdog(session))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1.5)
            assert False, "standby_active中はsilence_watchdogが発火してはならない"
        except asyncio.TimeoutError:
            pass
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_standby_watchdog_sends_checkpoint_once():
    """standby_checkpoint_atに到達したらsend_text_turnが1回だけ呼ばれ、
    standby_checkpoint_doneがTrueになること（再度のポーリングで
    重複送信しない）。"""

    async def scenario():
        session = _new_session()
        session.standby_active = True
        session.turn_detector.state = VadState.IDLE
        now = time.monotonic()
        session.standby_checkpoint_at = now - 1  # 既に経過済みにしておく
        session.standby_deadline = now + 30  # 60秒側はまだ先

        task = asyncio.create_task(watchdogs.standby_watchdog(session))
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        checkpoint_calls = [s for s in session.openai.sent if s[0] == "send_text_turn"]
        assert len(checkpoint_calls) == 1
        assert checkpoint_calls[0][1] == config.STANDBY_CHECKPOINT_TRIGGER_TEXT
        assert session.standby_checkpoint_done is True

    asyncio.run(scenario())


def test_standby_watchdog_raises_call_ended_at_deadline():
    """standby_deadline到達時にCallEnded('standby_timeout')が送出されること。"""

    async def scenario():
        session = _new_session()
        session.standby_active = True
        session.standby_checkpoint_done = True
        session.turn_detector.state = VadState.IDLE
        now = time.monotonic()
        session.standby_checkpoint_at = now - 10
        session.standby_deadline = now - 1  # 既に締切超過

        with _Patch(watchdogs.twilio_client, "hangup_call", lambda call_sid: None), \
             _Patch(watchdogs, "_play_goodbye_clip", _noop_goodbye), \
             _Patch(watchdogs.call_logger, "log_event", lambda *a, **kw: None):
            try:
                await asyncio.wait_for(watchdogs.standby_watchdog(session), timeout=2.0)
                assert False, "CallEndedが送出されるべき"
            except watchdogs.CallEnded as e:
                assert str(e) == "standby_timeout"

    asyncio.run(scenario())


async def _noop_goodbye(session, clip_path):
    return None


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
