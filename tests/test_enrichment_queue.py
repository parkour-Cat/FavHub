from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

import favhub.enrichment_queue as enrichment_queue
from favhub.database import Database
from favhub.enrichment_queue import EnrichmentQueue


def _claim_from_separate_connection(path: Path, barrier: Barrier) -> str | None:
    database = Database.open(path)
    try:
        barrier.wait(timeout=5)
        task = EnrichmentQueue(database).claim_next()
        return None if task is None else task.id
    finally:
        database.close()


def test_enqueue_is_idempotent_and_claim_is_exclusive(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        first = queue.enqueue("x", "42", "enrich_item", "hash-1")
        second = queue.enqueue("x", "42", "enrich_item", "hash-1")
        assert first == second
        claimed = queue.claim_next()
        assert claimed is not None
        assert claimed.id == first
        assert queue.claim_next() is None
    finally:
        database.close()


def test_claim_is_exclusive_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "favhub.sqlite3"
    database = Database.open(path)
    try:
        task_id = EnrichmentQueue(database).enqueue("x", "42", "enrich_item", "hash-1")
    finally:
        database.close()

    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_claim_from_separate_connection, path, barrier) for _ in range(2)
        ]
        claimed_ids = [future.result() for future in futures]

    assert claimed_ids.count(task_id) == 1
    assert claimed_ids.count(None) == 1


def test_claim_next_excludes_ids_without_changing_order(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        first = queue.enqueue("x", "1", "embed_content", "a")
        second = queue.enqueue("x", "2", "embed_content", "b")
        third = queue.enqueue("x", "3", "embed_content", "c")
        database.connection.executemany(
            "UPDATE enrichment_tasks SET created_at=? WHERE id=?",
            (
                ("2026-01-01T00:00:01.000000Z", first),
                ("2026-01-01T00:00:02.000000Z", second),
                ("2026-01-01T00:00:03.000000Z", third),
            ),
        )
        claimed_second = queue.claim_next(kind="embed_content", excluded_ids=(first,))
        claimed_first = queue.claim_next(kind="embed_content", excluded_ids=(second,))
        claimed_third = queue.claim_next(kind="embed_content", excluded_ids=(first, second))
        assert claimed_second is not None and claimed_second.id == second
        assert claimed_first is not None and claimed_first.id == first
        assert claimed_third is not None and claimed_third.id == third
    finally:
        database.close()


def test_now_uses_fixed_width_microseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = SimpleNamespace(now=lambda timezone: datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    monkeypatch.setattr(enrichment_queue, "datetime", clock)

    assert enrichment_queue.now() == "2026-01-02T03:04:05.000000Z"


def test_running_tasks_return_to_pending_after_restart(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
        assert queue.claim_next() is not None
        assert queue.reset_running() == 1
        claimed_again = queue.claim_next()
        assert claimed_again is not None
        assert claimed_again.id == task_id
        assert claimed_again.attempts == 2
    finally:
        database.close()


def test_enqueue_participates_in_caller_transaction(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        with pytest.raises(RuntimeError, match="roll back enqueue"), database.transaction():
            task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
            row = database.connection.execute(
                "SELECT id FROM enrichment_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            raise RuntimeError("roll back enqueue")

        row = database.connection.execute(
            "SELECT id FROM enrichment_tasks WHERE platform = 'x' AND source_id = '42'"
        ).fetchone()
        assert row is None
    finally:
        database.close()


def test_complete_participates_in_caller_transaction(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
        assert queue.claim_next() is not None
        database.connection.execute(
            "UPDATE enrichment_tasks SET error = 'previous failure' WHERE id = ?", (task_id,)
        )

        with pytest.raises(RuntimeError, match="roll back complete"), database.transaction():
            queue.complete(task_id)
            row = database.connection.execute(
                "SELECT status, error FROM enrichment_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            assert (row["status"], row["error"]) == ("completed", None)
            raise RuntimeError("roll back complete")

        row = database.connection.execute(
            "SELECT status, error FROM enrichment_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        assert (row["status"], row["error"]) == ("running", "previous failure")
    finally:
        database.close()


def test_fail_participates_in_caller_transaction(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
        assert queue.claim_next() is not None

        with pytest.raises(RuntimeError, match="roll back failure"), database.transaction():
            queue.fail(task_id, "transient failure")
            row = database.connection.execute(
                "SELECT status, error FROM enrichment_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            assert (row["status"], row["error"]) == ("pending", "transient failure")
            raise RuntimeError("roll back failure")

        row = database.connection.execute(
            "SELECT status, error FROM enrichment_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        assert (row["status"], row["error"]) == ("running", None)
    finally:
        database.close()


def test_reset_running_participates_in_caller_transaction(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
        assert queue.claim_next() is not None

        with pytest.raises(RuntimeError, match="roll back reset"), database.transaction():
            assert queue.reset_running() == 1
            row = database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            assert row is not None
            assert row["status"] == "pending"
            raise RuntimeError("roll back reset")

        row = database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "running"
    finally:
        database.close()


def test_complete_rejects_unknown_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        with pytest.raises(KeyError, match="missing-task"):
            queue.complete("missing-task")
    finally:
        database.close()


def test_fail_rejects_unknown_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        with pytest.raises(KeyError, match="missing-task"):
            queue.fail("missing-task", "transient failure")
    finally:
        database.close()


def test_complete_requires_running_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
        with pytest.raises(ValueError, match="pending"):
            queue.complete(task_id)

        assert queue.claim_next() is not None
        queue.complete(task_id)
        with pytest.raises(ValueError, match="completed"):
            queue.complete(task_id)
    finally:
        database.close()


def test_fail_requires_running_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    queue = EnrichmentQueue(database)
    try:
        task_id = queue.enqueue("x", "42", "enrich_item", "hash-1")
        with pytest.raises(ValueError, match="pending"):
            queue.fail(task_id, "transient failure")

        assert queue.claim_next() is not None
        queue.complete(task_id)
        with pytest.raises(ValueError, match="completed"):
            queue.fail(task_id, "late failure")
    finally:
        database.close()
