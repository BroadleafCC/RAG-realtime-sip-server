"""VAD推論のイベントループブロック対策（2026-08-05障害）のテスト。

守りたい性質:
  ①推論中もイベントループが回り続ける（ウォッチドッグが凍らない）
  ②入力が消費に追いつかなくてもVADバッファが無限に膨らまない
  ③ループが固まったら、ループ外の監視がそれを検知できる
"""
import asyncio
import inspect
import os
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session
import config
import loop_heartbeat
import vad_model


class _Patch:
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


# --- 対策①: 推論をイベントループから追い出す --------------------------------

class _FakeSilero:
    """feedが呼ばれたスレッドを記録し、任意の時間だけブロックする偽VAD。"""

    def __init__(self, block_sec=0.0):
        self.block_sec = block_sec
        self.threads = []
        self.dropped_samples_total = 0

    def feed(self, pcm16):
        self.threads.append(threading.get_ident())
        if self.block_sec:
            time.sleep(self.block_sec)
        return [(0.9, 0.032)]


def _session_with_fake_vad(block_sec=0.0):
    session = call_session.CallSession.__new__(call_session.CallSession)
    session.silero = _FakeSilero(block_sec)
    session.call_sid = "CA_vad"
    session._vad_drop_logged_total = 0
    session._vad_drop_logged_at = 0.0
    return session


def test_vad_inference_runs_off_the_event_loop_thread():
    """推論が専用Executorのワーカースレッドで実行されること。"""

    async def scenario():
        session = _session_with_fake_vad()
        with _Patch(config, "VAD_INFERENCE_IN_THREAD", True):
            results = await call_session._vad_feed(session, b"\x00" * 320)
        assert results == [(0.9, 0.032)]
        assert session.silero.threads, "feedが呼ばれていない"
        assert session.silero.threads[0] != threading.get_ident(), \
            "推論がイベントループのスレッドで実行されている（ブロックの原因）"

    asyncio.run(scenario())


def test_event_loop_keeps_running_during_slow_inference():
    """★本丸★ 推論が長引いてもイベントループが回り続けること。

    今回の障害では、推論がループを塞いだせいで無音タイマー・全ウォッチドッグの
    `asyncio.sleep` が復帰できず、発話終了検知も切電も止まった。ここでは
    ウォッチドッグ相当の並行タスクが、遅い推論中もカウントを進められることを見る。
    """
    ticks = {"n": 0}

    async def watchdog_like():
        while True:
            await asyncio.sleep(0.02)
            ticks["n"] += 1

    async def scenario():
        session = _session_with_fake_vad(block_sec=0.4)
        task = asyncio.create_task(watchdog_like())
        with _Patch(config, "VAD_INFERENCE_IN_THREAD", True):
            await call_session._vad_feed(session, b"\x00" * 320)
        task.cancel()

    asyncio.run(scenario())
    # 0.4秒の推論中に20ms間隔のタスクが何度も回れていれば、ループは生きている
    assert ticks["n"] >= 5, f"推論中にイベントループが止まっている (ticks={ticks['n']})"


def test_event_loop_is_blocked_when_thread_offload_disabled():
    """対策OFF時は実際にループが止まる（＝この対策が効いている証拠の対照実験）。"""
    ticks = {"n": 0}

    async def watchdog_like():
        while True:
            await asyncio.sleep(0.02)
            ticks["n"] += 1

    async def scenario():
        session = _session_with_fake_vad(block_sec=0.4)
        task = asyncio.create_task(watchdog_like())
        await asyncio.sleep(0.05)  # ウォッチドッグを立ち上げる
        ticks["n"] = 0
        with _Patch(config, "VAD_INFERENCE_IN_THREAD", False):
            await call_session._vad_feed(session, b"\x00" * 320)
        task.cancel()

    asyncio.run(scenario())
    assert ticks["n"] == 0, "OFFなのにループがブロックされていない（テストの前提が崩れている）"


def test_vad_feed_is_sequential_per_session():
    """SileroVadはスレッドセーフでない。同一セッションのfeedが重ならないこと。"""
    overlap = {"max": 0, "cur": 0}
    lock = threading.Lock()

    class _OverlapCheckingSilero(_FakeSilero):
        def feed(self, pcm16):
            with lock:
                overlap["cur"] += 1
                overlap["max"] = max(overlap["max"], overlap["cur"])
            time.sleep(0.05)
            with lock:
                overlap["cur"] -= 1
            return []

    async def scenario():
        session = _session_with_fake_vad()
        session.silero = _OverlapCheckingSilero()
        with _Patch(config, "VAD_INFERENCE_IN_THREAD", True):
            # pump_twilio_to_openai と同じく逐次awaitする
            for _ in range(4):
                await call_session._vad_feed(session, b"\x00" * 320)

    asyncio.run(scenario())
    assert overlap["max"] == 1, "同一セッションのfeedが並行実行された（状態が壊れる）"


# --- 対策②: VADバッファの上限 ------------------------------------------------

class _FakeModel:
    def __call__(self, chunk, sample_rate):
        class _R:
            @staticmethod
            def item():
                return 0.5
        return _R()

    def reset_states(self):
        pass


def _make_vad(max_buffer_sec=1.0):
    with _Patch(vad_model, "load_silero_vad", lambda onnx=True: _FakeModel()):
        return vad_model.SileroVad(max_buffer_sec=max_buffer_sec)


def _silence_pcm16(n_samples: int) -> bytes:
    return np.zeros(n_samples, dtype=np.int16).tobytes()


