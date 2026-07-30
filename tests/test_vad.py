import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vad import TurnDetector, VadEvent, VadState

FRAME_MS = 32.0  # 256サンプル@8kHzの1チャンク分の実時間


def feed(detector, prob, count, ai_is_speaking=False):
    """同じ確率を count 回連続で流し、最後のtransitionを返す"""
    t = None
    for _ in range(count):
        t = detector.update(prob, FRAME_MS, ai_is_speaking)
    return t


def test_idle_to_speaking_requires_sustained_speech():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    # 250ms未満の発話は無視される（250/32 = 7.8なので7回では届かない）
    t = feed(d, 0.9, 7)
    assert d.state == VadState.IDLE
    assert t.event is None
    # 8回目（256ms）で閾値到達
    t = d.update(0.9, FRAME_MS, False)
    assert t.event == VadEvent.SPEECH_STARTED
    assert d.state == VadState.SPEAKING


def test_idle_resets_on_short_noise():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    feed(d, 0.9, 5)  # 160ms、まだSPEAKINGになっていない
    assert d.state == VadState.IDLE
    d.update(0.1, FRAME_MS, False)  # 無音が挟まるとカウンタがリセットされる
    t = feed(d, 0.9, 5)
    assert d.state == VadState.IDLE  # リセットされたので再度5回では届かない
    assert t.event is None


def test_speaking_to_awaiting_response_on_sustained_silence():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    feed(d, 0.9, 8, )
    assert d.state == VadState.SPEAKING
    # 650ms未満の無音では終了しない (650/32 = 20.3)
    t = feed(d, 0.1, 20)
    assert d.state == VadState.SPEAKING
    assert t.event is None
    t = d.update(0.1, FRAME_MS, False)
    assert t.event == VadEvent.END_OF_SPEECH
    assert d.state == VadState.AWAITING_RESPONSE


def test_awaiting_response_never_transitions_on_probability():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    feed(d, 0.9, 8)
    feed(d, 0.1, 21)
    assert d.state == VadState.AWAITING_RESPONSE
    # どれだけ確率が動いてもAWAITING_RESPONSEのままであるべき
    for prob in (0.9, 0.1, 0.99, 0.0):
        t = feed(d, prob, 30)
        assert d.state == VadState.AWAITING_RESPONSE
        assert t.event is None


def test_force_idle_is_the_only_exit_from_awaiting_response():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    feed(d, 0.9, 8)
    feed(d, 0.1, 21)
    assert d.state == VadState.AWAITING_RESPONSE
    d.force_idle()
    assert d.state == VadState.IDLE


def test_barge_in_requires_sustained_speech_during_ai_turn():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    # 500ms未満 (500/32 = 15.6) の短い雑音では発火しない
    t = feed(d, 0.9, 15, ai_is_speaking=True)
    assert t.event is None
    assert d.state == VadState.IDLE
    # 途切れると barge-in カウンタもリセットされる
    d.update(0.1, FRAME_MS, True)
    t = feed(d, 0.9, 15, ai_is_speaking=True)
    assert t.event is None


def test_barge_in_fires_once_per_ai_turn():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    t = feed(d, 0.9, 16, ai_is_speaking=True)  # 512ms分、閾値超え
    assert t.event == VadEvent.BARGE_IN
    assert d.state == VadState.SPEAKING
    # 同じAIターン中は連続して発話していても再発火しない
    t = feed(d, 0.9, 20, ai_is_speaking=True)
    assert t.event is None
    # AIターンが切り替わる（ai_is_speaking False -> True）と再度武装される
    d.update(0.9, FRAME_MS, False)
    t = feed(d, 0.9, 16, ai_is_speaking=True)
    assert t.event == VadEvent.BARGE_IN


def test_barge_in_never_fires_while_ai_silent():
    d = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    t = feed(d, 0.9, 100, ai_is_speaking=False)
    # 長時間発話しても出るのは SPEECH_STARTED のみ、BARGE_IN は出ない
    assert d.state == VadState.SPEAKING
    seen_events = set()
    d2 = TurnDetector(threshold=0.5, speech_start_ms=250, speech_end_ms=650, barge_in_min_ms=500)
    for _ in range(100):
        t = d2.update(0.9, FRAME_MS, False)
        if t.event:
            seen_events.add(t.event)
    assert VadEvent.BARGE_IN not in seen_events


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
