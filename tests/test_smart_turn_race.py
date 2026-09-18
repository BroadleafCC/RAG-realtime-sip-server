import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("RECORDING_LINK_SECRET", "test-secret-for-local-verification")

import call_session
import config
import smart_turn_model
from smart_turn_model import SmartTurnResult
from vad import VadState


class FakeOpenAiSocket:
    def __init__(self):
        self.sent = []

    async def commit(self):
        self.sent.append(("commit", None))

    async def response_create(self):
        self.sent.append(("response_create", None))


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class _Patch:
    """属性を一時的に差し替えるヘルパー（tests/test_call_session_prewarm.pyと同じパターン）。"""

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


class _FakeDetector:
    def __init__(self, probability: float, delay: float = 0.0):
        self.probability = probability
        self.delay = delay
        self.calls = 0

    def predict(self, pcm16_bytes: bytes) -> SmartTurnResult:
        self.calls += 1
        if self.delay:
            import time as _time
            _time.sleep(self.delay)
        prediction = 1 if self.probability > 0.5 else 0
        return SmartTurnResult(prediction=prediction, probability=self.probability)


def _speaking_session() -> call_session.CallSession:
    """turn_detectorがSPEAKING状態、openaiがFakeでopenai_ready済みのsessionを作る。"""
    session = call_session.CallSession()
    session.openai = FakeOpenAiSocket()
    session._openai_ready.set()
    # IDLE -> SPEAKINGへ実際に発話を流して遷移させる（force_idle等の裏道を使わない）。
    for _ in range(20):
        t = session.turn_detector.update(0.9, 32.0, False)
    assert session.turn_detector.state == VadState.SPEAKING
    return session


# --- _is_smart_turn_result_stale: 純粋関数の決定的テスト ---

def test_stale_detects_generation_mismatch():
    session = _speaking_session()
    gen = session._speech_turn_generation
    session._speech_turn_generation += 1  # 別のイベントが世代を進めた体
    assert call_session._is_smart_turn_result_stale(session, gen) is True


def test_stale_detects_state_change_even_if_generation_matches():
    session = _speaking_session()
    gen = session._speech_turn_generation
    session.turn_detector.force_end_of_speech()  # 自然発火等でstateが変わった体
    assert call_session._is_smart_turn_result_stale(session, gen) is True


def test_not_stale_when_generation_and_state_both_unchanged():
    session = _speaking_session()
    gen = session._speech_turn_generation
    assert call_session._is_smart_turn_result_stale(session, gen) is False


# --- _maybe_kick_smart_turn: トリガーのエッジ検出 ---

def test_maybe_kick_smart_turn_fires_only_once_per_silence_run():
    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.9)
        with _Patch(smart_turn_model, "_detector", fake), \
             _Patch(config, "SMART_TURN_TRIGGER_SILENCE_MS", 200):
            ws = FakeWs()
            call_session._maybe_kick_smart_turn(session, ws, elapsed_ms=250.0)
            first_task = session._smart_turn_task
            assert first_task is not None
            # 同じ無音区間内でさらにelapsed_msが増えても再トリガーしない
            call_session._maybe_kick_smart_turn(session, ws, elapsed_ms=300.0)
            assert session._smart_turn_task is first_task
            await first_task
        assert fake.calls == 1

    asyncio.run(scenario())


def test_maybe_kick_smart_turn_does_not_fire_below_trigger_threshold():
    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.9)
        with _Patch(smart_turn_model, "_detector", fake), \
             _Patch(config, "SMART_TURN_TRIGGER_SILENCE_MS", 200):
            ws = FakeWs()
            call_session._maybe_kick_smart_turn(session, ws, elapsed_ms=100.0)
            assert session._smart_turn_task is None
        assert fake.calls == 0

    asyncio.run(scenario())


def test_maybe_kick_smart_turn_rearms_when_speech_resumes():
    """無音区間中にelapsed_msが減少(発話再開)したら世代を進めて再トリガー
    可能にする（vad.py自体はこのケースでイベントを出さないための自前検出）。"""

    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.9)
        with _Patch(smart_turn_model, "_detector", fake), \
             _Patch(config, "SMART_TURN_TRIGGER_SILENCE_MS", 200):
            ws = FakeWs()
            call_session._maybe_kick_smart_turn(session, ws, elapsed_ms=250.0)
            first_gen = session._speech_turn_generation
            await session._smart_turn_task

            # 発話が再開してelapsed_msが0に戻った(vad.pyはイベントを出さない)
            call_session._maybe_kick_smart_turn(session, ws, elapsed_ms=0.0)
            assert session._speech_turn_generation == first_gen + 1
            assert session._smart_turn_infer_fired is False

            # 再び無音がしきい値に達したら2回目のトリガーが起きる
            call_session._maybe_kick_smart_turn(session, ws, elapsed_ms=250.0)
            await session._smart_turn_task
        assert fake.calls == 2

    asyncio.run(scenario())