def test_normal_frames_never_drop():
    """通常の20msフレーム（160サンプル）が続く限り破棄は起きない
    （完了条件6: 平常時に[VAD-DROP]が出ないこと）。"""
    vad = _make_vad(max_buffer_sec=1.0)
    for _ in range(200):  # 4秒ぶん
        vad.feed(_silence_pcm16(160))
    assert vad.dropped_samples_total == 0
    assert len(vad._buffer) < vad_model.SAMPLE_RATE  # 溜まっていない


def test_buffer_is_capped_when_input_outpaces_consumption():
    """消費が追いつかず一気に流れ込んでも、バッファは上限で頭打ちになる。"""
    vad = _make_vad(max_buffer_sec=1.0)
    vad.feed(_silence_pcm16(3 * vad_model.SAMPLE_RATE))  # 3秒ぶんを一度に投入
    # 3秒 - 上限1秒 = 2秒ぶん(16000サンプル)が古い側から捨てられる
    assert vad.dropped_samples_total == 2 * vad_model.SAMPLE_RATE
    assert len(vad._buffer) < 256  # 上限ぶんは推論して消費し切っている


def test_buffer_does_not_grow_unboundedly_over_repeated_bursts():
    """バーストが続いてもバッファが増え続けない（暴走の螺旋を断つ）。"""
    vad = _make_vad(max_buffer_sec=1.0)
    sizes = []
    for _ in range(10):
        vad.feed(_silence_pcm16(2 * vad_model.SAMPLE_RATE))
        sizes.append(len(vad._buffer))
    assert max(sizes) <= vad_model.SAMPLE_RATE
    assert vad.dropped_samples_total > 0


def test_reset_clears_buffer_but_keeps_drop_counter():
    vad = _make_vad(max_buffer_sec=1.0)
    vad.feed(_silence_pcm16(3 * vad_model.SAMPLE_RATE))
    dropped = vad.dropped_samples_total
    vad.reset()
    assert len(vad._buffer) == 0
    assert vad.dropped_samples_total == dropped


def test_vad_drop_log_is_throttled():
    """破棄ログは間引かれる（毎フレーム出すとログ洪水になる）。"""
    logged = []
    session = _session_with_fake_vad()

    with _Patch(call_session.logger, "warning", lambda msg, *a: logged.append(msg % a)):
        session.silero.dropped_samples_total = 8000
        call_session._maybe_log_vad_drop(session)
        session.silero.dropped_samples_total = 16000
        call_session._maybe_log_vad_drop(session)  # 直後なので間引かれる

    assert len(logged) == 1
    assert "[VAD-DROP]" in logged[0]


def test_no_vad_drop_log_when_nothing_dropped():
    logged = []
    session = _session_with_fake_vad()
    with _Patch(call_session.logger, "warning", lambda msg, *a: logged.append(msg)):
        call_session._maybe_log_vad_drop(session)
    assert logged == []


# --- 対策③: ループ・ハートビート監視 -----------------------------------------

def test_no_stall_declared_while_beats_are_fresh():
    loop_heartbeat._set_active(True)
    try:
        loop_heartbeat._beat()
        assert loop_heartbeat._should_declare_stall(15.0) is None
    finally:
        loop_heartbeat._set_active(False)


def test_stall_declared_when_beats_stop():
    loop_heartbeat._set_active(True)
    try:
        with loop_heartbeat._lock:
            loop_heartbeat._last_beat_monotonic = time.monotonic() - 20.0
        age = loop_heartbeat._should_declare_stall(15.0)
        assert age is not None and age >= 15.0
    finally:
        loop_heartbeat._set_active(False)


def test_never_declares_stall_while_inactive():
    """起動前・シャットダウン後は誤発火しない。プロセスを落とす仕組みなので、
    誤発火しないことが最優先（進行中の他通話を巻き添えにするため）。"""
    loop_heartbeat._set_active(False)
    with loop_heartbeat._lock:
        loop_heartbeat._last_beat_monotonic = time.monotonic() - 999.0
    assert loop_heartbeat._should_declare_stall(15.0) is None


def test_activation_resets_the_beat_clock():
    """有効化した瞬間が起点になる（起動に時間がかかっても誤発火しない）。"""
    with loop_heartbeat._lock:
        loop_heartbeat._last_beat_monotonic = time.monotonic() - 999.0
    loop_heartbeat._set_active(True)
    try:
        assert loop_heartbeat._should_declare_stall(15.0) is None
    finally:
        loop_heartbeat._set_active(False)


def test_heartbeat_loop_beats_and_deactivates_on_cancel():
    async def scenario():
        with loop_heartbeat._lock:
            loop_heartbeat._last_beat_monotonic = time.monotonic() - 999.0
        task = asyncio.create_task(loop_heartbeat.heartbeat_loop(interval_sec=0.01))
        await asyncio.sleep(0.05)
        assert loop_heartbeat._monitoring_active is True
        assert loop_heartbeat._should_declare_stall(0.5) is None  # ビートが更新されている
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # シャットダウン後は監視が無効化され、誤発火しない
        assert loop_heartbeat._monitoring_active is False

    asyncio.run(scenario())


def test_start_monitor_does_not_spawn_duplicate_threads():
    before = getattr(loop_heartbeat, "_monitor_thread", None)
    try:
        loop_heartbeat.start_monitor(15.0)
        first = loop_heartbeat._monitor_thread
        loop_heartbeat.start_monitor(15.0)
        assert loop_heartbeat._monitor_thread is first
    finally:
        loop_heartbeat._set_active(False)
        loop_heartbeat._monitor_thread = before


if __name__ == "__main__":
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
