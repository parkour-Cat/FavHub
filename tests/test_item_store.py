import errno
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from favhub import item_store as item_store_module
from favhub.domain import CapturedAsset, CapturedItem, sha256_text
from favhub.item_store import ItemStore, SourceSnapshotError


def captured(body: str = "Original body") -> CapturedItem:
    return CapturedItem(
        platform="x",
        source_id="42",
        canonical_url="https://x.com/example/status/42",
        title="Saved post",
        author="example",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body=body,
        collections=("Research",),
        extractor_version="fixture-v1",
    )


SUBTITLE_RAW = '{"body": [{"from": 0.0, "to": 1.0, "content": "hi"}]}'
TRANSCRIPT_MD = "# Transcript\n\n[00:00.000 -> 00:01.000] hi\n"


def bilibili_item_with_assets() -> CapturedItem:
    return CapturedItem(
        platform="bilibili",
        source_id="BV1aa411c7mD",
        canonical_url="https://www.bilibili.com/video/BV1aa411c7mD",
        title="Sample video",
        author="UP",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="intro\n\n[00:00] hi",
        collections=("技术分享",),
        extractor_version="bilibili-browser-v1",
        platform_metadata={"subtitle_status": "available"},
        authoritative_asset_roots=("transcript", "assets"),
        assets=(
            CapturedAsset(
                "transcript/0001.md", "text/markdown", TRANSCRIPT_MD, sha256_text(TRANSCRIPT_MD)
            ),
            CapturedAsset(
                "assets/subtitles/zh.json",
                "application/json",
                SUBTITLE_RAW,
                sha256_text(SUBTITLE_RAW),
            ),
        ),
    )


