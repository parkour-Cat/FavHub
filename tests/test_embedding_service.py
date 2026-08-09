from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from favhub.database import Database
from favhub.embedding import EmbeddingProfile, encode_float32
from favhub.embedding_indexing import EmbeddedTask, EmbeddingIndexer
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.embedding_service import EmbeddingService
from favhub.enrichment_queue import EnrichmentQueue, EnrichmentTask


def profile() -> EmbeddingProfile:
    return EmbeddingProfile(
        id="profile-1",
        provider="fake",
        provider_version="1",
        model="fake-model",
        dimensions=2,
        normalization="l2",
        max_input_tokens=8,
        segment_tokens=2,
        overlap_tokens=1,
        artifact_digest="a" * 64,
    )


class FakeRuntime:
    def __init__(self, created: EmbeddingProfile) -> None:
        self.created = created

    def initialize(self) -> EmbeddingProfile:
        return self.created


class FakeIndexer:
    def __init__(self, database: Database, queue: EnrichmentQueue, events: list[str]) -> None:
        self.database = database
        self.queue = queue
        self.events = events
        self.calls: list[str] = []

    def index_task(self, task: EnrichmentTask) -> Any:
        self.events.append(f"index:{task.id}")
        self.calls.append(f"index:{task.id}")
        self.queue.complete(task.id)
        return type("Indexed", (), {"chunk_count": 1})()


class FakeEmbeddingIndexer:
    def __init__(self, database: Database, queue: EnrichmentQueue, events: list[str]) -> None:
        self.database = database
        self.queue = queue
        self.events = events
        self.calls: list[str] = []
        self.force_values: list[bool] = []

    def reindex_missing(self, force: bool = False) -> int:
        self.events.append("reconcile")
        self.force_values.append(force)
        return 0

    def index_task(self, task: EnrichmentTask) -> EmbeddedTask:
        self.events.append(f"embed:{task.id}")
        self.calls.append(f"embed:{task.id}")
        self.queue.complete(task.id)
        return EmbeddedTask(task, vector_count=1, segment_count=1)


@pytest.fixture
def service_parts(tmp_path: Path):
    database = Database.open(tmp_path / "db.sqlite3")
    queue = EnrichmentQueue(database)
    profiles = EmbeddingProfileStore(database)
    runtime = FakeRuntime(profile())
    events: list[str] = []
    indexer = FakeIndexer(database, queue, events)
    embedding_indexer = FakeEmbeddingIndexer(database, queue, events)
    service = EmbeddingService(
        database,
        runtime,
        profiles,
        queue,
        indexer,
        embedding_indexer,
    )
    try:
        yield database, queue, profiles, runtime, indexer, embedding_indexer, service, events
    finally:
        database.close()


def test_build_reports_progress_as_it_goes(service_parts) -> None:
    """A silent multi-hour job is indistinguishable from a stuck one."""
    _database, queue, _profiles, _runtime, _indexer, _embedding_indexer, service, _events = (
        service_parts
    )
    service.initialize()
    queue.enqueue("x", "1", "index_content", "index-hash")
    for ordinal in range(3):
        queue.enqueue("x", str(ordinal), "embed_content", f"embed-hash-{ordinal}")

    seen: list[Any] = []
    report = service.build(max_items=None, force=False, progress=seen.append)

    assert [beat.phase for beat in seen] == ["index", "reconcile", "embed"]
    # Work already done is visible before the run ends, and the vector count is
    # the unit that actually costs time.
    assert seen[-1].done == report.attempted
    assert seen[-1].vectors == 3
    assert seen[-1].remaining == 0
    assert seen[-1].elapsed_seconds >= 0


def test_progress_estimates_only_once_a_rate_exists() -> None:
    from favhub.embedding_service import EmbeddingBuildProgress

    cold = EmbeddingBuildProgress("embed", done=0, remaining=500, vectors=0, elapsed_seconds=0.0)
    assert cold.eta_seconds is None

    warm = EmbeddingBuildProgress("embed", done=50, remaining=500, vectors=0, elapsed_seconds=10.0)
    assert warm.rate == 5.0
    assert warm.eta_seconds == 100.0


def test_build_stays_silent_without_a_progress_callback(service_parts) -> None:
    _database, queue, _profiles, _runtime, _indexer, _embedding_indexer, service, _events = (
        service_parts
    )
    service.initialize()
    queue.enqueue("x", "1", "embed_content", "embed-hash")

    assert service.build(max_items=None, force=False).attempted == 1


