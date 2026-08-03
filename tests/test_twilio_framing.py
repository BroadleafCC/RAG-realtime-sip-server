import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twilio_client import FRAME_BYTES, SILENCE_BYTE, FrameAligner


def test_push_emits_full_frames_only():
    aligner = FrameAligner()
    # 160byte未満のdeltaはまだフレームにならない
    frames = aligner.push(b"\x01" * 100)
    assert frames == []
    # 追加で60byte届くと、ちょうど160byteで1フレーム確定する
    frames = aligner.push(b"\x02" * 60)
    assert len(frames) == 1
    assert len(frames[0]) == FRAME_BYTES
    assert frames[0] == b"\x01" * 100 + b"\x02" * 60


def test_push_splits_across_delta_boundaries():
    """deltaの境界とフレーム境界が一致しないケース（改善指示書2-aの核心）。
    1つのdeltaが複数フレームにまたがっても、届いたバイトが一切失われないこと。"""
    aligner = FrameAligner()
    frames = aligner.push(b"\xaa" * 250)  # 160 + 90(端数)
    assert len(frames) == 1
    assert frames[0] == b"\xaa" * 160

    frames = aligner.push(b"\xbb" * 250)  # 端数90 + 250 = 340 -> 2フレーム、端数20
    assert len(frames) == 2
    assert frames[0] == (b"\xaa" * 90 + b"\xbb" * 70)
    assert frames[1] == b"\xbb" * 160
    # 残り20byteは次回に持ち越されるはず
    tail = aligner.flush()
    assert tail is not None
    tail_frame, real_len = tail
    assert real_len == 20
    assert len(tail_frame) == FRAME_BYTES
    assert tail_frame[:20] == b"\xbb" * 20
    assert tail_frame[20:] == SILENCE_BYTE * (FRAME_BYTES - 20)


def test_flush_with_no_remainder_returns_none():
    aligner = FrameAligner()
    aligner.push(b"\x00" * FRAME_BYTES)
    assert aligner.flush() is None


def test_no_bytes_lost_across_push_and_flush():
    """bytes_in と bytes_out が一致するという完了条件の裏付け:
    任意サイズのdelta列を送っても、push()が返すフレームの実データ量 +
    flush()の実データ量の合計が、入力した総バイト数と一致すること。"""
    import random

    random.seed(0)
    aligner = FrameAligner()
    total_in = 0
    total_out = 0
    for _ in range(50):
        n = random.randint(1, 400)
        data = bytes([random.randint(0, 255) for _ in range(n)])
        total_in += len(data)
        for frame in aligner.push(data):
            total_out += len(frame)  # フル160byteフレームは端数なし

    tail = aligner.flush()
    if tail is not None:
        _, real_len = tail
        total_out += real_len

    assert total_out == total_in


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
