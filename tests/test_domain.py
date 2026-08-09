from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from favhub.domain import (
    CapturedAsset,
    CapturedItem,
    SyncMode,
    sha256_text,
    validate_enrichment,
)


def item() -> CapturedItem:
    return CapturedItem(
        platform="x",
        source_id="1844674407370955161",
        canonical_url="https://x.com/example/status/1844674407370955161",
        title="A saved post",
        author="example",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="Hybrid retrieval combines lexical and semantic search.",
        collections=("Research", "AI"),
        extractor_version="x-browser-v1",
    )


def test_content_hash_ignores_observation_time_and_collection_order() -> None:
    original = item()
    later = replace(
        original,
        observed_at=datetime(2026, 7, 19, tzinfo=UTC),
        collections=("AI", "Research"),
    )
    assert original.content_hash == later.content_hash


def test_content_hash_canonicalizes_equivalent_aware_instants() -> None:
    original = item()
    same_instant = replace(
        original,
        published_at=datetime(2026, 1, 2, 8, tzinfo=timezone(timedelta(hours=8))),
    )
    assert original.content_hash == same_instant.content_hash


@pytest.mark.parametrize("source_id", ["../escape", "a/b", "", "a\\b"])
def test_source_id_rejects_unsafe_paths(source_id: str) -> None:
    with pytest.raises(ValueError, match="source_id"):
        replace(item(), source_id=source_id)


@pytest.mark.parametrize(
    "source_id",
    [
        ".",
        "..",
        "post.",
        "CON",
        "prn.txt",
        "AuX.json",
        "nul",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"lpt{number}.txt" for number in range(1, 10)),
    ],
)
def test_source_id_rejects_windows_unsafe_names(source_id: str) -> None:
    with pytest.raises(ValueError, match="source_id"):
        replace(item(), source_id=source_id)


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://user@",
        "https://exa mple.com",
        "https://example.com:bad",
        "https://example.com/\x00",
    ],
)
def test_canonical_url_rejects_malformed_urls(canonical_url: str) -> None:
    with pytest.raises(ValueError, match="canonical_url"):
        replace(item(), canonical_url=canonical_url)


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(item(), published_at=datetime(2026, 1, 2))


def test_timestamps_reject_timezone_without_utc_offset() -> None:
    class NoOffsetTZ(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            return None

        def tzname(self, dt: datetime | None) -> str | None:
            return None

    with pytest.raises(ValueError, match="timezone-aware"):
        replace(item(), published_at=datetime(2026, 1, 2, tzinfo=NoOffsetTZ()))


def test_to_source_dict_normalizes_collections_and_timestamps() -> None:
    captured = replace(
        item(),
        published_at=datetime(2026, 1, 2, 8, tzinfo=timezone(timedelta(hours=8))),
        observed_at=datetime(2026, 7, 18, 8, tzinfo=timezone(timedelta(hours=8))),
        collections=("Research", "AI", "Research"),
    )

    assert captured.to_source_dict() == {
        "schema_version": 1,
        "platform": "x",
        "source_id": "1844674407370955161",
        "canonical_url": "https://x.com/example/status/1844674407370955161",
        "title": "A saved post",
        "author": "example",
        "published_at": "2026-01-02T00:00:00Z",
        "observed_at": "2026-07-18T00:00:00Z",
        "body": "Hybrid retrieval combines lexical and semantic search.",
        "collections": ["AI", "Research"],
        "extractor_version": "x-browser-v1",
        "content_hash": captured.content_hash,
    }


def test_sync_mode_values_are_stable() -> None:
    assert SyncMode.FULL.value == "full"
    assert SyncMode.INCREMENTAL.value == "incremental"


def subtitle_asset(
    text: str = '{"body": []}',
    *,
    path: str = "assets/subtitles/zh.json",
    media_type: str = "application/json",
) -> CapturedAsset:
    return CapturedAsset(
        relative_path=path,
        media_type=media_type,
        text=text,
        sha256=sha256_text(text),
    )


def test_captured_asset_accepts_text_subtitle() -> None:
    asset = subtitle_asset()
    assert asset.descriptor() == {
        "relative_path": "assets/subtitles/zh.json",
        "media_type": "application/json",
        "sha256": sha256_text('{"body": []}'),
        "size_bytes": len(b'{"body": []}'),
    }


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "assets/../../escape.json",
        "../assets/zh.json",
        "notes.md",
        "content.md",
        "assets\\subtitles\\zh.json",
        "C:/assets/zh.json",
        "assets",
        "assets/",
        "assets/CON.json",
    ],
)
def test_captured_asset_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        CapturedAsset(
            relative_path=path,
            media_type="application/json",
            text="{}",
            sha256=sha256_text("{}"),
        )