def test_initialize_activates_profile_and_build_orders_index_before_embed(service_parts) -> None:
    database, queue, profiles, _runtime, indexer, embedding_indexer, service, events = service_parts
    profile_id = service.initialize().id
    assert profile_id == "profile-1"
    events.clear()
    embedding_indexer.force_values.clear()
    index_id = queue.enqueue("x", "1", "index_content", "index-hash")
    embed_id = queue.enqueue("x", "1", "embed_content", "embed-hash")

    report = service.build(max_items=None, force=True)

    assert report.attempted == 2
    assert report.failed == 0
    assert indexer.calls == [f"index:{index_id}"]
    assert embedding_indexer.calls == [f"embed:{embed_id}"]
    assert events == [f"index:{index_id}", "reconcile", f"embed:{embed_id}"]
    assert embedding_indexer.force_values == [True]
    assert profiles.active() == profile()
    persisted = database.connection.execute(
        """SELECT status, max_items, counts_json, error_json, finished_at
           FROM embedding_build_runs WHERE id = ?""",
        (report.run_id,),
    ).fetchone()
    assert persisted is not None
    counts = json.loads(str(persisted["counts_json"]))
    assert counts["attempted"] == 2
    assert counts["processed"] == 2
    assert counts["failed"] == 0
    assert persisted["status"] == "completed"
    assert persisted["max_items"] is None
    assert persisted["error_json"] is None
    assert persisted["finished_at"] is not None


def test_initialize_reconciles_existing_indexed_items_after_activation(service_parts) -> None:
    _database, _queue, profiles, _runtime, _indexer, embedding_indexer, service, events = (
        service_parts
    )

    initialized = service.initialize()

    assert profiles.active() == initialized
    assert embedding_indexer.force_values == [False]
    assert events == ["reconcile"]


def test_build_skips_reconciliation_when_only_embedding_tasks_are_pending(service_parts) -> None:
    _database, queue, profiles, _runtime, _indexer, embedding_indexer, service, events = (
        service_parts
    )
    profiles.activate(profile())
    embed_id = queue.enqueue("x", "1", "embed_content", "embed-hash")

    report = service.build(max_items=None, force=False)

    assert report.processed == 1
    assert embedding_indexer.force_values == []
    assert events == [f"embed:{embed_id}"]


def test_build_attempts_each_task_once_and_honors_limit(service_parts) -> None:
    database, queue, profiles, _runtime, _indexer, _embedding_indexer, service, _events = (
        service_parts
    )
    profiles.activate(profile())
    queue.enqueue("x", "1", "index_content", "hash-1")
    queue.enqueue("x", "2", "index_content", "hash-2")
    queue.enqueue("x", "3", "index_content", "hash-3")

    report = service.build(max_items=2, force=False)
    assert report.attempted == 2
    assert report.remaining >= 1
    persisted = database.connection.execute(
        "SELECT max_items FROM embedding_build_runs WHERE id=?", (report.run_id,)
    ).fetchone()
    assert persisted["max_items"] == 2


def test_build_requires_initialized_profile(service_parts) -> None:
    service = service_parts[-2]
    with pytest.raises(RuntimeError, match="not initialized"):
        service.build(max_items=None, force=False)


def test_build_report_errors_are_stable(service_parts) -> None:
    database, queue, profiles, _runtime, _indexer, embedding_indexer, service, _events = (
        service_parts
    )
    profiles.activate(profile())
    queue.enqueue("x", "1", "embed_content", "hash-1")

    def fail(task: EnrichmentTask) -> EmbeddedTask:
        queue.fail(task.id, "provider unavailable")
        raise RuntimeError("provider unavailable")

    embedding_indexer.index_task = fail  # type: ignore[method-assign]
    report = service.build(max_items=None, force=False)
    task = database.connection.execute(
        "SELECT id, attempts, status FROM enrichment_tasks WHERE kind='embed_content'"
    ).fetchone()
    assert report.failed == 1
    assert report.attempted == 1
    assert report.remaining == 1
    assert task["attempts"] == 1
    assert task["status"] == "pending"
    assert report.errors[0].code == "embed_content_failed"
    assert report.errors[0].task_id
    persisted = database.connection.execute(
        "SELECT error_json FROM embedding_build_runs WHERE id=?", (report.run_id,)
    ).fetchone()
    assert json.loads(str(persisted["error_json"])) == [
        {
            "code": "embed_content_failed",
            "kind": "embed_content",
            "message": "provider unavailable",
            "task_id": task["id"],
        }
    ]


