import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

from salesforce_case import normalize_phone


def test_e164_japan_number_converts_to_domestic():
    assert normalize_phone("+819092415397") == "09092415397"


def test_already_domestic_number_unchanged():
    assert normalize_phone("09092415397") == "09092415397"


def test_empty_string_passthrough():
    assert normalize_phone("") == ""


def test_none_passthrough():
    assert normalize_phone(None) is None


def test_strips_non_digit_characters():
    assert normalize_phone("+81-90-9241-5397") == "09092415397"


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
