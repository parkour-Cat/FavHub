"""Contract tests for the frozen X (bookmarks) response fixtures.

Fixtures are redacted response *bodies* captured by passive interception in
the user's logged-in session (or synthetic where live capture is impossible).
They must be JSON objects and must never contain credentials, auth headers,
or browser-debug fields.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "x"

REQUIRED_FIXTURES = frozenset(
    {
        "bookmarks-page-1.json",
        "bookmarks-page-2.json",
        "tweet-with-images.json",
        "tweet-with-quote.json",
        "tombstone.json",
        "logged-out.json",
        "page-changed.json",
    }
)

# Matched case-insensitively against the whole encoded payload. X requests
# authenticate via headers, so none of these may ever appear in a body.
FORBIDDEN_SUBSTRINGS = (
    "cookie",
    "authorization",
    "bearer",
    "csrf",
    "ct0",
    "auth_token",
    "x-client-transaction",
    "devtools",
    "debugger",
)

# JSON keys that would carry the logged-in viewer's own identity. Bookmarks
# response bodies carry relational booleans only; a viewer block appearing in
# a fixture means the capture was not redacted.
FORBIDDEN_VIEWER_KEYS = frozenset({"viewer", "viewer_v2", "user_results_by_rest_id"})


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _all_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key).casefold())
            keys |= _all_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            keys |= _all_keys(nested)
    return keys


def test_x_fixture_set_is_complete() -> None:
    present = {path.name for path in _fixture_paths()}
    assert REQUIRED_FIXTURES.issubset(present), sorted(REQUIRED_FIXTURES - present)


def test_x_fixtures_are_redacted_json_objects() -> None:
    paths = _fixture_paths()
    assert paths, f"no fixtures found under {FIXTURE_DIR}"
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), path.name
        encoded = json.dumps(payload, ensure_ascii=False).casefold()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in encoded, f"{path.name} leaks {forbidden!r}"


def test_x_fixtures_carry_no_viewer_identity() -> None:
    for path in _fixture_paths():
        payload = json.loads(path.read_text(encoding="utf-8"))
        leaked = _all_keys(payload) & FORBIDDEN_VIEWER_KEYS
        assert not leaked, f"{path.name} carries viewer identity keys: {sorted(leaked)}"
