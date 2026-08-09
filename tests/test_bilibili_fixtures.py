"""Contract tests for the frozen Bilibili response fixtures.

These fixtures are the only Bilibili response shapes the pure parsers are
allowed to see in tests. They must be JSON objects and must never contain
session credentials, request headers, or browser-debug fields, whether they
were captured from a live session or synthesized from public response shapes.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "bilibili"

# The six response shapes the connector must handle end to end.
REQUIRED_FIXTURES = frozenset(
    {
        "folders.json",
        "resources-page-1.json",
        "video-detail.json",
        "subtitle.json",
        "login-required.json",
        "page-changed.json",
    }
)

# Substrings that would signal leaked credentials, headers, or debug data.
# Matched case-insensitively against the whole encoded payload.
FORBIDDEN_SUBSTRINGS = (
    "cookie",  # also catches set-cookie
    "authorization",
    "csrf",
    "sessdata",
    "bili_jct",
    "dedeuserid",
    "buvid",
    "access_key",
    "devtools",
    "debugger",
)


def _fixture_paths() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def test_bilibili_fixture_set_is_complete() -> None:
    present = {path.name for path in _fixture_paths()}
    assert REQUIRED_FIXTURES.issubset(present), sorted(REQUIRED_FIXTURES - present)


def test_bilibili_fixtures_are_redacted_json_objects() -> None:
    paths = _fixture_paths()
    assert paths, f"no fixtures found under {FIXTURE_DIR}"
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), path.name
        encoded = json.dumps(payload, ensure_ascii=False).casefold()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in encoded, f"{path.name} leaks {forbidden!r}"
