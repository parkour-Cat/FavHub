"""Contract tests for the frozen GitHub starred-API fixtures.

Fixtures are live captures from the public REST API (no session, no
credentials involved at any point); rate-limited and page-changed shapes are
synthetic where noted in ``scripts/probe_github_contract.md``.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "github"

REQUIRED_FIXTURES = frozenset(
    {
        "starred-page-1.json",
        "star-no-description.json",
        "readme-sample.md",
        "rate-limited.json",
        "user-not-found.json",
        "page-changed.json",
    }
)

FORBIDDEN_SUBSTRINGS = ("authorization", "bearer", "cookie", "x-oauth", "token")


def test_github_fixture_set_is_complete() -> None:
    present = {path.name for path in FIXTURE_DIR.glob("*")}
    assert REQUIRED_FIXTURES.issubset(present), sorted(REQUIRED_FIXTURES - present)


def test_github_fixtures_are_redacted() -> None:
    for path in sorted(FIXTURE_DIR.glob("*")):
        folded = path.read_text(encoding="utf-8").casefold()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in folded, (path.name, forbidden)


def test_starred_page_carries_real_starred_at() -> None:
    page = json.loads((FIXTURE_DIR / "starred-page-1.json").read_text(encoding="utf-8"))
    assert isinstance(page, list) and page
    for entry in page:
        assert isinstance(entry["starred_at"], str)
        assert entry["repo"]["full_name"]
        assert entry["repo"]["html_url"].startswith("https://github.com/")
        assert "default_branch" in entry["repo"]
