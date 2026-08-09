import json
import shutil
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from favhub.database import Database
from favhub.domain import CapturedAsset, CapturedItem, sha256_text
from favhub.enrichment_queue import EnrichmentQueue
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.library import BatchReceipt, LibraryModule


@pytest.fixture
def library_components(
    tmp_path: Path,
) -> Iterator[tuple[LibraryModule, Database, ItemStore]]:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    try:
        yield library, database, store
    finally:
        database.close()


def create_job(database: Database, job_id: str, *, mode: str = "incremental") -> None:
    timestamp = "2026-07-18T00:00:00Z"
    database.connection.execute(
        """
        INSERT INTO sync_jobs (
            id, mode, status, options_json, created_at, updated_at
        ) VALUES (?, ?, 'running', '{}', ?, ?)
        """,
        (job_id, mode, timestamp, timestamp),
    )


def captured(
    body: str,
    *,
    platform: str = "x",
    source_id: str = "42",
    collections: tuple[str, ...] = ("Research",),
    extractor_version: str = "fixture-v1",
) -> CapturedItem:
    return CapturedItem(
        platform=platform,
        source_id=source_id,
        canonical_url=f"https://example.com/{platform}/{source_id}",
        title=f"Saved item {source_id}",
        author="example",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body=body,
        collections=collections,
        extractor_version=extractor_version,
    )


def test_ingest_persists_bilibili_assets_and_enqueues_index(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "job-b", mode="full")
    transcript = "# Transcript\n\n[00:00] hi\n"
    raw = '{"body": [{"from": 0.0, "to": 1.0, "content": "hi"}]}'
    item = CapturedItem(
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
        assets=(
            CapturedAsset(
                "transcript/0001.md", "text/markdown", transcript, sha256_text(transcript)
            ),
            CapturedAsset("assets/subtitles/zh.json", "application/json", raw, sha256_text(raw)),
        ),
    )

    receipt = library.ingest_batch("job-b", "bilibili", "batch-1", [item], True)

    assert receipt.added == 1
    directory = store.items_root / "bilibili" / "BV1aa411c7mD"
    assert (directory / "transcript" / "0001.md").exists()
    assert (directory / "assets" / "subtitles" / "zh.json").exists()
    pending = database.connection.execute(
        "SELECT COUNT(*) AS count FROM enrichment_tasks "
        "WHERE platform = 'bilibili' AND kind = 'index_content'"
    ).fetchone()
    assert pending["count"] == 1


def test_replaying_batch_returns_persisted_receipt_without_duplicate_work(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-1")

    first = library.ingest_batch("job-1", "x", "batch-1", [captured("one")], False)
    replay = library.ingest_batch("job-1", "x", "batch-1", [captured("one")], False)

    assert replay == first
    assert first.added == 1
    assert first.refreshed == 0
    assert first.duplicates == 0
    assert first.out_of_range == 0
    assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM enrichment_tasks WHERE kind = 'index_content'"
        ).fetchone()[0]
        == 1
    )

    batch = database.connection.execute(
        """
        SELECT receipt_id, receipt_json
        FROM sync_batches
        WHERE job_id = 'job-1' AND platform = 'x' AND idempotency_key = 'batch-1'
        """
    ).fetchone()
    assert batch is not None
    assert batch["receipt_id"] == first.receipt_id
    payload = json.loads(batch["receipt_json"])
    assert payload == asdict(first)
    assert BatchReceipt(**payload) == first


