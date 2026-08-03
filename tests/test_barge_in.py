import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session


class FakeOpenAiSocket:
    """OpenAiRealtimeSocketの偽物。送信内容を記録するだけで、ネットワークには
    一切触れない。"""

    def __init__(self):
        self.sent = []

    async def response_cancel(self, response_id=None):
        self.sent.append(("response_cancel", response_id))

    async def truncate_item(self, item_id, audio_end_ms, content_index=0):
        self.sent.append(("truncate_item", item_id, audio_end_ms, content_index))


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


def _make_session_with_active_response(item_id="item_abc", resp_id="resp_xyz"):
    session = call_session.CallSession()
    session.stream_sid = "MZ_test"
    session.openai = FakeOpenAiSocket()
    session._response_id = resp_id
    session._current_item_id = item_id
    session._current_response_done = False
    return session


def test_barge_in_during_real_response_cancels_clears_and_truncates():
    """指示書の完了条件: 長い応答の途中で割り込むと、cancel→clear→
    audio_playing即時False→truncateの順で実行され、audio_end_msが
    実際に送信済みの秒数（bytes_out基準）でクランプされること。"""

    async def scenario():
        session = _make_session_with_active_response()
        ws = FakeWs()

        # 1.5秒ぶん送信済み、実際に音声送信を開始してから2.0秒経過した体で計算
        # （Twilio側の再生遅延を見込んで多めに見積もる近似の確認）。
        session._audio_stats.bytes_out = int(1.5 * 8000)
        session._audio_stats.first_frame_sent_at = time.monotonic() - 2.0
        session._pending_mark_name = "resp_3"
        session._audio_stats_by_mark["resp_3"] = session._audio_stats

        await call_session._handle_barge_in(session, ws, speech_ms=550.0)

        assert ("response_cancel", "resp_xyz") in session.openai.sent
        clear_events = [m for m in ws.sent if m.get("event") == "clear"]
        assert len(clear_events) == 1

        truncate_calls = [s for s in session.openai.sent if s[0] == "truncate_item"]
        assert len(truncate_calls) == 1
        _, item_id, audio_end_ms, content_index = truncate_calls[0]
        assert item_id == "item_abc"
        assert content_index == 0
        # 経過(2000ms)ではなく送信済みbytes_out相当(1500ms)にクランプされること
        assert audio_end_ms == 1500

        assert session.ai_is_speaking is False
        assert session._pending_mark_name is None
        assert session._current_item_id is None
        assert "resp_3" not in session._audio_stats_by_mark

    asyncio.run(scenario())


def test_barge_in_skips_cancel_when_response_already_done():
    """response.done後（生成完了後）はresponse.cancelを送らない
    （指示書「response.doneなら不要」）。truncateは引き続き行う。"""

    async def scenario():
        session = _make_session_with_active_response()
        session._current_response_done = True
        ws = FakeWs()
        session._audio_stats.bytes_out = 8000
        session._audio_stats.first_frame_sent_at = time.monotonic() - 0.5

        await call_session._handle_barge_in(session, ws, speech_ms=600.0)

        assert not any(s[0] == "response_cancel" for s in session.openai.sent)
        assert any(s[0] == "truncate_item" for s in session.openai.sent)

    asyncio.run(scenario())


def test_barge_in_during_clip_playback_only_clears():
    """挨拶/縮退運転クリップ再生中のバージインでは、response.cancelも
    truncateも呼ばず、Twilioへのclearのみ行うこと（改善指示書の要件）。"""

    async def scenario():
        session = call_session.CallSession()
        session.stream_sid = "MZ_test"
        session.openai = None  # 接続確立前（挨拶クリップ再生中）を模す
        session._current_item_id = None
        session._pending_mark_name = "greeting_1"
        ws = FakeWs()

        await call_session._handle_barge_in(session, ws, speech_ms=520.0)

        clear_events = [m for m in ws.sent if m.get("event") == "clear"]
        assert len(clear_events) == 1
        assert session.ai_is_speaking is False
        assert session._pending_mark_name is None

    asyncio.run(scenario())


def test_audio_stats_mark_snapshot_survives_next_response_starting():
    """改善指示書「バージイン実装」修正4: 次の応答が既に始まっていても、
    先の応答のmarkに対応するresp_idを正しく記録できること
    （session._audio_statsを直接参照すると新しい応答のidを誤って拾うバグの
    再発防止）。"""
    session = call_session.CallSession()

    # 応答Aの完了（mark確定）を模す
    stats_a = call_session._AudioStats(resp_id="resp_A", deltas=3, bytes_in=480, bytes_out=480)
    stats_a.first_frame_sent_at = time.monotonic()
    session._audio_stats = stats_a
    session._audio_stats_by_mark["resp_3"] = stats_a

    # 応答Bが既に始まった（response.created相当）とする
    stats_b = call_session._AudioStats(resp_id="resp_B")
    session._audio_stats = stats_b

    logged = {}

    def fake_info(msg, *args):
        if msg.startswith("[AUDIO-STATS]"):
            logged["resp_id"] = args[0]
            logged["mark"] = args[1]

    old_info = call_session.logger.info
    call_session.logger.info = fake_info
    try:
        call_session._log_audio_stats(session, "resp_3")
    finally:
        call_session.logger.info = old_info

    assert logged.get("resp_id") == "resp_A", "response Bが開始済みでも先にmark確定したAのresp_idを記録すべき"
    assert logged.get("mark") == "resp_3"
    assert "resp_3" not in session._audio_stats_by_mark, "一度ログ出力したスナップショットは破棄されるべき"


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