@pytest.mark.parametrize("force", [False, True])
def test_build_persists_failed_run_when_reconciliation_fails(service_parts, force: bool) -> None:
    database, queue, profiles, _runtime, _indexer, embedding_indexer, service, _events = (
        service_parts
    )
    profiles.activate(profile())
    queue.enqueue("x", "1", "index_content", "hash-1")
    observed_running: list[tuple[str, object, dict[str, object]]] = []

    def fail_reconciliation(force: bool = False) -> int:
        row = database.connection.execute(
            """SELECT status, counts_json, finished_at
               FROM embedding_build_runs ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        observed_running.append(
            (str(row["status"]), row["finished_at"], json.loads(row["counts_json"]))
        )
        raise RuntimeError(f"reconciliation failed (force={force})")

    embedding_indexer.reindex_missing = fail_reconciliation  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="reconciliation failed"):
        service.build(max_items=None, force=force)

    assert observed_running == [
        (
            "running",
            None,
            {
                "attempted": 0,
                "elapsed_seconds": 0.0,
                "failed": 0,
                "processed": 0,
                "profile_id": "profile-1",
                "remaining": 1,
                "run_id": observed_running[0][2]["run_id"],
                "skipped": 0,
            },
        )
    ]
    persisted = database.connection.execute(
        """SELECT id, status, counts_json, error_json, finished_at
           FROM embedding_build_runs"""
    ).fetchone()
    assert persisted is not None
    counts = json.loads(str(persisted["counts_json"]))
    assert persisted["status"] == "failed"
    assert persisted["finished_at"] is not None
    assert counts["run_id"] == persisted["id"]
    assert counts["attempted"] == 1
    assert counts["processed"] == 1
    assert counts["failed"] == 1
    assert json.loads(str(persisted["error_json"])) == [
        {
            "code": "reconcile_failed",
            "kind": "build",
            "message": f"reconciliation failed (force={force})",
            "task_id": None,
        }
    ]
    last_report = profiles.summary().last_build_report
    assert last_report is not None
    assert last_report["status"] == "failed"
    assert last_report["failed"] == 1


def test_force_build_persists_mid_requeue_failure_after_atomic_rollback(
    service_parts, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, queue, profiles, runtime, indexer, _embedding_indexer, _service, events = (
        service_parts
    )
    active = profile()
    profiles.activate(active)
    vector = encode_float32((1.0, 0.0), dimensions=2)
    embed_task_ids: list[str] = []
    for source_id in ("1", "2"):
        database.connection.execute(
            """INSERT INTO items(platform, source_id, content_hash, item_dir,
               published_at, first_seen_at, index_input_hash)
               VALUES ('x', ?, 'content', '.', '2026-01-01', '2026-01-01', 'input')""",
            (source_id,),
        )
        chunk_id = database.connection.execute(
            """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
               line_start, line_end, heading, text, input_hash, created_at)
               VALUES ('x', ?, 0, 'content.md', 1, 1, NULL,
                       'one two three', 'input', '2026-01-01')""",
            (source_id,),
        ).lastrowid
        index_task_id = queue.enqueue("x", source_id, "index_content", "input")
        embed_task_id = queue.enqueue(
            "x",
            source_id,
            "embed_content",
            embedding_task_input_hash(active.id, "input"),
        )
        database.connection.execute(
            "UPDATE enrichment_tasks SET status='completed' WHERE id IN (?, ?)",
            (index_task_id, embed_task_id),
        )
        embed_task_ids.append(embed_task_id)
        database.connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (chunk_id, active.id, vector),
        )

    class PreflightProvider:
        name = "fake"
        version = "1"
        dimensions = 2

        def tokenize(self, text: str) -> tuple[int, ...]:
            return tuple(range(len(text.split())))

        def decode_tokens(self, tokens: Sequence[int]) -> str:
            return " ".join(str(token) for token in tokens)

    original_enqueue = queue.enqueue

    def fail_second(platform: str, source_id: str, kind: str, input_hash: str) -> str:
        assert database.connection.in_transaction
        if source_id == "2" and kind == "embed_content":
            raise RuntimeError("injected force requeue failure")
        return original_enqueue(platform, source_id, kind, input_hash)

    monkeypatch.setattr(queue, "enqueue", fail_second)
    real_embedding_indexer = EmbeddingIndexer(
        database,
        queue,
        profiles,
        provider=PreflightProvider(),  # type: ignore[arg-type]
    )
    service = EmbeddingService(
        database,
        runtime,  # type: ignore[arg-type]
        profiles,
        queue,
        indexer,  # type: ignore[arg-type]
        real_embedding_indexer,
    )

    with pytest.raises(RuntimeError, match="injected force requeue failure"):
        service.build(max_items=None, force=True)

    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id=?", (active.id,)
        ).fetchone()[0]
        == 2
    )
    statuses = database.connection.execute(
        """SELECT status FROM enrichment_tasks WHERE id IN (?, ?)""",
        tuple(embed_task_ids),
    ).fetchall()
    assert [row["status"] for row in statuses] == ["completed", "completed"]
    run = database.connection.execute(
        "SELECT status, error_json, finished_at FROM embedding_build_runs"
    ).fetchone()
    assert run["status"] == "failed"
    assert run["finished_at"] is not None
    assert json.loads(str(run["error_json"]))[0]["code"] == "reconcile_failed"
    assert profiles.summary().last_build_report["status"] == "failed"  # type: ignore[index]
    assert events == []