def test_write_persists_transcript_and_subtitle_assets(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    result = store.write(bilibili_item_with_assets())
    directory = result.directory
    assert (directory / "transcript" / "0001.md").read_text("utf-8").startswith("# Transcript")
    raw = json.loads((directory / "assets" / "subtitles" / "zh.json").read_text("utf-8"))
    assert raw["body"][0]["content"] == "hi"
    assert (directory / "notes.md").read_text("utf-8") == "# Notes\n"
    snapshot = json.loads((directory / "source.json").read_text("utf-8"))
    assert snapshot["platform_metadata"] == {"subtitle_status": "available"}
    assert {descriptor["relative_path"] for descriptor in snapshot["assets"]} == {
        "transcript/0001.md",
        "assets/subtitles/zh.json",
    }


def test_write_assets_preserve_existing_user_notes(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    first = store.write(bilibili_item_with_assets())
    (first.directory / "notes.md").write_text("My durable note\n", encoding="utf-8")
    store.write(bilibili_item_with_assets())
    assert (first.directory / "notes.md").read_text("utf-8") == "My durable note\n"


def test_a_rewrite_that_lost_its_transcript_does_not_keep_indexing_the_old_one(
    tmp_path: Path,
) -> None:
    """Overwriting what a capture has says nothing about what it no longer has.

    A Bilibili video whose transcript was refused for belonging to another video
    was rewritten with a clean content.md and kept twelve kilobytes of the wrong
    video's words on disk — where `transcript/` is indexed in its own right, so
    search went on returning them. The refusal existed to prevent exactly that.
    """
    store = ItemStore(tmp_path / "items")
    full = store.write(bilibili_item_with_assets())
    assert (full.directory / "transcript" / "0001.md").is_file()

    without_transcript = replace(bilibili_item_with_assets(), assets=(), body="intro")
    store.write(without_transcript)

    assert not (full.directory / "transcript" / "0001.md").exists()
    assert not (full.directory / "assets" / "subtitles" / "zh.json").exists()
    assert dict(store.iter_index_markdown("bilibili", "BV1aa411c7mD")).keys() == {"content.md"}
    # The user's own file is not an asset and is never in reach of this.
    assert (full.directory / "notes.md").is_file()


def test_a_capture_that_was_never_asked_for_ocr_does_not_delete_it(tmp_path: Path) -> None:
    """Producing nothing is not the same claim as having found nothing.

    X captures OCR only when descriptions are handed to it, so an ordinary sync
    produces none — and pruning on that would have erased all 356 image
    descriptions in this library the next time their tweets were refreshed.
    Bilibili is the opposite case and says so; the difference is declared by the
    mapper rather than guessed from an empty list.
    """
    store = ItemStore(tmp_path / "items")
    described = replace(
        bilibili_item_with_assets(),
        platform="x",
        source_id="1232164438310380159",
        canonical_url="https://x.com/example/status/1232164438310380159",
        collections=(),
        platform_metadata=None,
        authoritative_asset_roots=("ocr",),
        assets=(CapturedAsset("ocr/0001.md", "text/markdown", "# 图1\n", sha256_text("# 图1\n")),),
    )
    written = store.write(described)

    plain_sync = replace(described, assets=(), authoritative_asset_roots=())
    store.write(plain_sync)

    assert (written.directory / "ocr" / "0001.md").is_file()
    assert "ocr/0001.md" in dict(store.iter_index_markdown("x", "1232164438310380159"))


def test_iter_index_markdown_includes_transcript_not_raw_subtitle(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    store.write(bilibili_item_with_assets())
    entries = dict(store.iter_index_markdown("bilibili", "BV1aa411c7mD"))
    assert "content.md" in entries
    assert "transcript/0001.md" in entries
    assert all("assets/" not in path for path in entries)


def test_round_trip_reads_bilibili_item_with_extensions(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    store.write(bilibili_item_with_assets())
    snapshots = ItemStore(tmp_path / "items").iter_sources()
    assert len(snapshots) == 1
    assert snapshots[0]["platform"] == "bilibili"
    assert snapshots[0]["platform_metadata"] == {"subtitle_status": "available"}


def test_iter_sources_rejects_malformed_asset_descriptor(tmp_path: Path) -> None:
    snapshot = bilibili_item_with_assets().to_source_dict()
    snapshot["assets"] = [{"relative_path": "assets/subtitles/zh.json"}]
    source = source_snapshot(tmp_path, snapshot, platform="bilibili", source_id="BV1aa411c7mD")

    with pytest.raises(SourceSnapshotError, match="asset descriptor") as error:
        ItemStore(tmp_path / "items").iter_sources()

    assert error.value.path == source


def test_write_creates_system_files_and_user_notes(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    result = store.write(captured())
    assert result.directory == tmp_path / "items" / "x" / "42"
    assert json.loads((result.directory / "source.json").read_text("utf-8"))["source_id"] == "42"
    assert "Original body" in (result.directory / "content.md").read_text("utf-8")
    assert (result.directory / "notes.md").read_text("utf-8") == "# Notes\n"


def test_refresh_never_overwrites_user_notes(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    first = store.write(captured())
    (first.directory / "notes.md").write_text("My durable note\n", encoding="utf-8")
    store.write(captured(body="Refreshed body"))
    assert (first.directory / "notes.md").read_text("utf-8") == "My durable note\n"
    assert "Refreshed body" in (first.directory / "content.md").read_text("utf-8")


def test_write_preserves_notes_when_absence_check_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ItemStore(tmp_path / "items")
    notes = tmp_path / "items" / "x" / "42" / "notes.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Concurrent durable note\n", encoding="utf-8")
    original_exists = Path.exists

    def stale_exists(path: Path) -> bool:
        if path == notes:
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", stale_exists)

    store.write(captured())

    assert notes.read_text("utf-8") == "Concurrent durable note\n"


def test_failed_initial_source_publication_is_not_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ItemStore(tmp_path / "items")
    original_atomic_text = store._atomic_text

    def fail_source_publication(path: Path, content: str) -> None:
        if path.name == "source.json":
            raise OSError("injected source publication failure")
        original_atomic_text(path, content)

    monkeypatch.setattr(store, "_atomic_text", fail_source_publication)

    with pytest.raises(OSError, match="injected source publication failure"):
        store.write(captured())

    directory = tmp_path / "items" / "x" / "42"
    assert "Original body" in (directory / "content.md").read_text("utf-8")
    assert (directory / "notes.md").read_text("utf-8") == "# Notes\n"
    assert (directory / "assets").is_dir()
    assert store.iter_sources() == []


def test_failed_refresh_source_publication_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ItemStore(tmp_path / "items")
    stored = store.write(captured())
    source = stored.directory / "source.json"
    previous_source = source.read_text("utf-8")
    original_atomic_text = store._atomic_text

    def fail_source_publication(path: Path, content: str) -> None:
        if path.name == "source.json":
            raise OSError("injected source publication failure")
        original_atomic_text(path, content)

    monkeypatch.setattr(store, "_atomic_text", fail_source_publication)

    with pytest.raises(OSError, match="injected source publication failure"):
        store.write(captured(body="Refreshed body"))

    assert source.read_text("utf-8") == previous_source
    assert "Refreshed body" in (stored.directory / "content.md").read_text("utf-8")

    assert store.ensure_content_from_source("x", "42") is True
    assert source.read_text("utf-8") == previous_source
    assert "Original body" in (stored.directory / "content.md").read_text("utf-8")


def test_iter_sources_reads_recoverable_snapshots(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    store.write(captured())
    snapshots = list(store.iter_sources())
    assert [(snapshot["platform"], snapshot["source_id"]) for snapshot in snapshots] == [
        ("x", "42")
    ]


def test_read_source_rejects_platform_path_traversal(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")

    with pytest.raises(ValueError, match="item path"):
        store.read_source("../outside", "id")

    assert not (tmp_path / "outside").exists()


def test_ensure_content_from_source_rejects_source_id_path_traversal(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")

    with pytest.raises(ValueError, match="item path"):
        store.ensure_content_from_source("x", "../outside")

    assert not (tmp_path / "outside").exists()


def test_write_rejects_casefold_source_id_collision(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    first = replace(captured(), source_id="ABC")
    second = replace(captured(), source_id="abc")
    store.write(first)

    with pytest.raises(ValueError, match="case-insensitive"):
        store.write(second)

    source = store.items_root / "x" / "ABC" / "source.json"
    assert json.loads(source.read_text(encoding="utf-8"))["source_id"] == "ABC"


def test_iter_sources_rejects_source_symlink_outside_items_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside_source = tmp_path / "outside" / "source.json"
    outside_source.parent.mkdir()
    outside_source.write_text(
        json.dumps(captured().to_source_dict()),
        encoding="utf-8",
    )
    source = tmp_path / "items" / "x" / "42" / "source.json"
    source.parent.mkdir(parents=True)
    try:
        source.symlink_to(outside_source)
    except OSError:
        source.write_text(outside_source.read_text(encoding="utf-8"), encoding="utf-8")
        original_resolve = Path.resolve

        def resolve_as_external(path: Path, strict: bool = False) -> Path:
            if path == source:
                return outside_source
            return original_resolve(path, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve_as_external)

    with pytest.raises(ValueError, match="item path"):
        ItemStore(tmp_path / "items").iter_sources()


def test_write_rejects_in_root_item_alias_without_modifying_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ItemStore(tmp_path / "items")
    target = replace(captured(body="target body"), source_id="B")
    store.write(target)
    target_directory = store.items_root / "x" / "B"
    target_source_before = (target_directory / "source.json").read_bytes()
    target_content_before = (target_directory / "content.md").read_bytes()
    alias_directory = store.items_root / "x" / "A"
    try:
        alias_directory.symlink_to(target_directory, target_is_directory=True)
    except OSError:
        alias_directory.mkdir()
        original_is_junction = Path.is_junction

        def treat_alias_as_junction(path: Path) -> bool:
            return path == alias_directory or original_is_junction(path)

        monkeypatch.setattr(Path, "is_junction", treat_alias_as_junction)

    with pytest.raises(ValueError, match="reparse"):
        store.write(replace(captured(body="alias body"), source_id="A"))

    assert (target_directory / "source.json").read_bytes() == target_source_before
    assert (target_directory / "content.md").read_bytes() == target_content_before


def test_iter_sources_scans_each_platform_directory_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    items_root = tmp_path / "items"
    writer = ItemStore(items_root)
    for number in range(6):
        writer.write(replace(captured(), source_id=f"item-{number}"))
    platform_directory = items_root / "x"
    original_iterdir = Path.iterdir
    scans = 0

    def count_platform_scans(path: Path) -> Any:
        nonlocal scans
        if path == platform_directory:
            scans += 1
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", count_platform_scans)

    snapshots = ItemStore(items_root).iter_sources()

    assert len(snapshots) == 6
    assert scans == 1


def test_casefold_index_refreshes_after_another_store_writes(tmp_path: Path) -> None:
    items_root = tmp_path / "items"
    first_store = ItemStore(items_root)
    assert first_store.read_source("x", "missing") is None

    ItemStore(items_root).write(replace(captured(), source_id="ABC"))

    with pytest.raises(ValueError, match="case-insensitive"):
        first_store.write(replace(captured(), source_id="abc"))

    source = items_root / "x" / "ABC" / "source.json"
    assert json.loads(source.read_text(encoding="utf-8"))["source_id"] == "ABC"


def test_item_store_rejects_root_reparse_alias_at_all_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x" / "42").mkdir(parents=True)
    (outside / "x" / "42" / "source.json").write_text(
        json.dumps(captured().to_source_dict()),
        encoding="utf-8",
    )
    items_root = tmp_path / "items"
    try:
        items_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        items_root.mkdir()
        original_is_junction = Path.is_junction

        def treat_root_as_junction(path: Path) -> bool:
            return path == items_root or original_is_junction(path)

        monkeypatch.setattr(Path, "is_junction", treat_root_as_junction)

    store = ItemStore(items_root)
    with pytest.raises(ValueError, match="reparse"):
        store.read_source("x", "42")
    with pytest.raises(ValueError, match="reparse"):
        store.iter_sources()


@pytest.mark.parametrize(
    ("snapshot", "cause_type"),
    [
        ("{", json.JSONDecodeError),
        ("[]", TypeError),
        (json.dumps({"platform": "x"}), KeyError),
    ],
)
def test_iter_sources_reports_corrupt_snapshot_with_path_and_cause(
    tmp_path: Path,
    snapshot: str,
    cause_type: type[BaseException],
) -> None:
    source = tmp_path / "items" / "x" / "42" / "source.json"
    source.parent.mkdir(parents=True)
    source.write_text(snapshot, encoding="utf-8")

    with pytest.raises(SourceSnapshotError) as error:
        ItemStore(tmp_path / "items").iter_sources()

    assert error.value.path == source
    assert str(source) in str(error.value)
    assert isinstance(error.value.__cause__, cause_type)


def source_snapshot(
    tmp_path: Path,
    snapshot: dict[str, Any],
    *,
    platform: str = "x",
    source_id: str = "42",
) -> Path:
    source = tmp_path / "items" / platform / source_id / "source.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    return source


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("platform", 7),
        ("platform", "mastodon"),
        ("source_id", 42),
        ("source_id", "unsafe/id"),
        ("canonical_url", 7),
        ("canonical_url", "not-a-url"),
        ("title", 7),
        ("title", "   "),
        ("author", 7),
        ("body", 7),
        ("collections", "Research"),
        ("collections", ["Research", 7]),
        ("extractor_version", 7),
        ("extractor_version", "   "),
        ("published_at", 7),
        ("published_at", "2026-01-02T00:00:00"),
        ("observed_at", 7),
        ("observed_at", "not-a-timestamp"),
        ("content_hash", 7),
    ],
)
def test_iter_sources_rejects_invalid_snapshot_fields(
    tmp_path: Path, field: str, value: Any
) -> None:
    snapshot = captured().to_source_dict()
    snapshot[field] = value
    source = source_snapshot(tmp_path, snapshot)

    with pytest.raises(SourceSnapshotError) as error:
        ItemStore(tmp_path / "items").iter_sources()

    assert error.value.path == source
    assert field in str(error.value)
    assert error.value.__cause__ is not None


def test_iter_sources_rejects_content_hash_mismatch(tmp_path: Path) -> None:
    snapshot = captured().to_source_dict()
    snapshot["body"] = "Tampered body"
    source = source_snapshot(tmp_path, snapshot)

    with pytest.raises(SourceSnapshotError, match="content_hash") as error:
        ItemStore(tmp_path / "items").iter_sources()

    assert error.value.path == source


@pytest.mark.parametrize(
    ("directory_platform", "directory_source_id", "field"),
    [("x", "99", "source_id"), ("bilibili", "42", "platform")],
)
def test_iter_sources_rejects_parent_directory_metadata_mismatch(
    tmp_path: Path,
    directory_platform: str,
    directory_source_id: str,
    field: str,
) -> None:
    source = source_snapshot(
        tmp_path,
        captured().to_source_dict(),
        platform=directory_platform,
        source_id=directory_source_id,
    )

    with pytest.raises(SourceSnapshotError, match=field) as error:
        ItemStore(tmp_path / "items").iter_sources()

    assert error.value.path == source


def test_atomic_text_closes_descriptor_if_fdopen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor: int | None = None

    def fail_fdopen(raw_descriptor: int, mode: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal descriptor
        descriptor = raw_descriptor
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)

    with pytest.raises(OSError, match="injected fdopen failure"):
        ItemStore._atomic_text(tmp_path / "content.md", "body")

    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert list(tmp_path.iterdir()) == []


def test_atomic_text_cleans_temp_if_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        ItemStore._atomic_text(tmp_path / "content.md", "body")

    assert list(tmp_path.iterdir()) == []


def test_atomic_text_syncs_parent_directory_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[Path] = []

    def record_sync(directory: Path) -> None:
        synced.append(directory)

    monkeypatch.setattr(item_store_module, "_fsync_directory", record_sync, raising=False)

    ItemStore._atomic_text(tmp_path / "content.md", "body")

    assert synced == [tmp_path]


@pytest.mark.parametrize("error_number", [errno.EINVAL, errno.ENOTSUP, errno.ENOSYS])
def test_fsync_directory_treats_unsupported_posix_open_errors_as_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    monkeypatch.setattr(os, "name", "posix")

    def fail_open(path: str | os.PathLike[str], flags: int) -> int:
        raise OSError(error_number, "unsupported directory fsync")

    monkeypatch.setattr(os, "open", fail_open)

    item_store_module._fsync_directory(tmp_path)


def test_fsync_directory_propagates_posix_open_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "posix")

    def fail_open(path: str | os.PathLike[str], flags: int) -> int:
        raise PermissionError("permission denied")

    monkeypatch.setattr(os, "open", fail_open)

    with pytest.raises(PermissionError, match="permission denied"):
        item_store_module._fsync_directory(tmp_path)


def test_fsync_directory_propagates_posix_fsync_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "open", lambda path, flags: 123)
    monkeypatch.setattr(os, "close", lambda descriptor: None)

    def fail_fsync(descriptor: int) -> None:
        raise OSError(errno.EIO, "I/O failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="I/O failure"):
        item_store_module._fsync_directory(tmp_path)


def test_fsync_directory_is_a_windows_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")

    def fail_open(path: str | os.PathLike[str], flags: int) -> int:
        raise AssertionError("Windows directory fsync must be a no-op")

    monkeypatch.setattr(os, "open", fail_open)

    item_store_module._fsync_directory(tmp_path)


OCR_MD = "# OCR\n\n图中文字 学习卡片\n"


def x_item_with_ocr() -> CapturedItem:
    return CapturedItem(
        platform="x",
        source_id="1232164438310380159",
        canonical_url="https://x.com/example/status/1232164438310380159",
        title="带图推文",
        author="示例名称10",
        published_at=datetime(2026, 7, 20, tzinfo=UTC),
        observed_at=datetime(2026, 7, 26, tzinfo=UTC),
        body="正文",
        collections=(),
        extractor_version="x-browser-v1",
        platform_metadata={"media": [{"type": "photo", "ocr_status": "available"}]},
        assets=(CapturedAsset("ocr/0001.md", "text/markdown", OCR_MD, sha256_text(OCR_MD)),),
    )


def test_write_persists_ocr_assets_and_indexes_them(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    result = store.write(x_item_with_ocr())
    assert (result.directory / "ocr" / "0001.md").read_text("utf-8") == OCR_MD
    entries = dict(store.iter_index_markdown("x", "1232164438310380159"))
    assert "ocr/0001.md" in entries
    assert "content.md" in entries
    snapshots = ItemStore(tmp_path / "items").iter_sources()
    assert len(snapshots) == 1
    assert snapshots[0]["assets"][0]["relative_path"] == "ocr/0001.md"


ENRICHMENT = {
    "summary": "介绍 hybrid retrieval 的实现思路与踩坑记录。",
    "tags": ["retrieval", "混合检索", "fts5"],
    "content_type": "text",
    "provider": "agent",
    "model": "claude-fable-5",
    "generated_at": "2026-07-26T12:00:00Z",
    "input_hash": "b" * 64,
}


def test_apply_enrichment_renders_summary_and_survives_repair(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    stored = store.write(captured())
    notes = stored.directory / "notes.md"
    notes.write_text("durable\n", encoding="utf-8")
    original_hash = json.loads((stored.directory / "source.json").read_text("utf-8"))[
        "content_hash"
    ]

    store.apply_enrichment("x", "42", ENRICHMENT)

    snapshot = json.loads((stored.directory / "source.json").read_text("utf-8"))
    assert snapshot["enrichment"]["tags"] == ["retrieval", "混合检索", "fts5"]
    assert snapshot["content_hash"] == original_hash
    content = (stored.directory / "content.md").read_text("utf-8")
    assert "## 摘要" in content
    assert "hybrid retrieval" in content
    assert "标签：retrieval · 混合检索 · fts5" in content
    assert notes.read_text("utf-8") == "durable\n"

    # content.md stays deterministically rebuildable from source.json.
    assert store.ensure_content_from_source("x", "42") is False
    (stored.directory / "content.md").write_text("tampered", encoding="utf-8")
    assert store.ensure_content_from_source("x", "42") is True
    assert "## 摘要" in (stored.directory / "content.md").read_text("utf-8")

    snapshots = ItemStore(tmp_path / "items").iter_sources()
    assert snapshots[0]["enrichment"]["summary"].startswith("介绍 hybrid")


def test_apply_enrichment_unknown_item_raises(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    with pytest.raises(KeyError, match="unknown item"):
        store.apply_enrichment("x", "missing", ENRICHMENT)


def test_apply_enrichment_rejects_malformed_block(tmp_path: Path) -> None:
    store = ItemStore(tmp_path / "items")
    stored = store.write(captured())
    before = (stored.directory / "source.json").read_bytes()
    bad = dict(ENRICHMENT, content_type="audio")
    with pytest.raises(ValueError):
        store.apply_enrichment("x", "42", bad)
    assert (stored.directory / "source.json").read_bytes() == before


def test_iter_sources_rejects_malformed_enrichment_on_disk(tmp_path: Path) -> None:
    snapshot = captured().to_source_dict()
    snapshot["enrichment"] = {"summary": 5}
    source = source_snapshot(tmp_path, snapshot)
    with pytest.raises(SourceSnapshotError) as error:
        ItemStore(tmp_path / "items").iter_sources()
    assert error.value.path == source
