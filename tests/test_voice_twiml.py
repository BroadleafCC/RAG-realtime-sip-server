"""/voice のTwiMLと /call-status の疎通テスト（修正指示書パートA）。

完了条件A-4-1（OFFのときデグレしない）を守るための最低限の確認。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("CALL_LOG_DB_PATH", "./call_log.db")

from fastapi.testclient import TestClient

import call_session
import config
import main


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


def _post_voice(client):
    return client.post("/voice", data={"From": "+819012345678", "CallSid": "CA_twiml"})


def _no_heartbeat():
    """テスト中はループ監視を起動しない。監視は stall 検知時に os._exit する
    仕組みなので、テストプロセスに常駐させない（誤発火でpytestごと落ちる）。"""
    return _Patch(config, "LOOP_HEARTBEAT_ENABLED", False)


def test_twiml_unchanged_when_tracking_disabled():
    """OFFのときのTwiMLは従来どおり（statusCallbackを一切足さない）。"""
    with _no_heartbeat(), _Patch(config, "DISCONNECT_TRACKING_ENABLED", False), \
         _Patch(call_session, "prewarm_openai_connection", lambda call_sid: None):
        with TestClient(main.app) as client:
            resp = _post_voice(client)
    assert resp.status_code == 200
    assert "statusCallback" not in resp.text
    assert "<Stream url=\"wss://" in resp.text
    assert '<Parameter name="caller" value="+819012345678" />' in resp.text


def test_twiml_adds_status_callback_when_tracking_enabled():
    with _no_heartbeat(), _Patch(config, "DISCONNECT_TRACKING_ENABLED", True), \
         _Patch(call_session, "prewarm_openai_connection", lambda call_sid: None):
        with TestClient(main.app) as client:
            resp = _post_voice(client)
    assert resp.status_code == 200
    assert '/call-status"' in resp.text
    assert 'statusCallbackMethod="POST"' in resp.text
    # Streamのurl属性を壊していないこと（属性の連結ミスは即通話断になる）
    assert 'wss://testserver/media-stream" statusCallback="https://testserver/call-status"' in resp.text


def test_call_status_returns_204_and_records_nothing_when_disabled():
    recorded = []
    with _no_heartbeat(), _Patch(config, "DISCONNECT_TRACKING_ENABLED", False), \
         _Patch(call_session, "record_call_status", lambda sid, payload: recorded.append(sid)):
        with TestClient(main.app) as client:
            resp = client.post("/call-status", data={"CallSid": "CA_x", "CallDuration": "0"})
    assert resp.status_code == 204
    assert recorded == []


def test_call_status_records_twilio_fields_when_enabled():
    """Twilioのフィールド名は確定値。憶測の別名を使わないこと。"""
    recorded = []
    with _no_heartbeat(), _Patch(config, "DISCONNECT_TRACKING_ENABLED", True), \
         _Patch(call_session, "record_call_status", lambda sid, payload: recorded.append((sid, payload))):
        with TestClient(main.app) as client:
            resp = client.post("/call-status", data={
                "CallSid": "CA_y",
                "CallStatus": "completed",
                "CallDuration": "37",
                "StreamEvent": "stream-stopped",
            })
    assert resp.status_code == 204
    assert recorded == [("CA_y", {
        "CallStatus": "completed",
        "CallDuration": "37",
        "SipResponseCode": "",
        "StreamEvent": "stream-stopped",
    })]


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
