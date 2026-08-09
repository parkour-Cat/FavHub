import json
from datetime import UTC, datetime
from pathlib import Path

from favhub.bilibili_mapping import VideoObservation, deduplicate, map_captured_item
from favhub.bilibili_models import (
    SOURCE_UNAVAILABLE,
    SUBTITLE_UNAVAILABLE,
    BilibiliCaptureError,
    BilibiliListEntry,
)
from favhub.bilibili_parsers import parse_resource_page, parse_subtitle, parse_video_detail

FIXTURES = Path(__file__).parent / "fixtures" / "bilibili"
OBSERVED = datetime(2026, 7, 26, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def entry(bvid: str, *, title: str = "T") -> BilibiliListEntry:
    return BilibiliListEntry(
        bvid=bvid,
        title=title,
        author="UP",
        author_mid="80000002",
        intro="intro",
        cover_url="https://i0.hdslb.com/x.jpg",
        published_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


def test_deduplicate_unions_folder_names_across_scopes() -> None:
    shared = entry("BV1")
    observations = deduplicate(
        {"100001": (shared,), "100002": (shared, entry("BV2"))},
        {"100001": "默认收藏夹", "100002": "技术分享"},
    )
    assert set(observations) == {"BV1", "BV2"}
    assert observations["BV1"].collections == ("技术分享", "默认收藏夹")
    assert observations["BV2"].collections == ("技术分享",)


def test_map_captured_item_with_detail_and_subtitle() -> None:
    observation = VideoObservation("BV1bkz2gvaz6", entry("BV1bkz2gvaz6"), ("默认收藏夹",))
    detail = parse_video_detail(load("video-detail.json"))
    subtitle = parse_subtitle(load("subtitle.json"))
    raw = json.dumps(load("subtitle.json"), ensure_ascii=False)

    item = map_captured_item(
        observation,
        detail=detail,
        subtitle=subtitle,
        subtitle_raw=raw,
        observed_at=OBSERVED,
    )

    assert item.platform == "bilibili"
    assert item.source_id == "BV1bkz2gvaz6"
    assert item.collections == ("默认收藏夹",)
    assert item.extractor_version == "bilibili-browser-v1"
    assert item.platform_metadata is not None
    assert item.platform_metadata["source_status"] == "available"
    assert item.platform_metadata["subtitle_status"] == "available"
    # Description and normalized cues both land in the body.
    assert "示例简介2" in item.body
    assert "[00:00] 示例内容2，用于验" in item.body
    paths = {asset.relative_path for asset in item.assets}
    assert paths == {"transcript/0001.md", "assets/subtitles/zh.json"}


def test_map_captured_item_detail_failure_keeps_list_metadata() -> None:
    observation = VideoObservation("BV1", entry("BV1", title="List title"), ("默认收藏夹",))
    item = map_captured_item(
        observation,
        detail=BilibiliCaptureError(SOURCE_UNAVAILABLE, "gone"),
        subtitle=None,
        subtitle_raw=None,
        observed_at=OBSERVED,
    )
    assert item.title == "List title"
    assert item.assets == ()
    assert item.platform_metadata is not None
    assert item.platform_metadata["source_status"] == SOURCE_UNAVAILABLE
    assert item.platform_metadata["subtitle_status"] == "unavailable"


def test_map_captured_item_records_subtitle_failure_code() -> None:
    observation = VideoObservation("BV1bkz2gvaz6", entry("BV1bkz2gvaz6"), ("默认收藏夹",))
    detail = parse_video_detail(load("video-detail.json"))
    item = map_captured_item(
        observation,
        detail=detail,
        subtitle=BilibiliCaptureError(SUBTITLE_UNAVAILABLE, "none"),
        subtitle_raw=None,
        observed_at=OBSERVED,
    )
    assert item.platform_metadata is not None
    assert item.platform_metadata["subtitle_status"] == SUBTITLE_UNAVAILABLE
    assert item.assets == ()


def test_map_captured_item_uses_resource_entry_when_detail_missing() -> None:
    page = parse_resource_page(load("resources-page-1.json"))
    observation = VideoObservation(page.entries[0].bvid, page.entries[0], ("默认收藏夹",))
    item = map_captured_item(
        observation,
        detail=None,
        subtitle=None,
        subtitle_raw=None,
        observed_at=OBSERVED,
    )
    assert item.title == "示例标题17：用于验证解析与索引行为"
    assert item.platform_metadata is not None
    assert item.platform_metadata["source_status"] == SOURCE_UNAVAILABLE


def test_deduplicate_keeps_earliest_fav_time_across_folders() -> None:
    from dataclasses import replace as _replace

    early = _replace(entry("BV1"), fav_time=datetime(2026, 1, 1, tzinfo=UTC))
    late = _replace(entry("BV1"), fav_time=datetime(2026, 6, 1, tzinfo=UTC))
    observations = deduplicate({"A": (late,), "B": (early,)}, {"A": "夹A", "B": "夹B"})
    assert observations["BV1"].favorited_at == datetime(2026, 1, 1, tzinfo=UTC)

    item = map_captured_item(
        observations["BV1"],
        detail=None,
        subtitle=None,
        subtitle_raw=None,
        observed_at=OBSERVED,
    )
    assert item.platform_metadata["favorited_at"] == "2026-01-01T00:00:00Z"


def test_unknown_fav_time_omits_metadata_key() -> None:
    observations = deduplicate({"A": (entry("BV2"),)}, {"A": "夹A"})
    assert observations["BV2"].favorited_at is None
    item = map_captured_item(
        observations["BV2"],
        detail=None,
        subtitle=None,
        subtitle_raw=None,
        observed_at=OBSERVED,
    )
    assert "favorited_at" not in item.platform_metadata
