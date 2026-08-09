import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.database import Database
from favhub.domain import CapturedItem
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore, SourceSnapshotError
from favhub.library import LibraryModule
from favhub.maintenance import MaintenanceReport, StartupMaintenance


def captured(
    body: str,
    *,
    platform: str = "x",
    source_id: str = "42",
    published_at: datetime | None = None,
    observed_at: datetime | None = None,
    collections: tuple[str, ...] = ("Research",),
    extractor_version: str = "fixture-v1",
) -> CapturedItem:
    return CapturedItem(
        platform=platform,
        source_id=source_id,
        canonical_url=f"https://example.com/{platform}/{source_id}",
        title=f"Saved item {source_id}",
        author="example",
        published_at=published_at or datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=observed_at or datetime(2026, 7, 18, tzinfo=UTC),
        body=body,
        collections=collections,
        extractor_version=extractor_version,
    )


def test_run_registers_published_item_and_resets_running_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    item = captured("published snapshot")
    store.write(item)
    running_id = queue.enqueue("other", "7", "enrich_item", "other-hash")
    pending_id = queue.enqueue("pending", "8", "enrich_item", "pending-hash")
    completed_id = queue.enqueue("completed", "9", "enrich_item", "completed-hash")
    database.connection.execute(
        "UPDATE enrichment_tasks SET status = 'completed' WHERE id = ?",
        (completed_id,),
    )
    assert queue.claim_next() is not None

    try:
        report = StartupMaintenance(database, store, queue).run()

        assert report == MaintenanceReport(registered_items=1, reset_tasks=1)
        row = database.connection.execute(
            """
            SELECT content_hash, item_dir, published_at, first_seen_at,
                   index_input_hash
            FROM items
            WHERE platform = 'x' AND source_id = '42'
            """
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "content_hash": item.content_hash,
            "item_dir": str(store.items_root / "x" / "42"),
            "published_at": "2026-01-02T00:00:00Z",
            "first_seen_at": "2026-07-18T00:00:00Z",
            "index_input_hash": store.index_fingerprint("x", "42"),
        }
        tasks = database.connection.execute(
            """
            SELECT id, platform, source_id, input_hash, status
            FROM enrichment_tasks
            ORDER BY platform, source_id
            """
        ).fetchall()
        assert [tuple(task) for task in tasks] == [
            (completed_id, "completed", "9", "completed-hash", "completed"),
            (running_id, "other", "7", "other-hash", "pending"),
            (pending_id, "pending", "8", "pending-hash", "pending"),
            (tasks[3]["id"], "x", "42", store.index_fingerprint("x", "42"), "pending"),
        ]
    finally:
        database.close()


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    store.write(captured("published snapshot"))
    maintenance = StartupMaintenance(database, store, queue)

    try:
        assert maintenance.run() == MaintenanceReport(registered_items=1, reset_tasks=0)

        assert maintenance.run() == MaintenanceReport(registered_items=0, reset_tasks=0)
        assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
        assert (
            database.connection.execute("SELECT COUNT(*) FROM enrichment_tasks").fetchone()[0] == 1
        )
    finally:
        database.close()


