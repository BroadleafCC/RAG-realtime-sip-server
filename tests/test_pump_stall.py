"""pump停止（awaitハング）の検知・特定・強制復帰のテスト（2026-08-05障害）。

守りたい性質:
  1. 外部I/Oのawaitがハングしても、必ず例外に変換される（ハングは例外を出さない）
  2. pumpが止まったら、止まった位置を名指しするスタックが必ずログに出る
  3. どの経路で異常終了しても、切電とSalesforceケース作成まで必ず到達する
     （安全網はOpenAI＝故障を疑う相手に依存しない）
"""
import asyncio
import inspect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session
import config
import openai_client
import watchdogs
from openai_client import OpenAiSendTimeout


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


class FakeWs:
    def __init__(self, incoming=None, fail_send=False):
        self.sent = []
        self._incoming = list(incoming or [])
        self.fail_send = fail_send

    async def send_text(self, text):
        if self.fail_send:
            raise RuntimeError("twilio ws is dead")
        self.sent.append(json.loads(text))

    async def receive_text(self):
        if not self._incoming:
            # 何も届かない＝Twilio側の半死を模す
            await asyncio.Event().wait()
        return json.dumps(self._incoming.pop(0))


class _FakeSilero:
    def __init__(self):
        self.dropped_samples_total = 0

    def feed(self, pcm16):
        return []


def _make_session(call_sid="CA_stall"):
    """onnxモデルのロードを避けつつ、実際のCallSessionを組み立てる。"""
    with _Patch(call_session, "SileroVad", lambda max_buffer_sec=1.0: _FakeSilero()):
        session = call_session.CallSession()
    session.call_sid = call_sid
    session.stream_sid = "MZ_stall"
    session.caller_number = "09012345678"
    return session


# --- 3-1: OpenAI送信のタイムアウト化 -----------------------------------------

class _HangingWs:
    """sendが永久に返らないWebSocket（フロー制御で詰まった状態を模す）。"""

    def __init__(self):
        self.attempts = 0

    async def send(self, payload):
        self.attempts += 1
        await asyncio.Event().wait()  # 永久ハング


def test_send_hang_becomes_exception_instead_of_hanging():
    """★本丸★ 送信ハングが OpenAiSendTimeout に変換されること。

    ハングは例外を出さないのでtry/exceptでは捕らえられない。タイムアウトだけが
    ハングを例外に変換でき、それによって既存の例外処理が機能するようになる。
    """

    async def scenario():
        socket = openai_client.OpenAiRealtimeSocket()
        socket._ws = _HangingWs()
        with _Patch(config, "OPENAI_SEND_TIMEOUT_SEC", 0.05):
            try:
                await socket.append_audio("dummy_base64")
            except OpenAiSendTimeout as e:
                assert "input_audio_buffer.append" in str(e)
                return
        raise AssertionError("送信ハングがタイムアウトしていない（永久ブロックのまま）")

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_every_send_method_goes_through_the_timeout_path():
    """個別対応は必ず漏れる（実際に_say_and_wait_for_goodbyeが漏れた）。
    全送信メソッドが _send を経由し、生の _ws.send が残っていないこと。"""
    source = inspect.getsource(openai_client)
    raw_sends = [
        line for line in source.splitlines()
        if "_ws.send(" in line and "asyncio.wait_for" not in line
    ]
    # _send 内の1行（wait_forの引数として渡している行）だけが許容される
    assert len(raw_sends) == 1, f"タイムアウトを経由しない生送信が残っている: {raw_sends}"
    assert "json.dumps(payload)" in raw_sends[0]


def test_goodbye_phrase_failure_does_not_hang_the_watchdog():
    """完了条件7: OpenAIソケット死亡状態でも _say_and_wait_for_goodbye が
    ハングせず戻ること（無音・最大通話時間からの切電が完遂できる）。"""

    class _DeadSocket:
        async def send_text_turn(self, text):
            raise OpenAiSendTimeout("dead")

    async def scenario():
        session = _make_session()
        session.openai = _DeadSocket()
        await watchdogs._say_and_wait_for_goodbye(session, "さようなら")
        assert session.is_goodbye is True

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


# --- 3-2: 縮退運転（OpenAIを経由しない終了経路） -----------------------------

