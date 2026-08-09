import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.capture import PAGE_CHANGED, RATE_LIMITED, SOURCE_UNAVAILABLE, CaptureError
from favhub.github_mapping import map_captured_item, safe_source_id
from favhub.github_parsers import parse_starred_page

FIXTURES = Path(__file__).parent / "fixtures" / "github"
OBSERVED = datetime(2026, 7, 26, tzinfo=UTC)


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_starred_page_extracts_real_star_metadata() -> None:
    stars = parse_starred_page(load("starred-page-1.json"))
    assert [s.full_name for s in stars] == [
        "example-org/example-canvas",
        "example-org/example-ocr",
        "example-user/example-improve",
    ]
    first = stars[0]
    assert first.starred_at == datetime(2026, 7, 24, 7, 25, tzinfo=UTC)
    assert first.html_url == "https://github.com/example-org/example-canvas"
    assert first.default_branch
    assert first.description is None
    second = stars[1]
    assert second.language == "Python"
    assert second.description is not None


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [
        ("rate-limited.json", RATE_LIMITED),
        ("user-not-found.json", SOURCE_UNAVAILABLE),
        ("page-changed.json", PAGE_CHANGED),
    ],
)
def test_error_envelopes_are_typed(fixture: str, expected_code: str) -> None:
    with pytest.raises(CaptureError) as error:
        parse_starred_page(load(fixture))
    assert error.value.code == expected_code


def test_non_array_payload_is_page_changed() -> None:
    with pytest.raises(CaptureError) as error:
        parse_starred_page("<html>")
    assert error.value.code == PAGE_CHANGED


@pytest.mark.parametrize(
    "entry",
    [
        "not-an-object",
        {"starred_at": "2026-07-24T07:25:00Z"},
        {
            "starred_at": "昨天",
            "repo": {
                "full_name": "a/b",
                "html_url": "https://github.com/a/b",
                "default_branch": "main",
                "created_at": "2026-01-01T00:00:00Z",
            },
        },
        {
            "starred_at": "2026-07-24T07:25:00Z",
            "repo": {
                "html_url": "https://github.com/a/b",
                "default_branch": "main",
                "created_at": "2026-01-01T00:00:00Z",
            },
        },
    ],
)
def test_malformed_entries_are_page_changed(entry) -> None:
    with pytest.raises(CaptureError) as error:
        parse_starred_page([entry])
    assert error.value.code == PAGE_CHANGED


def test_safe_source_id_conversion() -> None:
    assert safe_source_id("example-org/example-canvas") == "example-org__example-canvas"


@pytest.mark.parametrize(
    "full_name",
    ["a-b/c.d.e", "owner/repo_name", "Dot.Owner/dash-repo", "a/b__c"],
)
def test_safe_source_id_edge_names_survive_domain_validation(full_name: str) -> None:
    star = parse_starred_page(
        [
            {
                "starred_at": "2026-07-24T07:25:00Z",
                "repo": {
                    "full_name": full_name,
                    "html_url": f"https://github.com/{full_name}",
                    "default_branch": "main",
                    "created_at": "2026-01-01T00:00:00Z",
                    "owner": {"login": full_name.split("/")[0]},
                },
            }
        ]
    )[0]
    item = map_captured_item(star, readme_text=None, observed_at=OBSERVED)
    assert item.source_id == safe_source_id(full_name)


def test_safe_source_id_documented_collision_is_accepted() -> None:
    # Both spellings resolve to the same item; the later sync refreshes it.
    assert safe_source_id("a/b__c") == safe_source_id("a__b/c") == "a__b__c"


def test_missing_owner_login_falls_back_to_full_name_owner() -> None:
    star = parse_starred_page(
        [
            {
                "starred_at": "2026-07-24T07:25:00Z",
                "repo": {
                    "full_name": "example-user/example-improve",
                    "html_url": "https://github.com/example-user/example-improve",
                    "default_branch": "main",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            }
        ]
    )[0]
    assert star.owner == "example-user"


def test_map_star_with_readme_and_real_favorited_at() -> None:
    stars = parse_starred_page(load("starred-page-1.json"))
    improve = next(s for s in stars if s.full_name == "example-user/example-improve")
    readme = (FIXTURES / "readme-sample.md").read_text(encoding="utf-8")

    item = map_captured_item(improve, readme_text=readme, observed_at=OBSERVED)

    assert item.platform == "github"
    assert item.source_id == "example-user__example-improve"
    assert item.canonical_url == "https://github.com/example-user/example-improve"
    assert item.title == "example-user/example-improve"
    assert item.author == "example-user"
    assert item.extractor_version == "github-api-v1"
    assert "## README" in item.body
    assert "audits any codebase" in item.body
    metadata = item.platform_metadata
    assert metadata["favorited_at"] == "2026-07-22T09:42:07Z"
    assert "favorited_at_estimated" not in metadata
    assert metadata["readme_status"] == "available"


def test_map_star_without_readme_degrades() -> None:
    star = parse_starred_page([load("star-no-description.json")])[0]
    item = map_captured_item(star, readme_text=None, observed_at=OBSERVED)
    assert item.platform_metadata["readme_status"] == "missing"
    assert "## README" not in item.body


def test_map_truncates_giant_readme_by_utf8_bytes() -> None:
    stars = parse_starred_page(load("starred-page-1.json"))
    # 128Ki CJK chars are ~384KB of UTF-8 — under the old character cap
    # but far over the 256KB byte cap.
    item = map_captured_item(stars[1], readme_text="汉" * (128 * 1024), observed_at=OBSERVED)
    assert item.platform_metadata["readme_status"] == "truncated"
    assert len(item.body.encode("utf-8")) < 256 * 1024 + 1000