def test_captured_asset_rejects_non_text_media_type() -> None:
    with pytest.raises(ValueError, match="media_type"):
        CapturedAsset(
            relative_path="assets/cover.bin",
            media_type="application/octet-stream",
            text="x",
            sha256=sha256_text("x"),
        )


def test_captured_asset_rejects_invalid_utf8() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        CapturedAsset(
            relative_path="assets/subtitles/zh.json",
            media_type="application/json",
            text="\ud800",
            sha256="0" * 64,
        )


def test_captured_asset_rejects_oversized_text() -> None:
    big = "a" * (2 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="maximum size"):
        CapturedAsset(
            relative_path="assets/subtitles/big.json",
            media_type="application/json",
            text=big,
            sha256=sha256_text(big),
        )


def test_captured_asset_rejects_sha256_mismatch() -> None:
    with pytest.raises(ValueError, match="sha256"):
        CapturedAsset(
            relative_path="assets/subtitles/zh.json",
            media_type="application/json",
            text="{}",
            sha256="0" * 64,
        )


def test_captured_item_defaults_have_no_extension_fields() -> None:
    base = item()
    assert base.platform_metadata is None
    assert base.assets == ()
    snapshot = base.to_source_dict()
    assert "platform_metadata" not in snapshot
    assert "assets" not in snapshot


def test_to_source_dict_includes_extensions_without_asset_text() -> None:
    asset = subtitle_asset()
    captured = replace(
        item(),
        platform="bilibili",
        source_id="BV1aa411c7mD",
        canonical_url="https://www.bilibili.com/video/BV1aa411c7mD",
        platform_metadata={"subtitle_status": "available", "cover_url": "https://i0/y.jpg"},
        assets=(asset,),
    )
    snapshot = captured.to_source_dict()
    assert snapshot["platform_metadata"] == {
        "subtitle_status": "available",
        "cover_url": "https://i0/y.jpg",
    }
    assert snapshot["assets"] == [asset.descriptor()]
    assert "text" not in snapshot["assets"][0]
    # Extensions never influence content_hash.
    assert (
        snapshot["content_hash"]
        == replace(captured, platform_metadata=None, assets=()).content_hash
    )


def test_platform_metadata_must_be_mapping() -> None:
    with pytest.raises(ValueError, match="platform_metadata"):
        replace(item(), platform_metadata=["not", "a", "dict"])  # type: ignore[arg-type]


def test_assets_reject_duplicate_paths() -> None:
    asset = subtitle_asset()
    with pytest.raises(ValueError, match="duplicate asset"):
        replace(item(), assets=(asset, asset))


def test_captured_asset_accepts_ocr_root() -> None:
    text = "# OCR\n\n图内文字\n"
    asset = CapturedAsset(
        relative_path="ocr/0001.md",
        media_type="text/markdown",
        text=text,
        sha256=sha256_text(text),
    )
    assert asset.descriptor()["relative_path"] == "ocr/0001.md"


def enrichment_block(**overrides: object) -> dict:
    payload: dict = {
        "summary": "介绍如何用 codex 生成带货视频的完整工作流。",
        "tags": ["codex", "视频剪辑", "ai 工作流"],
        "content_type": "video",
        "provider": "agent",
        "model": "claude-fable-5",
        "generated_at": "2026-07-26T12:00:00Z",
        "input_hash": "a" * 64,
    }
    payload.update(overrides)
    return payload


def test_validate_enrichment_accepts_and_copies() -> None:
    block = enrichment_block()
    validated = validate_enrichment(block)
    assert validated == block
    assert validated is not block


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": "   "},
        {"summary": "长" * 2001},
        {"tags": []},
        {"tags": ["t"] * 9},
        {"tags": ["ok", 7]},
        {"tags": ["长" * 41]},
        {"tags": ["dup", "DUP"]},
        {"content_type": "audio"},
        {"provider": ""},
        {"model": "   "},
        {"generated_at": "2026-07-26T12:00:00"},
        {"generated_at": "not-a-time"},
        {"input_hash": ""},
        {"cookie": "x"},
    ],
)
def test_validate_enrichment_rejects_malformed(overrides: dict) -> None:
    with pytest.raises(ValueError):
        validate_enrichment(enrichment_block(**overrides))


def test_validate_enrichment_rejects_non_mapping() -> None:
    with pytest.raises(ValueError):
        validate_enrichment(["not", "a", "mapping"])