def test_ingest_batch_joins_a_caller_owned_transaction(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-1")

    with database.transaction():
        receipt = library.ingest_batch("job-1", "x", "batch-1", [captured("one")], False)

    assert receipt.added == 1
    assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1


def test_ingest_batch_keeps_caller_transaction_atomic_on_rollback(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-1")

    with pytest.raises(RuntimeError, match="rollback"), database.transaction():
        library.ingest_batch("job-1", "x", "batch-1", [captured("one")], False)
        raise RuntimeError("rollback")

    assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert database.connection.execute("SELECT COUNT(*) FROM sync_batches").fetchone()[0] == 0


def test_incremental_duplicates_and_full_refreshes_changed_content(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job")
    create_job(database, "incremental-job")
    create_job(database, "full-job", mode="full")
    original = captured("one")
    changed = captured("two")

    added = library.ingest_batch("initial-job", "x", "initial-batch", [original], False)
    original_index_hash = store.index_fingerprint("x", "42")
    initial_row = database.connection.execute(
        """SELECT first_seen_at, index_input_hash FROM items
           WHERE platform = 'x' AND source_id = '42'"""
    ).fetchone()
    assert initial_row is not None
    assert initial_row["index_input_hash"] == original_index_hash
    first_seen_at = initial_row["first_seen_at"]
    notes = store.items_root / "x" / "42" / "notes.md"
    notes.write_text("My durable note\n", encoding="utf-8")

    duplicate = library.ingest_batch("incremental-job", "x", "incremental-batch", [changed], False)

    content = store.items_root / "x" / "42" / "content.md"
    assert added.added == 1
    assert duplicate.duplicates == 1
    assert duplicate.added == 0
    assert duplicate.refreshed == 0
    assert "one" in content.read_text(encoding="utf-8")
    assert "two" not in content.read_text(encoding="utf-8")

    refreshed = library.ingest_batch("full-job", "x", "full-batch", [changed], True)

    assert refreshed.refreshed == 1
    assert refreshed.added == 0
    assert refreshed.duplicates == 0
    assert "two" in content.read_text(encoding="utf-8")
    assert notes.read_text(encoding="utf-8") == "My durable note\n"
    item_row = database.connection.execute(
        """
        SELECT content_hash, first_seen_at, last_full_synced_at, index_input_hash
        FROM items
        WHERE platform = 'x' AND source_id = '42'
        """
    ).fetchone()
    assert item_row is not None
    assert item_row["content_hash"] == changed.content_hash
    assert item_row["index_input_hash"] == store.index_fingerprint("x", "42")
    assert item_row["first_seen_at"] == first_seen_at
    assert first_seen_at == "2026-07-18T00:00:00Z"
    assert item_row["last_full_synced_at"] is not None
    assert parse_timestamp(item_row["last_full_synced_at"]).utcoffset() == timedelta(0)
    hashes = {
        row["input_hash"]
        for row in database.connection.execute(
            """
            SELECT input_hash
            FROM enrichment_tasks
            WHERE platform = 'x' AND source_id = '42' AND kind = 'index_content'
            """
        ).fetchall()
    }
    assert hashes == {original_index_hash, store.index_fingerprint("x", "42")}

    newly_seen = captured("brand new", source_id="43")
    new_receipt = library.ingest_batch("full-job", "x", "full-new-batch", [newly_seen], True)
    assert new_receipt.added == 1
    new_row = database.connection.execute(
        """
        SELECT first_seen_at, last_full_synced_at
        FROM items
        WHERE platform = 'x' AND source_id = '43'
        """
    ).fetchone()
    assert new_row is not None
    assert new_row["first_seen_at"] == iso_timestamp(newly_seen.observed_at)
    assert new_row["last_full_synced_at"] != new_row["first_seen_at"]


def test_full_metadata_refresh_enqueues_changed_aggregate_and_can_reindex(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job")
    create_job(database, "full-job", mode="full")
    original = captured("one", collections=("A",), extractor_version="fixture-v1")
    metadata_changed = replace(
        original,
        collections=("B",),
        extractor_version="fixture-v2",
        observed_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    library.ingest_batch("initial-job", "x", "initial-batch", [original], False)
    original_index_hash = store.index_fingerprint("x", "42")
    notes = store.items_root / "x" / "42" / "notes.md"
    notes.write_text("durable note\n", encoding="utf-8")

    receipt = library.ingest_batch("full-job", "x", "full-batch", [metadata_changed], True)

    assert metadata_changed.content_hash == original.content_hash
    assert receipt.refreshed == 1
    assert receipt.duplicates == 0
    source_path = store.items_root / "x" / "42" / "source.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    assert source["collections"] == ["B"]
    assert source["extractor_version"] == "fixture-v2"
    content = (store.items_root / "x" / "42" / "content.md").read_text(encoding="utf-8")
    assert 'collections: ["B"]' in content
    assert notes.read_text(encoding="utf-8") == "durable note\n"
    current_index_hash = store.index_fingerprint("x", "42")
    assert current_index_hash != original_index_hash
    item_hash = database.connection.execute(
        "SELECT index_input_hash FROM items WHERE platform='x' AND source_id='42'"
    ).fetchone()[0]
    assert item_hash == current_index_hash
    task_hashes = {
        row[0]
        for row in database.connection.execute(
            """SELECT input_hash FROM enrichment_tasks
               WHERE platform='x' AND source_id='42' AND kind='index_content'"""
        )
    }
    assert task_hashes == {original_index_hash, current_index_hash}

    indexer = ContentIndexer(database, store, library.queue)
    assert indexer.index_next() is not None
    assert indexer.index_next() is not None
    indexed = database.connection.execute(
        "SELECT input_hash, text FROM content_chunks ORDER BY ordinal"
    ).fetchall()
    assert indexed and {row["input_hash"] for row in indexed} == {current_index_hash}
    assert 'collections: ["B"]' in "\n".join(str(row["text"]) for row in indexed)


def test_full_observation_time_only_change_remains_duplicate(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job")
    create_job(database, "full-job", mode="full")
    original = captured("one")
    observed_later = replace(original, observed_at=datetime(2026, 7, 19, tzinfo=UTC))
    library.ingest_batch("initial-job", "x", "initial-batch", [original], False)
    source_path = store.items_root / "x" / "42" / "source.json"
    source_before = source_path.read_text(encoding="utf-8")

    receipt = library.ingest_batch("full-job", "x", "full-batch", [observed_later], True)

    assert receipt.refreshed == 0
    assert receipt.duplicates == 1
    assert source_path.read_text(encoding="utf-8") == source_before
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM enrichment_tasks WHERE kind = 'index_content'"
        ).fetchone()[0]
        == 1
    )


def test_platform_mismatch_is_rejected_before_any_side_effect(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "job-1")

    with pytest.raises(ValueError, match="platform"):
        library.ingest_batch(
            "job-1",
            "x",
            "batch-1",
            [
                captured("valid", source_id="41"),
                captured("wrong platform", platform="bilibili", source_id="42"),
            ],
            False,
        )

    assert not store.items_root.exists()
    for table in ("items", "enrichment_tasks", "sync_batches"):
        assert database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_unchanged_full_item_is_duplicate_without_rewrite_or_new_task(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job")
    create_job(database, "full-job", mode="full")
    item = captured("one")
    library.ingest_batch("initial-job", "x", "initial-batch", [item], False)
    content = store.items_root / "x" / "42" / "content.md"
    original_content = content.read_text(encoding="utf-8")
    source = store.items_root / "x" / "42" / "source.json"
    original_source = source.read_text(encoding="utf-8")

    receipt = library.ingest_batch("full-job", "x", "full-batch", [item], True)

    assert receipt.added == 0
    assert receipt.refreshed == 0
    assert receipt.duplicates == 1
    assert content.read_text(encoding="utf-8") == original_content
    assert source.read_text(encoding="utf-8") == original_source
    row = database.connection.execute(
        """
        SELECT first_seen_at, last_full_synced_at
        FROM items
        WHERE platform = 'x' AND source_id = '42'
        """
    ).fetchone()
    assert row is not None
    assert row["last_full_synced_at"] is not None
    assert parse_timestamp(row["first_seen_at"]).utcoffset() == timedelta(0)
    assert parse_timestamp(row["last_full_synced_at"]).utcoffset() == timedelta(0)
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM enrichment_tasks WHERE kind = 'index_content'"
        ).fetchone()[0]
        == 1
    )


def test_full_same_content_rebuilds_missing_system_files_without_new_task(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job")
    create_job(database, "full-job", mode="full")
    item = captured("one")
    library.ingest_batch("initial-job", "x", "initial-batch", [item], False)
    item_directory = store.items_root / "x" / "42"
    shutil.rmtree(item_directory)
    database.connection.execute(
        "UPDATE items SET access_status = 'missing' WHERE platform = 'x' AND source_id = '42'"
    )

    receipt = library.ingest_batch("full-job", "x", "full-batch", [item], True)

    assert receipt.refreshed == 1
    assert receipt.duplicates == 0
    assert (item_directory / "source.json").is_file()
    assert (item_directory / "content.md").is_file()
    row = database.connection.execute(
        "SELECT access_status FROM items WHERE platform = 'x' AND source_id = '42'"
    ).fetchone()
    assert row is not None and row["access_status"] == "available"
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM enrichment_tasks WHERE kind = 'index_content'"
        ).fetchone()[0]
        == 1
    )


def test_full_asset_only_change_refreshes_snapshot(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job")
    create_job(database, "full-job", mode="full")
    item = captured("tweet text", source_id="77")
    library.ingest_batch("initial-job", "x", "initial-batch", [item], False)
    before_index_hash = database.connection.execute(
        "SELECT index_input_hash FROM items WHERE platform='x' AND source_id='77'"
    ).fetchone()[0]

    description = "图中文字：hello\n\n画面：截图。\n"
    enriched = replace(
        item,
        assets=(
            CapturedAsset("ocr/0001.md", "text/markdown", description, sha256_text(description)),
        ),
    )
    assert enriched.content_hash == item.content_hash

    receipt = library.ingest_batch("full-job", "x", "full-batch", [enriched], True)

    assert receipt.refreshed == 1
    assert receipt.duplicates == 0
    directory = store.items_root / "x" / "77"
    assert (directory / "ocr" / "0001.md").read_text(encoding="utf-8") == description
    snapshot = store.read_source("x", "77")
    assert snapshot is not None
    assert [a["relative_path"] for a in snapshot["assets"]] == ["ocr/0001.md"]
    row = database.connection.execute(
        "SELECT index_input_hash FROM items WHERE platform='x' AND source_id='77'"
    ).fetchone()
    assert row["index_input_hash"] != before_index_hash


def test_asset_only_refresh_preserves_enrichment_block(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "initial-job", mode="full")
    create_job(database, "full-job", mode="full")
    item = captured("tweet text", source_id="88")
    library.ingest_batch("initial-job", "x", "initial-batch", [item], True)
    queue = EnrichmentQueue(database)
    task = queue.claim_next(kind="summarize")
    assert task is not None
    assert library.apply_enrichment(task.id, ENRICH_FIELDS) == "applied"

    description = "图中文字：hello\n"
    enriched = replace(
        item,
        assets=(
            CapturedAsset("ocr/0001.md", "text/markdown", description, sha256_text(description)),
        ),
    )
    receipt = library.ingest_batch("full-job", "x", "full-batch", [enriched], True)

    assert receipt.refreshed == 1
    snapshot = store.read_source("x", "88")
    assert snapshot is not None
    assert snapshot["enrichment"]["input_hash"] == item.content_hash
    content = (store.items_root / "x" / "88" / "content.md").read_text(encoding="utf-8")
    assert "## 摘要" in content


def test_database_changes_roll_back_together_when_enqueue_fails(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")

    class FailingQueue(EnrichmentQueue):
        def enqueue(
            self,
            platform: str,
            source_id: str,
            kind: str,
            input_hash: str,
        ) -> str:
            super().enqueue(platform, source_id, kind, input_hash)
            raise RuntimeError("injected enqueue failure")

    library = LibraryModule(database, store, FailingQueue(database))
    try:
        create_job(database, "job-1")

        with pytest.raises(RuntimeError, match="injected enqueue failure"):
            library.ingest_batch("job-1", "x", "batch-1", [captured("one")], False)

        for table in ("items", "enrichment_tasks", "sync_batches"):
            assert database.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        database.close()


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


ENRICH_FIELDS = {
    "summary": "总结 hybrid retrieval 的关键结论与实现要点。",
    "tags": ["retrieval", "检索"],
    "content_type": "text",
    "provider": "agent",
    "model": "claude-fable-5",
}


def test_redo_enrichment_requeues_one_models_output_and_leaves_the_rest(
    library_components,
) -> None:
    """A batch can be bad in a way only a reader catches, and completed tasks
    have no way back — backfill only covers items that never had one."""
    library, database, _store = library_components
    create_job(database, "job-1", mode="full")
    cheap = captured("便宜模型写的", source_id="cheap")
    good = captured("另一个模型写的", source_id="good")
    library.ingest_batch("job-1", "x", "batch", [cheap, good], True)
    queue = EnrichmentQueue(database)
    for _ in range(2):
        task = queue.claim_next(kind="summarize")
        assert task is not None
        model = "cheap-model" if task.source_id == "cheap" else "claude-fable-5"
        library.apply_enrichment(task.id, {**ENRICH_FIELDS, "model": model})

    result = library.redo_enrichment("cheap-model")

    assert result == {"matched": 1, "requeued": 1}
    statuses = {
        str(row["source_id"]): str(row["status"])
        for row in database.connection.execute(
            "SELECT source_id, status FROM enrichment_tasks WHERE kind='summarize'"
        )
    }
    assert statuses == {"cheap": "pending", "good": "completed"}
    # Claiming now hands back exactly the item whose batch was rejected.
    reclaimed = queue.claim_next(kind="summarize")
    assert reclaimed is not None
    assert reclaimed.source_id == "cheap"


def test_redo_declined_reopens_every_refusal(library_components) -> None:
    """Declining is a judgement an agent can get wrong, and it is one-way.

    Seven Bilibili items were refused for having a subtitle that did not match
    their title, two of them carrying descriptions of nearly three thousand
    characters that were perfectly summarisable.
    """
    library, database, _store = library_components
    create_job(database, "job-1", mode="full")
    items = [captured("正文", source_id=str(index)) for index in range(3)]
    library.ingest_batch("job-1", "x", "batch", items, True)
    queue = EnrichmentQueue(database)
    for _ in range(2):
        task = queue.claim_next(kind="summarize")
        assert task is not None
        queue.decline(task.id, "content_unsupported: 看着不像有内容")
    kept = queue.claim_next(kind="summarize")
    assert kept is not None
    library.apply_enrichment(kept.id, ENRICH_FIELDS)

    assert library.redo_declined_enrichment() == {"requeued": 2}

    statuses = sorted(
        str(row["status"])
        for row in database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE kind='summarize'"
        )
    )
    # The two refusals are back; the one that succeeded is left alone.
    assert statuses == ["completed", "pending", "pending"]


def test_redo_declined_is_harmless_when_nothing_was_refused(library_components) -> None:
    library, database, _store = library_components
    create_job(database, "job-1", mode="full")
    library.ingest_batch("job-1", "x", "batch", [captured("正文", source_id="1")], True)

    assert library.redo_declined_enrichment() == {"requeued": 0}


def test_redo_enrichment_matches_nothing_for_an_unused_model(library_components) -> None:
    library, database, _store = library_components
    create_job(database, "job-1", mode="full")
    library.ingest_batch("job-1", "x", "batch", [captured("正文", source_id="1")], True)
    queue = EnrichmentQueue(database)
    task = queue.claim_next(kind="summarize")
    assert task is not None
    library.apply_enrichment(task.id, ENRICH_FIELDS)

    assert library.redo_enrichment("some-other-model") == {"matched": 0, "requeued": 0}


def _summarize_tasks(database: Database) -> list[tuple[str, str]]:
    return [
        (str(row["input_hash"]), str(row["status"]))
        for row in database.connection.execute(
            "SELECT input_hash, status FROM enrichment_tasks "
            "WHERE kind = 'summarize' ORDER BY created_at"
        )
    ]


def test_ingest_enqueues_summarize_keyed_by_content_hash(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-s", mode="full")
    item = captured("one")

    library.ingest_batch("job-s", "x", "b1", [item], True)
    assert _summarize_tasks(database) == [(item.content_hash, "pending")]

    # Unchanged re-sync does not create another summarize task.
    create_job(database, "job-s2", mode="full")
    library.ingest_batch("job-s2", "x", "b1", [item], True)
    assert _summarize_tasks(database) == [(item.content_hash, "pending")]

    # Changed content enqueues a task for the new hash.
    changed = captured("two")
    create_job(database, "job-s3", mode="full")
    library.ingest_batch("job-s3", "x", "b1", [changed], True)
    hashes = [h for h, _ in _summarize_tasks(database)]
    assert hashes == [item.content_hash, changed.content_hash]


def test_apply_enrichment_completes_task_and_reindexes(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "job-a", mode="full")
    item = captured("body to summarize")
    library.ingest_batch("job-a", "x", "b1", [item], True)
    queue = EnrichmentQueue(database)
    task = queue.claim_next(kind="summarize")
    assert task is not None and task.input_hash == item.content_hash
    before_index_hash = database.connection.execute(
        "SELECT index_input_hash FROM items WHERE platform='x' AND source_id='42'"
    ).fetchone()[0]

    outcome = library.apply_enrichment(task.id, ENRICH_FIELDS)

    assert outcome == "applied"
    snapshot = store.read_source("x", "42")
    assert snapshot is not None
    assert snapshot["enrichment"]["summary"].startswith("总结 hybrid")
    assert snapshot["enrichment"]["input_hash"] == item.content_hash
    assert snapshot["enrichment"]["provider"] == "agent"
    row = database.connection.execute(
        "SELECT content_type, index_input_hash FROM items WHERE platform='x' AND source_id='42'"
    ).fetchone()
    assert row["content_type"] == "text"
    assert row["index_input_hash"] != before_index_hash
    status = database.connection.execute(
        "SELECT status FROM enrichment_tasks WHERE id = ?", (task.id,)
    ).fetchone()[0]
    assert status == "completed"
    pending_index = database.connection.execute(
        "SELECT COUNT(*) FROM enrichment_tasks "
        "WHERE kind='index_content' AND status='pending' AND input_hash = ?",
        (row["index_input_hash"],),
    ).fetchone()[0]
    assert pending_index == 1
    assert "## 摘要" in (store.items_root / "x" / "42" / "content.md").read_text("utf-8")


def test_apply_enrichment_stale_task_is_superseded(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, store = library_components
    create_job(database, "job-b", mode="full")
    original = captured("original")
    library.ingest_batch("job-b", "x", "b1", [original], True)
    queue = EnrichmentQueue(database)
    task = queue.claim_next(kind="summarize")
    assert task is not None

    # Content changes while the Agent is still working on the old task.
    create_job(database, "job-b2", mode="full")
    library.ingest_batch("job-b2", "x", "b2", [captured("rewritten")], True)

    outcome = library.apply_enrichment(task.id, ENRICH_FIELDS)

    assert outcome == "stale"
    status = database.connection.execute(
        "SELECT status FROM enrichment_tasks WHERE id = ?", (task.id,)
    ).fetchone()[0]
    assert status == "completed"
    snapshot = store.read_source("x", "42")
    assert snapshot is not None and "enrichment" not in snapshot


def test_apply_enrichment_rejects_bad_task_states(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-c", mode="full")
    library.ingest_batch("job-c", "x", "b1", [captured("one")], True)
    queue = EnrichmentQueue(database)

    with pytest.raises(KeyError):
        library.apply_enrichment("no-such-task", ENRICH_FIELDS)

    pending_id = database.connection.execute(
        "SELECT id FROM enrichment_tasks WHERE kind='summarize'"
    ).fetchone()[0]
    with pytest.raises(ValueError, match="running"):
        library.apply_enrichment(str(pending_id), ENRICH_FIELDS)

    index_task = queue.claim_next(kind="index_content")
    assert index_task is not None
    with pytest.raises(ValueError, match="summarize"):
        library.apply_enrichment(index_task.id, ENRICH_FIELDS)


def test_ingest_lifts_favorited_at_into_column(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-f", mode="full")
    item = CapturedItem(
        platform="bilibili",
        source_id="BV1FAVTIME01",
        canonical_url="https://www.bilibili.com/video/BV1FAVTIME01",
        title="t",
        author=None,
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="b",
        collections=(),
        extractor_version="v1",
        platform_metadata={"favorited_at": "2026-06-01T00:00:00Z"},
    )
    plain = captured("no-fav", source_id="43")

    library.ingest_batch("job-f", "bilibili", "b1", [item], True)
    library.ingest_batch("job-f", "x", "b2", [plain], True)

    rows = {
        str(r["source_id"]): r["favorited_at"]
        for r in database.connection.execute("SELECT source_id, favorited_at FROM items")
    }
    assert rows["BV1FAVTIME01"] == "2026-06-01T00:00:00Z"
    assert rows["43"] is None


def test_backfill_favorited_at_lifts_from_snapshots(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components
    create_job(database, "job-g", mode="full")
    item = CapturedItem(
        platform="bilibili",
        source_id="BV1FAVTIME02",
        canonical_url="https://www.bilibili.com/video/BV1FAVTIME02",
        title="t",
        author=None,
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="b",
        collections=(),
        extractor_version="v1",
        platform_metadata={"favorited_at": "2026-05-05T00:00:00Z"},
    )
    library.ingest_batch("job-g", "bilibili", "b1", [item], True)
    with database.transaction():
        database.connection.execute("UPDATE items SET favorited_at = NULL")

    first = library.backfill_favorited_at()
    assert first == {"updated": 1, "unchanged": 0}
    second = library.backfill_favorited_at()
    assert second == {"updated": 0, "unchanged": 1}


def test_refresh_keeps_earliest_favorited_at(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _ = library_components

    def fav_item(body: str, favorited_at: str | None) -> CapturedItem:
        metadata = {"favorited_at": favorited_at} if favorited_at else None
        return CapturedItem(
            platform="bilibili",
            source_id="BV1FAVKEEP01",
            canonical_url="https://www.bilibili.com/video/BV1FAVKEEP01",
            title="t",
            author=None,
            published_at=datetime(2026, 1, 2, tzinfo=UTC),
            observed_at=datetime(2026, 7, 18, tzinfo=UTC),
            body=body,
            collections=(),
            extractor_version="v1",
            platform_metadata=metadata,
        )

    def column() -> str | None:
        return database.connection.execute(
            "SELECT favorited_at FROM items WHERE source_id='BV1FAVKEEP01'"
        ).fetchone()[0]

    create_job(database, "job-k1", mode="full")
    library.ingest_batch("job-k1", "bilibili", "b1", [fav_item("v1", "2026-03-01T00:00:00Z")], True)
    assert column() == "2026-03-01T00:00:00Z"

    # A later refresh with a newer favorited_at keeps the earliest value.
    create_job(database, "job-k2", mode="full")
    library.ingest_batch("job-k2", "bilibili", "b1", [fav_item("v2", "2026-07-01T00:00:00Z")], True)
    assert column() == "2026-03-01T00:00:00Z"

    # A refresh lacking the metadata never NULLs a known value.
    create_job(database, "job-k3", mode="full")
    library.ingest_batch("job-k3", "bilibili", "b1", [fav_item("v3", None)], True)
    assert column() == "2026-03-01T00:00:00Z"

    # An earlier value (older capture surfacing) moves it back.
    create_job(database, "job-k4", mode="full")
    library.ingest_batch("job-k4", "bilibili", "b1", [fav_item("v4", "2026-01-15T00:00:00Z")], True)
    assert column() == "2026-01-15T00:00:00Z"


def _access_status_of(database: Database, source_id: str) -> str:
    row = database.connection.execute(
        "SELECT access_status FROM items WHERE platform = 'x' AND source_id = ?",
        (source_id,),
    ).fetchone()
    assert row is not None
    return str(row["access_status"])


def test_an_item_the_platform_no_longer_serves_is_stored_as_unavailable(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _store = library_components
    create_job(database, "job-gone", mode="full")
    item = replace(
        captured("only a tombstone was left", source_id="gone"),
        platform_metadata={"source_status": "source_unavailable"},
    )

    library.ingest_batch("job-gone", "x", "batch-1", [item], True)

    # Every search path filters on this column, so marking it is what keeps a
    # shell with no content out of results.
    assert _access_status_of(database, "gone") == "unavailable"


def test_a_transient_failure_never_buries_a_live_item(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _store = library_components
    create_job(database, "job-flaky", mode="full")
    live = captured("real content", source_id="flaky")
    library.ingest_batch("job-flaky", "x", "batch-1", [live], True)
    assert _access_status_of(database, "flaky") == "available"

    # Rate limiting says nothing about whether the source still exists. Reading
    # it as "gone" would delete a healthy item from every search result on a
    # bad afternoon, with nothing anywhere to say why.
    throttled = replace(
        captured("real content", source_id="flaky"),
        platform_metadata={"source_status": "rate_limited"},
    )
    library.ingest_batch("job-flaky", "x", "batch-2", [throttled], True)
    assert _access_status_of(database, "flaky") == "available"


def test_a_transient_failure_also_never_revives_a_dead_item(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _store = library_components
    create_job(database, "job-dead", mode="full")
    gone = replace(
        captured("tombstone", source_id="dead"),
        platform_metadata={"source_status": "source_unavailable"},
    )
    library.ingest_batch("job-dead", "x", "batch-1", [gone], True)

    throttled = replace(
        captured("tombstone", source_id="dead"),
        platform_metadata={"source_status": "rate_limited"},
    )
    library.ingest_batch("job-dead", "x", "batch-2", [throttled], True)

    # The verdict is withheld in both directions: a run that learned nothing
    # keeps whatever the last informed run decided.
    assert _access_status_of(database, "dead") == "unavailable"


def test_a_source_that_comes_back_is_available_again(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    """Platforms do restore things, and the library must be able to notice."""
    library, database, _store = library_components
    create_job(database, "job-back", mode="full")
    gone = replace(
        captured("tombstone", source_id="back"),
        platform_metadata={"source_status": "source_unavailable"},
    )
    library.ingest_batch("job-back", "x", "batch-1", [gone], True)
    assert _access_status_of(database, "back") == "unavailable"

    restored = replace(
        captured("the real article, back again", source_id="back"),
        platform_metadata={"source_status": "available"},
    )
    library.ingest_batch("job-back", "x", "batch-2", [restored], True)
    assert _access_status_of(database, "back") == "available"


def _collections_of(database: Database, source_id: str) -> list[str]:
    return [
        str(row["name"])
        for row in database.connection.execute(
            "SELECT name FROM item_collections WHERE platform = 'x' AND source_id = ? "
            "ORDER BY name",
            (source_id,),
        )
    ]


def test_folder_membership_is_queryable_after_ingest(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _store = library_components
    create_job(database, "job-c", mode="full")
    item = replace(captured("body", source_id="folders"), collections=("NLP", "考研"))

    library.ingest_batch("job-c", "x", "batch-1", [item], True)

    assert _collections_of(database, "folders") == ["NLP", "考研"]


def test_a_partial_run_adds_folders_without_dropping_the_ones_it_never_saw(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    """An incremental run stops at each folder's frontier, so its view is partial.

    Replacing memberships from that view would delete every folder the run did
    not happen to scan.
    """
    library, database, _store = library_components
    create_job(database, "job-partial", mode="full")
    library.ingest_batch(
        "job-partial",
        "x",
        "batch-1",
        [replace(captured("body", source_id="wide"), collections=("NLP", "考研", "钢琴"))],
        True,
    )

    # A later run that only scanned one folder reports only that one.
    library.ingest_batch(
        "job-partial",
        "x",
        "batch-2",
        [replace(captured("body changed", source_id="wide"), collections=("NLP",))],
        True,
    )

    assert _collections_of(database, "wide") == ["NLP", "考研", "钢琴"]


def test_blank_folder_names_are_never_recorded(
    library_components: tuple[LibraryModule, Database, ItemStore],
) -> None:
    library, database, _store = library_components
    create_job(database, "job-blank", mode="full")
    item = replace(captured("body", source_id="blank"), collections=("  ", "", " NLP "))

    library.ingest_batch("job-blank", "x", "batch-1", [item], True)

    # Trimmed, so " NLP " and "NLP" are one folder rather than two.
    assert _collections_of(database, "blank") == ["NLP"]