def test_run_repairs_item_row_from_newer_published_snapshot(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    old = captured("old body", published_at=datetime(2025, 1, 2, tzinfo=UTC))
    new = captured("new body", published_at=datetime(2026, 6, 7, tzinfo=UTC))
    old_directory = tmp_path / "old-location"
    first_seen_at = "2025-07-18T00:00:00Z"
    last_full_synced_at = "2026-07-17T12:34:56Z"
    database.connection.execute(
        """
        INSERT INTO items (
            platform, source_id, content_hash, item_dir, published_at,
            first_seen_at, last_full_synced_at
        ) VALUES ('x', '42', ?, ?, ?, ?, ?)
        """,
        (
            old.content_hash,
            str(old_directory),
            "2025-01-02T00:00:00Z",
            first_seen_at,
            last_full_synced_at,
        ),
    )
    queue.enqueue("x", "42", "enrich_item", old.content_hash)
    store.write(new)
    content_path = store.items_root / "x" / "42" / "content.md"
    notes_path = store.items_root / "x" / "42" / "notes.md"
    content_path.write_text("published content sentinel\n", encoding="utf-8")
    notes_path.write_text("durable notes sentinel\n", encoding="utf-8")

    try:
        report = StartupMaintenance(database, store, queue).run()

        assert report == MaintenanceReport(registered_items=0, reset_tasks=0)
        row = database.connection.execute(
            """
            SELECT content_hash, item_dir, published_at, first_seen_at,
                   last_full_synced_at, index_input_hash
            FROM items
            WHERE platform = 'x' AND source_id = '42'
            """
        ).fetchone()
        assert row is not None
        assert dict(row) == {
            "content_hash": new.content_hash,
            "item_dir": str(store.items_root / "x" / "42"),
            "published_at": "2026-06-07T00:00:00Z",
            "first_seen_at": first_seen_at,
            "last_full_synced_at": last_full_synced_at,
            "index_input_hash": store.index_fingerprint("x", "42"),
        }
        hashes = {
            row["input_hash"]
            for row in database.connection.execute(
                """
                SELECT input_hash
                FROM enrichment_tasks
                WHERE platform = 'x' AND source_id = '42'
                  AND kind = 'index_content'
                """
            )
        }
        assert hashes == {store.index_fingerprint("x", "42")}
        assert "new body" in content_path.read_text(encoding="utf-8")
        assert notes_path.read_text(encoding="utf-8") == "durable notes sentinel\n"
    finally:
        database.close()


@pytest.mark.parametrize("existing_row", [False, True])
def test_enqueue_failure_rolls_back_item_registration_or_repair(
    tmp_path: Path,
    existing_row: bool,
) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    published = captured("published snapshot")
    store.write(published)
    if existing_row:
        database.connection.execute(
            """
            INSERT INTO items (
                platform, source_id, content_hash, item_dir, published_at,
                first_seen_at, last_full_synced_at
            ) VALUES ('x', '42', 'old-hash', 'old-dir',
                      '2025-01-01T00:00:00Z', '2025-02-03T00:00:00Z',
                      '2026-07-17T00:00:00Z')
            """
        )

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

    try:
        with pytest.raises(RuntimeError, match="injected enqueue failure"):
            StartupMaintenance(database, store, FailingQueue(database)).run()

        row = database.connection.execute(
            """
            SELECT content_hash, item_dir, published_at, first_seen_at,
                   last_full_synced_at
            FROM items
            WHERE platform = 'x' AND source_id = '42'
            """
        ).fetchone()
        if existing_row:
            assert row is not None
            assert tuple(row) == (
                "old-hash",
                "old-dir",
                "2025-01-01T00:00:00Z",
                "2025-02-03T00:00:00Z",
                "2026-07-17T00:00:00Z",
            )
        else:
            assert row is None
        assert (
            database.connection.execute("SELECT COUNT(*) FROM enrichment_tasks").fetchone()[0] == 0
        )
        assert (store.items_root / "x" / "42" / "source.json").is_file()
    finally:
        database.close()


def test_invalid_snapshot_aborts_before_database_changes(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    store.write(captured("valid", source_id="41"))
    store.write(captured("will be corrupted", source_id="42"))
    corrupt_path = store.items_root / "x" / "42" / "source.json"
    content_path = store.items_root / "x" / "42" / "content.md"
    content_path.write_text("do not rebuild from corrupt source\n", encoding="utf-8")
    corrupt = json.loads(corrupt_path.read_text(encoding="utf-8"))
    corrupt["content_hash"] = "not-the-content-hash"
    corrupt_path.write_text(json.dumps(corrupt), encoding="utf-8")
    running_id = queue.enqueue("other", "7", "enrich_item", "other-hash")
    assert queue.claim_next() is not None

    try:
        with pytest.raises(SourceSnapshotError, match="content_hash"):
            StartupMaintenance(database, store, queue).run()

        assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        running = database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id = ?", (running_id,)
        ).fetchone()
        assert running is not None
        assert running["status"] == "running"
        assert content_path.read_text(encoding="utf-8") == "do not rebuild from corrupt source\n"
    finally:
        database.close()


def test_mixed_files_after_source_publication_failure_are_recovered_from_published_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    old = captured("old body")
    new = replace(old, body="new body")
    try:
        database.connection.execute(
            "INSERT INTO sync_jobs (id, mode, status, options_json, created_at, updated_at) "
            "VALUES ('initial', 'incremental', 'running', '{}', '2026-07-18T00:00:00Z', "
            "'2026-07-18T00:00:00Z')"
        )
        database.connection.execute(
            "INSERT INTO sync_jobs (id, mode, status, options_json, created_at, updated_at) "
            "VALUES ('full', 'full', 'running', '{}', '2026-07-18T00:00:00Z', "
            "'2026-07-18T00:00:00Z')"
        )
        library = LibraryModule(database, store, queue)
        library.ingest_batch("initial", "x", "initial-batch", [old], False)
        notes = store.items_root / "x" / "42" / "notes.md"
        notes.write_text("durable note\n", encoding="utf-8")
        original_atomic_text = store._atomic_text

        def fail_source_publication(path: Path, content: str) -> None:
            if path.name == "source.json":
                raise OSError("injected source publication failure")
            original_atomic_text(path, content)

        monkeypatch.setattr(store, "_atomic_text", fail_source_publication)
        with pytest.raises(OSError, match="injected source publication failure"):
            library.ingest_batch("full", "x", "full-batch", [new], True)

        row = database.connection.execute(
            "SELECT content_hash FROM items WHERE platform = 'x' AND source_id = '42'"
        ).fetchone()
        assert row is not None and row["content_hash"] == old.content_hash
        assert "new body" in (store.items_root / "x" / "42" / "content.md").read_text(
            encoding="utf-8"
        )

        StartupMaintenance(database, store, queue).run()

        assert "old body" in (store.items_root / "x" / "42" / "content.md").read_text(
            encoding="utf-8"
        )
        assert notes.read_text(encoding="utf-8") == "durable note\n"
        source = json.loads(
            (store.items_root / "x" / "42" / "source.json").read_text(encoding="utf-8")
        )
        assert source["body"] == "old body"
        row = database.connection.execute(
            "SELECT content_hash, access_status "
            "FROM items WHERE platform = 'x' AND source_id = '42'"
        ).fetchone()
        assert row is not None
        assert row["content_hash"] == old.content_hash
        assert row["access_status"] == "available"
    finally:
        database.close()


def test_db_only_item_is_marked_missing_and_can_be_restored(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    item = captured("db only")
    database.connection.execute(
        """
        INSERT INTO items (
            platform, source_id, content_hash, item_dir, published_at, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item.platform,
            item.source_id,
            item.content_hash,
            str(store.items_root / item.platform / item.source_id),
            "2026-01-02T00:00:00Z",
            "2026-07-18T00:00:00Z",
        ),
    )
    try:
        StartupMaintenance(database, store, queue).run()
        row = database.connection.execute(
            "SELECT access_status FROM items WHERE platform = 'x' AND source_id = '42'"
        ).fetchone()
        assert row is not None and row["access_status"] == "missing"

        store.write(item)
        StartupMaintenance(database, store, queue).run()
        row = database.connection.execute(
            "SELECT access_status FROM items WHERE platform = 'x' AND source_id = '42'"
        ).fetchone()
        assert row is not None and row["access_status"] == "available"
    finally:
        database.close()


@pytest.mark.parametrize("content_state", ["missing", "tampered"])
def test_maintenance_rebuilds_content_from_source_without_touching_source_or_notes(
    tmp_path: Path, content_state: str
) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    item = captured("published body")
    store.write(item)
    source_path = store.items_root / "x" / "42" / "source.json"
    source_before = source_path.read_text(encoding="utf-8")
    notes_path = store.items_root / "x" / "42" / "notes.md"
    notes_path.write_text("durable note\n", encoding="utf-8")
    content_path = store.items_root / "x" / "42" / "content.md"
    if content_state == "missing":
        content_path.unlink()
    else:
        content_path.write_text("tampered content\n", encoding="utf-8")
    try:
        StartupMaintenance(database, store, queue).run()
        assert source_path.read_text(encoding="utf-8") == source_before
        assert "published body" in content_path.read_text(encoding="utf-8")
        assert notes_path.read_text(encoding="utf-8") == "durable note\n"
    finally:
        database.close()


def test_maintenance_never_resurrects_an_item_the_platform_dropped(tmp_path: Path) -> None:
    """A healthy local snapshot is not evidence that the source came back.

    Maintenance treated every non-`available` status as damage and reset it, so
    the first backfill that marked 238 tombstones was undone by the very next
    command that opened the application.
    """
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    item = captured("a tombstone with a perfectly good snapshot")
    store.write(item)
    try:
        StartupMaintenance(database, store, queue).run()
        database.connection.execute(
            "UPDATE items SET access_status = 'unavailable' WHERE platform = ? AND source_id = ?",
            (item.platform, item.source_id),
        )
        database.connection.commit()

        StartupMaintenance(database, store, queue).run()

        row = database.connection.execute(
            "SELECT access_status FROM items WHERE platform = ? AND source_id = ?",
            (item.platform, item.source_id),
        ).fetchone()
        assert row is not None and row["access_status"] == "unavailable"
    finally:
        database.close()
