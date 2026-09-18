"""実モデル(assets/models/smart-turn-v3.2-cpu.onnx)を使ったスモークテスト。

モデルファイルが存在しない環境（CI等）では自動的にスキップする。
判定精度そのものは検証しない（実通話サンプルでの検証が必要、指示書6章）。
ここでは「例外なく実行でき、確率が[0,1]に収まる」ことだけを確認する。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "models", "smart-turn-v3.2-cpu.onnx")


def _pcm16_bytes(num_samples: int) -> bytes:
    rng = np.random.default_rng(0)
    return rng.integers(-3000, 3000, size=num_samples, dtype="int16").tobytes()


def test_real_model_predict_returns_valid_probability():
    if not os.path.exists(MODEL_PATH):
        print(f"SKIP: モデルファイルが見つかりません ({MODEL_PATH})")
        return
    from smart_turn_model import SmartTurnDetector

    detector = SmartTurnDetector(MODEL_PATH)
    result = detector.predict(_pcm16_bytes(8000 * 2))  # 8kHzで2秒分
    assert 0.0 <= result.probability <= 1.0
    assert result.prediction in (0, 1)


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
