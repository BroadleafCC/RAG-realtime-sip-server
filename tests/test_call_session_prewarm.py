import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import call_session
import config


class FakeOpenAiSocket:
    """OpenAiRealtimeSocketの偽物。ネットワークに一切触れず、送信内容を
    記録し、テスト側からイベントを注入できるようにする。"""

    def __init__(self):
        self.sent = []
        self._events = asyncio.Queue()

    async def connect(self):
        return self

    async def send_session_update(self, instructions):
        self.sent.append(("session.update", instructions))

    async def inject_greeting_said(self):
        self.sent.append(("greeting_said", None))

    async def append_audio(self, payload_b64):
        self.sent.append(("append_audio", payload_b64))

    async def commit(self):
        self.sent.append(("commit", None))

    async def response_create(self):
        self.sent.append(("response_create", None))

    async def response_cancel(self, response_id=None):
        self.sent.append(("response_cancel", response_id))

    async def close(self):
        pass

    async def recv_events(self):
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def push_session_updated(self):
        self._events.put_nowait({"type": "session.created", "session": {}})
        self._events.put_nowait({"type": "session.updated", "session": {}})


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class _Patch:
    """属性を一時的に差し替えるヘルパー。pytestのmonkeypatchフィクスチャは
    使わない（このファイルは`python tests/test_x.py`単体実行とも互換性を
    保つため、他のテストファイルと同じくフィクスチャ非依存にしている）。"""

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


def test_queued_audio_flushes_in_order_and_deferred_commit_fires():
    """start受信直後〜OpenAI接続完了までにキューされた音声フレームが順序
    通りにflushされ、その間に検知したEND_OF_SPEECHのcommitがflush直後に
    発火すること（改善指示書「挨拶即時再生」3章の中核）。"""
    fake_socket = FakeOpenAiSocket()

    async def scenario():
        with _Patch(call_session, "OpenAiRealtimeSocket", lambda: fake_socket), \
             _Patch(call_session.twilio_client, "start_recording", lambda call_sid: None):
            session = call_session.CallSession()
            session.call_sid = "CA_test1"
            session.stream_sid = "MZ_test1"
            ws = FakeWs()

            call_session.prewarm_openai_connection(session.call_sid)

            # start受信〜接続完了までに届いた音声はローカルキューに積まれる
            session._pending_audio_queue.extend(["frame1", "frame2", "frame3"])
            # その間に発話終了(END_OF_SPEECH)を検知したケースを模す
            session._deferred_end_of_speech = True

            fake_socket.push_session_updated()

            await call_session.openai_connection_task(session, ws, start_received_at=time.monotonic())

            assert session._openai_ready.is_set()
            assert session.openai is fake_socket
            assert session._pending_audio_queue == []

            append_calls = [s[1] for s in fake_socket.sent if s[0] == "append_audio"]
            assert append_calls == ["frame1", "frame2", "frame3"]

            assert ("commit", None) in fake_socket.sent
            assert ("response_create", None) in fake_socket.sent
            assert session.response_deadline is not None
            assert session._deferred_end_of_speech is False

    asyncio.run(scenario())


def test_connection_failure_triggers_degraded_flow():
    """OpenAI接続が失敗した場合、案内クリップ再生→切電→要折り返し
    (escalation=True)ケース作成→CallEnded送出、という縮退運転が動くこと
    （改善指示書「挨拶即時再生」3章）。"""
    async def failing_connect(call_sid):
        raise ConnectionError("boom")

    sf_calls = []

    def fake_create_case(transcript_lines, call_id, phone_number, escalation):
        sf_calls.append((call_id, phone_number, escalation))
        return "case123"

    hangup_calls = []

    async def scenario():
        with _Patch(call_session, "_connect_openai_for_call", failing_connect), \
             _Patch(call_session.salesforce_case, "create_salesforce_case", fake_create_case), \
             _Patch(call_session.twilio_client, "hangup_call", lambda call_sid: hangup_calls.append(call_sid)), \
             _Patch(call_session.twilio_client, "start_recording", lambda call_sid: None), \
             _Patch(call_session, "_clip_cache", {config.DEGRADED_AUDIO_PATH: b"\x01" * 160}):
            session = call_session.CallSession()
            session.call_sid = "CA_test2"
            session.stream_sid = "MZ_test2"
            session.caller_number = "09012345678"
            ws = FakeWs()

            async def simulate_mark_echo():
                # _play_clip_with_markが送るmarkを、Twilioが折り返してきた
                # ことにする（実際のechoはpump_twilio_to_openai側で処理される
                # ため、ここではgoodbye_eventを直接立てて模す）。
                await asyncio.sleep(0.05)
                session.goodbye_event.set()

            echo_task = asyncio.create_task(simulate_mark_echo())

            raised = False
            try:
                await call_session.openai_connection_task(session, ws, start_received_at=time.monotonic())
            except call_session.watchdogs.CallEnded:
                raised = True

            await echo_task
            assert raised, "接続失敗時はCallEndedが送出されるべき"
            assert session.openai is None
            assert session.case_created is True
            assert hangup_calls == ["CA_test2"]
            assert sf_calls == [("CA_test2", "09012345678", True)]
            # 案内クリップがTwilioへ送信されている（mediaメッセージがあること）
            assert any(m for m in ws.sent if '"event": "media"' in m)

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
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    if failures:
        sys.exit(1)