def test_degrade_and_end_hangs_up_and_creates_case_without_openai():
    """縮退経路がOpenAIを一切使わずに切電とケース作成まで完遂すること。"""
    sf_calls, hangups = [], []

    async def scenario():
        session = _make_session()
        session.recording_sid = "RE_x"
        session.openai = object()  # 触ったら AttributeError になる番人
        ws = FakeWs()
        with _Patch(call_session, "_clip_cache", {config.DEGRADED_AUDIO_PATH: b"\x01" * 160}), \
             _Patch(call_session.salesforce_case, "create_salesforce_case",
                    lambda *a, **kw: sf_calls.append(a) or "case_1"), \
             _Patch(call_session.twilio_client, "hangup_call", lambda sid: hangups.append(sid)), \
             _Patch(call_session.call_logger, "log_event", lambda *a, **kw: None), \
             _Patch(call_session, "_DEGRADE_CLIP_TIMEOUT_SEC", 0.05), \
             _Patch(call_session, "_DEGRADE_SETTLE_SEC", 0.0):
            session.goodbye_event.set()
            try:
                await call_session.degrade_and_end(session, ws, reason="pump_stall")
            except watchdogs.CallEnded as e:
                assert str(e) == "pump_stall"
            else:
                raise AssertionError("CallEndedが送出されていない")

        assert hangups == ["CA_stall"]
        assert len(sf_calls) == 1
        # escalation=True と recording_sid が引き渡されること
        assert sf_calls[0][3] is True
        assert sf_calls[0][4] == "RE_x"
        assert session.case_created is True
        assert any("pump_stall" in line for line in session.transcript_lines)
        # 縮退クリップがTwilioへ送られている
        assert any(m.get("event") == "media" for m in ws.sent)

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_degrade_completes_even_when_twilio_send_fails():
    """★重要★ クリップ再生に失敗しても、切電とケース作成まで必ず進むこと。
    （Twilio側が半死のケース。案件を落とさない設計思想の最後の砦）"""
    sf_calls, hangups = [], []

    async def scenario():
        session = _make_session()
        ws = FakeWs(fail_send=True)
        with _Patch(call_session, "_clip_cache", {config.DEGRADED_AUDIO_PATH: b"\x01" * 160}), \
             _Patch(call_session.salesforce_case, "create_salesforce_case",
                    lambda *a, **kw: sf_calls.append(a) or "case_1"), \
             _Patch(call_session.twilio_client, "hangup_call", lambda sid: hangups.append(sid)), \
             _Patch(call_session.call_logger, "log_event", lambda *a, **kw: None), \
             _Patch(call_session, "_DEGRADE_CLIP_TIMEOUT_SEC", 0.05), \
             _Patch(call_session, "_DEGRADE_SETTLE_SEC", 0.0):
            try:
                await call_session.degrade_and_end(session, ws, reason="twilio_recv_timeout")
            except watchdogs.CallEnded:
                pass
        assert hangups == ["CA_stall"], "クリップ失敗で切電まで到達していない"
        assert len(sf_calls) == 1, "クリップ失敗でケース作成まで到達していない"

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_degrade_is_not_started_twice():
    """pumpとwatchdogが同時に縮退へ入ってもケースを二重に作らないこと。"""
    sf_calls, hangups = [], []

    async def scenario():
        session = _make_session()
        ws = FakeWs()
        with _Patch(call_session, "_clip_cache", {config.DEGRADED_AUDIO_PATH: b""}), \
             _Patch(call_session.salesforce_case, "create_salesforce_case",
                    lambda *a, **kw: sf_calls.append(a) or "case_1"), \
             _Patch(call_session.twilio_client, "hangup_call", lambda sid: hangups.append(sid)), \
             _Patch(call_session.call_logger, "log_event", lambda *a, **kw: None), \
             _Patch(call_session, "_DEGRADE_CLIP_TIMEOUT_SEC", 0.05), \
             _Patch(call_session, "_DEGRADE_SETTLE_SEC", 0.0):
            for _ in range(2):
                try:
                    await call_session.degrade_and_end(session, ws, reason="pump_stall")
                except watchdogs.CallEnded:
                    pass
        assert len(sf_calls) == 1, "縮退運転が二重に走りケースが二重作成された"
        assert len(hangups) == 1

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


# --- 3-2: pump進捗ウォッチドッグと原因特定器 ---------------------------------

