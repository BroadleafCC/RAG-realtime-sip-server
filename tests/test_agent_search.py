import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

import agent_search
import config
from google.cloud import storage


class _Patch:
    """属性を一時的に差し替えるヘルパー（tests/test_call_session_prewarm.pyと同趣旨）。"""

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


class _FakeResponse:
    def __init__(self, status_code, json_body, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or json.dumps(json_body, ensure_ascii=False)

    def json(self):
        return self._json_body


class _FakeBlob:
    def __init__(self, text):
        self._text = text

    def download_as_text(self, encoding="utf-8"):
        return self._text


class _FakeBucket:
    def __init__(self, blobs_by_path):
        self._blobs_by_path = blobs_by_path

    def blob(self, path):
        return self._blobs_by_path[path]


class _FakeStorageClient:
    """google.cloud.storage.Clientの偽物。bucket名/blobパスごとに固定の
    テキストを返すだけで、実際のGCSには一切アクセスしない。"""

    def __init__(self, buckets):
        self._buckets = buckets

    def bucket(self, name):
        return self._buckets[name]


def _with_common_patches(post_fn, storage_client):
    return (
        _Patch(config, "AGENTSEARCH_PROJECT_ID", "proj"),
        _Patch(config, "AGENTSEARCH_ENGINE_ID", "engine"),
        _Patch(agent_search, "_get_access_token", lambda: "fake-token"),
        _Patch(agent_search.requests, "post", post_fn),
        _Patch(storage, "Client", lambda: storage_client),
    )


def test_search_success_fetches_gcs_text_for_top_result():
    """search:searchが1件返した場合、そのderivedStructData.linkが指す
    GCSファイルの中身がそのまま返ること（answer要約を経由しない）。"""
    fake_client = _FakeStorageClient({
        "voicebot-rag-faq-docs": _FakeBucket({
            "qa/oss2-003.txt": _FakeBlob("【分類】OSS2\n【想定質問】...\n【回答スクリプト】..."),
        }),
    })

    def fake_post(url, headers, json, timeout):
        assert url.endswith(":search")
        assert json == {"query": "電子保適 自賠責", "pageSize": 1}
        return _FakeResponse(200, {
            "results": [
                {"document": {"derivedStructData": {"link": "gs://voicebot-rag-faq-docs/qa/oss2-003.txt"}}},
            ],
        })

    patches = _with_common_patches(fake_post, fake_client)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = agent_search._search_faq_sync("電子保適 自賠責")

    assert result == "【分類】OSS2\n【想定質問】...\n【回答スクリプト】..."


def test_search_no_results_raises_agent_search_error():
    def fake_post(url, headers, json, timeout):
        return _FakeResponse(200, {"results": []})

    patches = _with_common_patches(fake_post, _FakeStorageClient({}))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        try:
            agent_search._search_faq_sync("何か")
            assert False, "AgentSearchErrorが送出されるべき"
        except agent_search.AgentSearchError as e:
            assert "0件" in str(e)


def test_search_missing_link_raises_agent_search_error():
    def fake_post(url, headers, json, timeout):
        return _FakeResponse(200, {
            "results": [{"document": {"derivedStructData": {}}}],
        })

    patches = _with_common_patches(fake_post, _FakeStorageClient({}))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        try:
            agent_search._search_faq_sync("何か")
            assert False, "AgentSearchErrorが送出されるべき"
        except agent_search.AgentSearchError as e:
            assert "GCS" in str(e)


def test_search_non_200_status_raises_agent_search_error():
    def fake_post(url, headers, json, timeout):
        return _FakeResponse(500, {}, text="internal error")

    patches = _with_common_patches(fake_post, _FakeStorageClient({}))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        try:
            agent_search._search_faq_sync("何か")
            assert False, "AgentSearchErrorが送出されるべき"
        except agent_search.AgentSearchError as e:
            assert "status=500" in str(e)


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
