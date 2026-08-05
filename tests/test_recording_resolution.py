"""録音開始のリトライと、録音のCall SID紐付け解決（修正指示書パートB）のテスト。

守りたい性質は2つ:
  1. 21220（まだ録音できない）で1回失敗しただけで録音を諦めない
  2. 録音が無い通話が、絶対に他通話の録音を掴まない
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import config
import salesforce_case
import twilio_client


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


class FakeTwilioError(Exception):
    """twilio.base.exceptions.TwilioRestException の代役（.code を持つ）。"""

    def __init__(self, code, msg="boom"):
        super().__init__(msg)
        self.code = code


class FakeRecording:
    def __init__(self, sid="RE_fake", status="completed", duration="7"):
        self.sid = sid
        self.status = status
        self.duration = duration


class FakeRecordingsResource:
    def __init__(self, owner):
        self.owner = owner

    def create(self):
        self.owner.parent.create_calls += 1
        errors = self.owner.parent.create_errors
        if errors:
            raise errors.pop(0)
        return FakeRecording(sid="RE_started")

    def list(self, limit=1):
        self.owner.parent.call_scoped_list_calls.append(self.owner.call_sid)
        return self.owner.parent.call_scoped_recordings


class FakeCall:
    def __init__(self, parent, call_sid):
        self.parent = parent
        self.call_sid = call_sid
        self.recordings = FakeRecordingsResource(self)


class FakeGlobalRecordings:
    """アカウント全体の録音リソース。時間窓での総ざらい（他通話の録音を掴む
    原因になった経路）が呼ばれたら即座にテストを失敗させる。"""

    def __init__(self, parent):
        self.parent = parent

    def list(self, *a, **kw):
        raise AssertionError(
            "アカウント全体の recordings.list が呼ばれた（他通話の録音を掴む経路）"
        )

    def __call__(self, recording_sid):
        self.parent.fetched_sids.append(recording_sid)
        return _FakeFetchable(self.parent)


class _FakeFetchable:
    def __init__(self, parent):
        self.parent = parent

    def fetch(self):
        return self.parent.recording_by_sid


class FakeTwilioClient:
    def __init__(self, create_errors=None, call_scoped_recordings=None, recording_by_sid=None):
        self.create_errors = list(create_errors or [])
        self.create_calls = 0
        self.call_scoped_list_calls = []
        self.call_scoped_recordings = list(call_scoped_recordings or [])
        self.recording_by_sid = recording_by_sid
        self.fetched_sids = []
        self.recordings = FakeGlobalRecordings(self)

    def calls(self, call_sid):
        return FakeCall(self, call_sid)


class FakeTime:
    """time.sleep を記録するだけの偽物（テストを実時間で待たせない）。"""

    def __init__(self):
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)


# --- B-a: start_recording のリトライ -----------------------------------------

def test_start_recording_succeeds_first_attempt_without_retry():
    """通常はリトライせず1回で成功する（完了条件B-4-1）。"""
    fake = FakeTwilioClient()
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(twilio_client, "time", fake_time):
        sid = twilio_client.start_recording("CA_1")
    assert sid == "RE_started"
    assert fake.create_calls == 1
    assert fake_time.slept == []


def test_start_recording_retries_on_21220_then_succeeds():
    """21220は待てば成功する見込みがあるのでリトライする（完了条件B-4-2）。
    今回の3本目（コールドスタートで録音開始が早すぎた）の再現。"""
    fake = FakeTwilioClient(create_errors=[FakeTwilioError(21220)])
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(twilio_client, "time", fake_time), \
         _Patch(config, "RECORDING_RETRY_ENABLED", True), \
         _Patch(config, "RECORDING_MAX_RETRIES", 4), \
         _Patch(config, "RECORDING_RETRY_BACKOFF_MS", 300):
        sid = twilio_client.start_recording("CA_2")
    assert sid == "RE_started"
    assert fake.create_calls == 2
    assert fake_time.slept == [0.3]  # 1回目の失敗後に300ms待つ


def test_start_recording_backoff_increases_and_gives_up():
    """リトライを尽くしたらNoneを返す（呼び出し側は録音無しとして扱う）。"""
    fake = FakeTwilioClient(create_errors=[FakeTwilioError(21220) for _ in range(4)])
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(twilio_client, "time", fake_time), \
         _Patch(config, "RECORDING_RETRY_ENABLED", True), \
         _Patch(config, "RECORDING_MAX_RETRIES", 4), \
         _Patch(config, "RECORDING_RETRY_BACKOFF_MS", 300):
        sid = twilio_client.start_recording("CA_3")
    assert sid is None
    assert fake.create_calls == 4
    assert fake_time.slept == [0.3, 0.6, 0.9]  # 伸びるバックオフ・最後は待たない


def test_start_recording_does_not_retry_non_timing_errors():
    """認証エラー等、待っても直らない失敗はリトライしない。"""
    fake = FakeTwilioClient(create_errors=[FakeTwilioError(20003), FakeTwilioError(20003)])
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(twilio_client, "time", fake_time), \
         _Patch(config, "RECORDING_RETRY_ENABLED", True):
        sid = twilio_client.start_recording("CA_4")
    assert sid is None
    assert fake.create_calls == 1
    assert fake_time.slept == []


def test_start_recording_retry_can_be_disabled():
    fake = FakeTwilioClient(create_errors=[FakeTwilioError(21220), FakeTwilioError(21220)])
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(twilio_client, "time", fake_time), \
         _Patch(config, "RECORDING_RETRY_ENABLED", False):
        sid = twilio_client.start_recording("CA_5")
    assert sid is None
    assert fake.create_calls == 1


# --- B-b: 録音の Call SID 直引き ---------------------------------------------

def test_fetch_recording_for_call_uses_call_scoped_list():
    fake = FakeTwilioClient(call_scoped_recordings=[FakeRecording(sid="RE_mine")])
    with _Patch(twilio_client, "_get_client", lambda: fake):
        rec = twilio_client.fetch_recording_for_call("CA_mine")
    assert rec.sid == "RE_mine"
    assert fake.call_scoped_list_calls == ["CA_mine"]


def test_fetch_recording_for_call_returns_none_when_call_has_no_recording():
    """録音開始に失敗した通話では None を返す。ここで他通話を探しにいかない
    ことが、別通話の文字起こし混入を構造的に防ぐ要（完了条件B-4-4）。"""
    fake = FakeTwilioClient(call_scoped_recordings=[])
    with _Patch(twilio_client, "_get_client", lambda: fake):
        assert twilio_client.fetch_recording_for_call("CA_none") is None


def test_resolve_recording_prefers_known_recording_sid():
    """start_recording が返したSIDがあれば、それを直接引く。"""
    fake = FakeTwilioClient(recording_by_sid=FakeRecording(sid="RE_known"))
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(salesforce_case, "time_module", fake_time):
        rec = salesforce_case._resolve_recording("CA_x", "RE_known")
    assert rec.sid == "RE_known"
    assert fake.fetched_sids == ["RE_known"]
    assert fake.call_scoped_list_calls == []


def test_resolve_recording_polls_until_completed():
    """録音は通話終了直後はまだ in-progress。completed になるまで待つ。"""
    fake = FakeTwilioClient(call_scoped_recordings=[FakeRecording(sid="RE_p", status="in-progress")])
    fake_time = FakeTime()
    with _Patch(twilio_client, "_get_client", lambda: fake), \
         _Patch(salesforce_case, "time_module", fake_time), \
         _Patch(salesforce_case, "RECORDING_MAX_WAIT_SEC", 15):
        # 2回目の照会で completed になる
        original = twilio_client.fetch_recording_for_call
        calls = {"n": 0}

        def flaky(call_sid):
            calls["n"] += 1
            if calls["n"] >= 2:
                return FakeRecording(sid="RE_p", status="completed")
            return original(call_sid)

        with _Patch(twilio_client, "fetch_recording_for_call", flaky):
            rec = salesforce_case._resolve_recording("CA_poll", "")

    assert rec is not None and rec.status == "completed"
    assert calls["n"] == 2


def test_resolve_recording_survives_transient_api_error():
    fake_time = FakeTime()
    calls = {"n": 0}

    def flaky(call_sid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("temporary API failure")
        return FakeRecording(sid="RE_after_error")

    with _Patch(twilio_client, "fetch_recording_for_call", flaky), \
         _Patch(salesforce_case, "time_module", fake_time), \
         _Patch(salesforce_case, "RECORDING_MAX_WAIT_SEC", 15):
        rec = salesforce_case._resolve_recording("CA_err", "")
    assert rec is not None and rec.sid == "RE_after_error"


def test_resolve_recording_gives_up_and_returns_none():
    fake_time = FakeTime()
    with _Patch(twilio_client, "fetch_recording_for_call", lambda call_sid: None), \
         _Patch(salesforce_case, "time_module", fake_time), \
         _Patch(salesforce_case, "RECORDING_MAX_WAIT_SEC", 10):
        assert salesforce_case._resolve_recording("CA_never", "") is None


class FakeSalesforce:
    def __init__(self):
        self.updates = []
        self.Case = self

    def update(self, case_id, fields, headers=None):
        self.updates.append((case_id, fields))


def test_attach_recording_writes_explicit_note_when_no_recording():
    """録音が無い通話は「録音取得失敗のため通話内容なし」と明記して更新し、
    Whisperには進まない（完了条件B-4-3）。"""
    sf = FakeSalesforce()
    fake_time = FakeTime()
    logged = []

    def boom(*a, **kw):
        raise AssertionError("録音が無いのに音声をダウンロードしようとした")

    with _Patch(config, "TWILIO_ACCOUNT_SID", "AC_dummy"), \
         _Patch(config, "TWILIO_AUTH_TOKEN", "token_dummy"), \
         _Patch(salesforce_case, "_sf_connect", lambda: sf), \
         _Patch(salesforce_case, "time_module", fake_time), \
         _Patch(salesforce_case, "RECORDING_MAX_WAIT_SEC", 5), \
         _Patch(salesforce_case.requests, "get", boom), \
         _Patch(twilio_client, "fetch_recording_for_call", lambda call_sid: None), \
         _Patch(salesforce_case.logger, "warning", lambda msg, *a: logged.append(msg % a)):
        salesforce_case.attach_recording_to_case("500_case", "CA_norec", "")

    assert len(sf.updates) == 1
    case_id, fields = sf.updates[0]
    assert case_id == "500_case"
    assert "録音取得失敗のため通話内容なし" in fields["SC_CorrespondenceRemarks__c"]
    assert any("[POSTCALL] 録音無し" in line for line in logged)


def test_time_window_recording_sweep_is_removed_from_source():
    """時間窓での録音探索（DateCreated>＋直近N件）の経路が撤去されていること。
    実装として二度と書かないための構造的な歯止め（完了条件B-4-4）。"""
    source = inspect.getsource(salesforce_case)
    assert "date_created_after" not in source
    assert "recordings.list(" not in source


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