def test_pump_stall_watchdog_fires_on_frozen_progress():
    """★本丸★ フレーム処理が進まなくなったら、VAD状態に関係なく発火すること。

    無音ウォッチドッグはVAD状態がSPEAKINGに固着すると発火できない（今回の障害で
    実際に漏れた盲点）。このウォッチドッグは状態を一切見ない。
    """
    degraded = []

    async def fake_degrade(session, ws, reason):
        degraded.append(reason)
        raise watchdogs.CallEnded(reason)

    async def scenario():
        import time as _t
        session = _make_session()
        # SPEAKING固着（無音ウォッチドッグが発火できない状態）を再現
        from vad import VadState
        session.turn_detector.state = VadState.SPEAKING
        session.last_frame_processed_at = _t.monotonic() - 100
        with _Patch(config, "PUMP_STALL_SEC", 0.01), \
             _Patch(call_session, "degrade_and_end", fake_degrade):
            try:
                await watchdogs.pump_stall_watchdog(session, FakeWs())
            except watchdogs.CallEnded as e:
                assert str(e) == "pump_stall"
        assert degraded == ["pump_stall"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_pump_stall_watchdog_quiet_while_frames_progress():
    """正常通話では発火しないこと（完了条件2）。"""

    async def scenario():
        session = _make_session()
        session.last_frame_processed_at = None  # まだ1フレームも来ていない
        task = asyncio.create_task(watchdogs.pump_stall_watchdog(session, FakeWs()))
        with _Patch(config, "PUMP_STALL_SEC", 0.05):
            await asyncio.sleep(0.1)
            # フレーム処理が進んでいる状態にする
            import time as _t
            for _ in range(3):
                session.last_frame_processed_at = _t.monotonic()
                await asyncio.sleep(0.05)
        assert not task.done(), "正常進捗中に発火した"
        task.cancel()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_pump_stall_watchdog_defers_to_running_degrade():
    """既に別経路が縮退中なら割り込まない（ケース作成の中断を防ぐ）。"""

    async def scenario():
        session = _make_session()
        import time as _t
        session.last_frame_processed_at = _t.monotonic() - 100
        session.degrade_started = True
        task = asyncio.create_task(watchdogs.pump_stall_watchdog(session, FakeWs()))
        with _Patch(config, "PUMP_STALL_SEC", 0.01):
            await asyncio.sleep(0.1)
        assert not task.done(), "縮退運転中の別経路に割り込んだ"
        task.cancel()

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_stack_dump_names_the_hung_await():
    """★原因特定器★ ハングしているawaitの位置がログに出ること。

    3候補（append_audio / VAD executor / receive_text）はログからは区別
    できなかった。次にフリーズしたとき、このダンプが止まった行を名指しする。
    """
    logged = []

    async def scenario():
        async def hung_pump():
            await asyncio.Event().wait()  # ここで止まる

        session = _make_session()
        session.pump_task = asyncio.create_task(hung_pump())
        await asyncio.sleep(0.05)
        with _Patch(watchdogs.logger, "error", lambda msg, *a: logged.append(msg % a if a else msg)):
            watchdogs._dump_pump_task_stack(session)
        session.pump_task.cancel()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    dump = "\n".join(logged)
    assert "[PUMP-STALL] pump stack" in dump
    assert "hung_pump" in dump, "止まっている関数名がダンプに出ていない"


def test_stack_dump_survives_missing_task_reference():
    logged = []
    session = _make_session()
    session.pump_task = None
    with _Patch(watchdogs.logger, "error", lambda msg, *a: logged.append(msg % a if a else msg)):
        watchdogs._dump_pump_task_stack(session)
    assert any("pump_task参照がありません" in line for line in logged)


# --- 3-3 / 3-4: pump内のタイムアウト ------------------------------------------

def test_twilio_receive_timeout_ends_the_call():
    """Twilioからフレームが届かなくなったら切断扱いにすること
    （発信者を無限の沈黙に置かない）。"""

    async def scenario():
        session = _make_session()
        with _Patch(config, "TWILIO_RECV_TIMEOUT_SEC", 0.05):
            try:
                await call_session.pump_twilio_to_openai(session, FakeWs())
            except watchdogs.CallEnded as e:
                assert str(e) == "twilio_recv_timeout"
                return
        raise AssertionError("受信途絶で通話が終わらない（ハングしたまま）")

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_append_audio_timeout_triggers_degrade():
    """OpenAI送信タイムアウトを握りつぶさず、縮退運転に接続すること。"""
    degraded = []

    class _DeadOpenAi:
        async def append_audio(self, payload_b64):
            raise OpenAiSendTimeout("backpressure")

    async def fake_degrade(session, ws, reason):
        degraded.append(reason)
        raise watchdogs.CallEnded(reason)

    async def scenario():
        session = _make_session()
        session.openai = _DeadOpenAi()
        session._openai_ready.set()
        ws = FakeWs([{"event": "media", "media": {"payload": "AAAA"}}])
        with _Patch(call_session, "degrade_and_end", fake_degrade):
            try:
                await call_session.pump_twilio_to_openai(session, ws)
            except watchdogs.CallEnded as e:
                assert str(e) == "openai_send_timeout"
        assert degraded == ["openai_send_timeout"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_vad_feed_timeout_skips_frame_and_counts_streak():
    """推論が返らないフレームはスキップして通話を継続し、連続回数を数えること。"""

    class _HangingSilero:
        dropped_samples_total = 0

        def feed(self, pcm16):
            import time as _t
            _t.sleep(0.3)
            return [(0.9, 0.032)]

    async def scenario():
        session = _make_session()
        session.silero = _HangingSilero()
        with _Patch(config, "VAD_INFERENCE_IN_THREAD", True), \
             _Patch(config, "VAD_FEED_TIMEOUT_SEC", 0.05):
            assert await call_session._vad_feed(session, b"\x00" * 320) == []
            assert session._vad_timeout_streak == 1
            assert await call_session._vad_feed(session, b"\x00" * 320) == []
            assert session._vad_timeout_streak == 2

    asyncio.run(asyncio.wait_for(scenario(), timeout=10))


def test_vad_feed_success_resets_streak():
    async def scenario():
        session = _make_session()
        session._vad_timeout_streak = 2
        with _Patch(config, "VAD_INFERENCE_IN_THREAD", True), \
             _Patch(config, "VAD_FEED_TIMEOUT_SEC", 1.0):
            await call_session._vad_feed(session, b"\x00" * 320)
        assert session._vad_timeout_streak == 0

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_frame_progress_is_recorded_after_each_media_frame():
    """進捗タイムスタンプがmediaフレーム処理ごとに更新されること
    （pump_stall_watchdogが見ている唯一の値）。"""

    async def scenario():
        session = _make_session()
        assert session.last_frame_processed_at is None
        ws = FakeWs([
            {"event": "media", "media": {"payload": "AAAA"}},
            {"event": "stop"},
        ])
        try:
            await call_session.pump_twilio_to_openai(session, ws)
        except watchdogs.CallEnded:
            pass
        assert session.last_frame_processed_at is not None

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_degrade_waits_for_clip_playback_when_mark_cannot_return():
    """pump自身から縮退した場合、markは絶対に返らない（markを処理するのがpump
    だから）。それでもクリップの実再生時間ぶんは待ってから切電すること
    （即切電するとTwilioのバッファごと捨てられ、発信者には何も聞こえない）。"""
    # 1秒ぶんのμ-lawクリップ = 8000バイト
    one_sec_clip = b"\x01" * 8000
    with _Patch(call_session, "_clip_cache", {config.DEGRADED_AUDIO_PATH: one_sec_clip}):
        wait_sec = call_session._degrade_wait_sec(config.DEGRADED_AUDIO_PATH)
    assert 1.0 < wait_sec <= call_session._DEGRADE_CLIP_TIMEOUT_SEC
    # クリップが長すぎても上限で頭打ちにする
    with _Patch(call_session, "_clip_cache", {config.DEGRADED_AUDIO_PATH: b"\x01" * 800000}):
        assert call_session._degrade_wait_sec(config.DEGRADED_AUDIO_PATH) == \
            call_session._DEGRADE_CLIP_TIMEOUT_SEC


# --- 切断分類との整合 ---------------------------------------------------------

def test_new_end_reasons_are_classified_as_server_initiated():
    """新しい終了経路を発信側の切断と誤分類しないこと。"""
    for reason in ("pump_stall", "openai_send_timeout", "twilio_recv_timeout",
                   "vad_executor_stall"):
        session = _make_session()
        session.end_reason = reason
        category, text = call_session.classify_disconnect(session)
        assert category == "SERVER_INITIATED_HANGUP", reason
        assert reason in text


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