# --- _run_smart_turn_and_maybe_fire: レース制御と発火経路の統合テスト ---

def test_early_fire_when_complete_and_not_shadow():
    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.9)
        gen = session._speech_turn_generation
        ws = FakeWs()
        with _Patch(config, "SMART_TURN_SHADOW_MODE", False), \
             _Patch(config, "SMART_TURN_COMPLETE_THRESHOLD", 0.7), \
             _Patch(config, "SMART_TURN_INFER_TIMEOUT_MS", 200):
            await call_session._run_smart_turn_and_maybe_fire(
                session, ws, fake, gen, b"\x00\x00" * 100, elapsed_at_trigger=250.0,
            )
        assert session.turn_detector.state == VadState.AWAITING_RESPONSE
        assert ("commit", None) in session.openai.sent
        assert ("response_create", None) in session.openai.sent

    asyncio.run(scenario())


def test_shadow_mode_logs_only_and_does_not_fire():
    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.9)
        gen = session._speech_turn_generation
        ws = FakeWs()
        with _Patch(config, "SMART_TURN_SHADOW_MODE", True), \
             _Patch(config, "SMART_TURN_COMPLETE_THRESHOLD", 0.7):
            await call_session._run_smart_turn_and_maybe_fire(
                session, ws, fake, gen, b"\x00\x00" * 100, elapsed_at_trigger=250.0,
            )
        # シャドーモードでは状態機械にも接続にも一切影響してはならない
        assert session.turn_detector.state == VadState.SPEAKING
        assert session.openai.sent == []

    asyncio.run(scenario())


def test_stale_result_is_ignored_even_if_complete():
    """推論完了までの間に自然閾値到達等で既にAWAITING_RESPONSEへ遷移して
    いた場合、古い結果は無視され、二重にcommitされないこと。"""

    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.95)
        gen = session._speech_turn_generation
        ws = FakeWs()

        # 推論完了を待つ間に自然発火が先に起きた体（generationも進む）
        session.turn_detector.force_end_of_speech()
        session._speech_turn_generation += 1
        await call_session._fire_end_of_speech(session, ws, source="vad_timeout")
        sent_before = list(session.openai.sent)

        with _Patch(config, "SMART_TURN_SHADOW_MODE", False), \
             _Patch(config, "SMART_TURN_COMPLETE_THRESHOLD", 0.7):
            await call_session._run_smart_turn_and_maybe_fire(
                session, ws, fake, gen, b"\x00\x00" * 100, elapsed_at_trigger=250.0,
            )

        # 古い結果によって新たにcommit/response_createが追加されていないこと
        assert session.openai.sent == sent_before

    asyncio.run(scenario())


def test_incomplete_decision_does_not_fire():
    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.2)
        gen = session._speech_turn_generation
        ws = FakeWs()
        with _Patch(config, "SMART_TURN_SHADOW_MODE", False), \
             _Patch(config, "SMART_TURN_COMPLETE_THRESHOLD", 0.7):
            await call_session._run_smart_turn_and_maybe_fire(
                session, ws, fake, gen, b"\x00\x00" * 100, elapsed_at_trigger=250.0,
            )
        assert session.turn_detector.state == VadState.SPEAKING
        assert session.openai.sent == []

    asyncio.run(scenario())


def test_inference_timeout_falls_back_without_firing():
    async def scenario():
        session = _speaking_session()
        fake = _FakeDetector(probability=0.95, delay=0.5)  # SMART_TURN_INFER_TIMEOUT_MSより長く待たせる
        gen = session._speech_turn_generation
        ws = FakeWs()
        with _Patch(config, "SMART_TURN_SHADOW_MODE", False), \
             _Patch(config, "SMART_TURN_INFER_TIMEOUT_MS", 50):
            await call_session._run_smart_turn_and_maybe_fire(
                session, ws, fake, gen, b"\x00\x00" * 100, elapsed_at_trigger=250.0,
            )
        assert session.turn_detector.state == VadState.SPEAKING
        assert session.openai.sent == []

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
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)
