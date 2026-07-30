import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from audio_convert import pcm16_to_float32, ulaw_to_pcm16


def test_ulaw_silence_decodes_near_zero():
    # mu-lawの無音バイトは0xFF（既存main.pyのbarge_in等のテストでも使われる値）
    mulaw_silence = bytes([0xFF]) * 160
    pcm16 = ulaw_to_pcm16(mulaw_silence)
    assert len(pcm16) == 160 * 2  # 8bit mu-law -> 16bit PCM で倍のバイト数
    samples = np.frombuffer(pcm16, dtype="<i2")
    assert np.all(np.abs(samples) < 50)  # ほぼ無音


def test_pcm16_to_float32_range_and_length():
    ints = np.array([0, 32767, -32768, 16384], dtype="<i2")
    floats = pcm16_to_float32(ints.tobytes())
    assert len(floats) == 4
    assert floats.dtype == np.float32
    assert -1.0 <= floats.min()
    assert floats.max() <= 1.0
    assert abs(floats[1] - 1.0) < 0.001
    assert abs(floats[2] - (-1.0)) < 0.001


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
