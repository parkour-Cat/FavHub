"""Contract tests for the frozen Zhihu collections-API fixtures.

Fixtures are live captures from the user's own logged-in session (personal
identifiers, url_token fields, and public image-token attributes stripped);
login-required / rate-limited / page-changed are synthetic where noted in
``scripts/probe_zhihu_contract.md``.
"""

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "zhihu"

REQUIRED_FIXTURES = frozenset(
    {
        "collections-page-1.json",
        "items-page-answer-article.json",
        "items-page-short-not-end.json",
        "item-unknown-type.json",
        "login-required.json",
        "rate-limited.json",
        "page-changed.json",
    }
)

FORBIDDEN_SUBSTRINGS = ("authorization", "bearer", "cookie", "z_c0", "token", "x-zse")


def load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_zhihu_fixture_set_is_complete() -> None:
    present = {path.name for path in FIXTURE_DIR.glob("*")}
    assert REQUIRED_FIXTURES.issubset(present), sorted(REQUIRED_FIXTURES - present)


def test_zhihu_fixtures_are_redacted() -> None:
    for path in sorted(FIXTURE_DIR.glob("*")):
        folded = path.read_text(encoding="utf-8").casefold()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in folded, (path.name, forbidden)


def test_collections_page_carries_scope_fields() -> None:
    page = load("collections-page-1.json")
    assert page["data"]
    for folder in page["data"]:
        assert isinstance(folder["id"], int)
        assert folder["title"]
        assert isinstance(folder["item_count"], int)
    assert page["paging"]["is_end"] is False


def test_items_page_carries_real_favorited_time_and_full_html() -> None:
    page = load("items-page-answer-article.json")
    types = [item["content"]["type"] for item in page["data"]]
    assert types == ["answer", "article"]
    for item in page["data"]:
        assert "+08:00" in item["created"]
        assert len(item["content"]["content"]) > 100  # full HTML, not an excerpt
    answer, article = (item["content"] for item in page["data"])
    assert answer["question"]["title"]
    assert "created_time" in answer and "updated_time" in answer
    assert article["title"] and "zhuanlan.zhihu.com" in article["url"]
    assert "created" in article and "updated" in article  # different key names


def test_short_page_is_not_end() -> None:
    # Deleted/hidden favorites shrink pages below the limit while more pages
    # remain: is_end is the only honest end signal, totals stay advisory.
    page = load("items-page-short-not-end.json")
    assert len(page["data"]) < page["paging"]["totals"]
    assert page["paging"]["is_end"] is False


def test_error_envelopes_use_the_documented_shape() -> None:
    for name in ("login-required.json", "rate-limited.json"):
        envelope = load(name)
        assert isinstance(envelope["error"], dict)
        assert envelope["error"]["message"]
    assert not isinstance(load("page-changed.json")["data"], list)
