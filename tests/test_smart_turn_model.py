import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from smart_turn_model import SmartTurnDetector, _truncate_or_pad


class _FakeSession:
    """onnxruntime.InferenceSessionの代わりに注入する。run()に渡された
    input_featuresの形状を記録しつつ、固定のprobabilityを返す。"""

    def __init__(self, probability: float):
        self.probability = probability
        self.last_input_features = None

    def run(self, output_names, feed_dict):
        self.last_input_features = feed_dict["input_features"]
        return [np.array([[self.probability]], dtype=np.float32)]


def _pcm16_bytes(num_samples: int, value: int = 0) -> bytes:
    return (np.full(num_samples, value, dtype="<i2")).tobytes()


def test_truncate_or_pad_pads_short_audio_with_leading_zeros():
    audio = np.ones(100, dtype=np.float32)
    out = _truncate_or_pad(audio, 128000)
    assert len(out) == 128000
    assert np.all(out[:127900] == 0.0)
    assert np.all(out[127900:] == 1.0)


def test_truncate_or_pad_keeps_tail_of_long_audio():
    audio = np.arange(200000, dtype=np.float32)
    out = _truncate_or_pad(audio, 128000)
    assert len(out) == 128000
    assert out[0] == 72000  # 200000 - 128000
    assert out[-1] == 199999


def test_truncate_or_pad_exact_length_is_unchanged():
    audio = np.ones(128000, dtype=np.float32)
    out = _truncate_or_pad(audio, 128000)
    assert out is audio


def test_predict_resamples_8k_to_16k_and_shapes_input_features():
    fake = _FakeSession(probability=0.83)
    detector = SmartTurnDetector(session=fake)
    # 1秒分の8kHz無音(8000サンプル) -> 16kHzへリサンプルされ、8秒へゼロパディングされるはず
    pcm16_1sec_8k = _pcm16_bytes(8000)

    result = detector.predict(pcm16_1sec_8k)

    assert fake.last_input_features.shape == (1, 80, 800)
    assert abs(result.probability - 0.83) < 1e-6  # float32往復による誤差を許容
    assert result.prediction == 1


def test_predict_prediction_is_zero_when_probability_at_or_below_half():
    fake = _FakeSession(probability=0.4)
    detector = SmartTurnDetector(session=fake)
    result = detector.predict(_pcm16_bytes(8000))
    assert result.prediction == 0
    assert abs(result.probability - 0.4) < 1e-6


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
