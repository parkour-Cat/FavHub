import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.database import Database
from favhub.domain import CapturedItem, SyncMode
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.sync_module import (
    ScopeFinish,
    StartSyncRequest,
    SubmitBatchReceipt,
    SyncModule,
)


@pytest.fixture
def sync_components(tmp_path: Path) -> Iterator[tuple[SyncModule, Database, ItemStore]]:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    module = SyncModule(database, library)
    try:
        yield module, database, store
    finally:
        database.close()


def item(
    published_at: datetime,
    *,
    platform: str = "x",
    source_id: str | None = None,
) -> CapturedItem:
    source_id = source_id or str(int(published_at.timestamp()))
    return CapturedItem(
        platform=platform,
        source_id=source_id,
        canonical_url=f"https://example.com/{platform}/{source_id}",
        title="Saved post",
        author=None,
        published_at=published_at,
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="body",
        collections=(),
        extractor_version="fixture-v1",
    )


def request(
    *,
    platforms: tuple[str, ...] = ("x",),
    mode: SyncMode = SyncMode.INCREMENTAL,
    published_since: datetime | None = None,
    published_until: datetime | None = None,
    max_scan_items: int | None = None,
) -> StartSyncRequest:
    return StartSyncRequest(
        platforms=platforms,
        mode=mode,
        published_since=published_since,
        published_until=published_until,
        max_scan_items=max_scan_items,
    )


def test_submit_filters_by_publication_date_and_reports_partial_completion(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request(published_since=datetime(2025, 1, 1, tzinfo=UTC)))

    receipt = module.submit_batch(
        started.job_id,
        "x",
        "batch-1",
        [
            item(datetime(2024, 1, 1, tzinfo=UTC)),
            item(datetime(2026, 1, 1, tzinfo=UTC)),
        ],
    )

    assert receipt.added == 1
    assert receipt.refreshed == 0
    assert receipt.duplicates == 0
    assert receipt.out_of_range == 1
    status = module.get_status(started.job_id)
    assert status["platforms"][0]["counts"] == {
        "scanned": 2,
        "added": 1,
        "refreshed": 0,
        "duplicates": 0,
        "out_of_range": 1,
    }
    row = database.connection.execute("SELECT COUNT(*) FROM items WHERE platform = 'x'").fetchone()
    assert row[0] == 1


def test_pause_preserves_confirmed_batches(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())
    receipt = module.submit_batch(
        started.job_id,
        "x",
        "batch-1",
        [item(datetime(2026, 1, 1, tzinfo=UTC))],
    )

    module.pause_sync(started.job_id, "x", "rate_limited", "try later")

    status = module.get_status(started.job_id)
    assert status["capture_status"] == "paused"
    assert status["platforms"][0]["status"] == "paused"
    assert status["platforms"][0]["counts"]["added"] == receipt.added
    assert status["platforms"][0]["error"] == {
        "code": "rate_limited",
        "message": "try later",
    }


def test_fail_sync_is_idempotent_and_job_stays_failed_until_all_runs_finish(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request(platforms=("x", "bilibili")))

    module.fail_sync(started.job_id, "x", "capture_failed", "browser disconnected")
    module.fail_sync(started.job_id, "x", "capture_failed", "browser disconnected")

    failed = module.get_status(started.job_id)
    assert failed["capture_status"] == "failed"
    assert failed["capture_finished_at"] is None
    assert failed["platforms"][1]["status"] == "failed"
    assert failed["platforms"][1]["error"] == {
        "code": "capture_failed",
        "message": "browser disconnected",
    }

    module.finish_scan(
        started.job_id,
        "bilibili",
        observed_end=True,
        max_scan_reached=False,
        visible_total=0,
        frontier_ids=(),
    )

    finished = module.get_status(started.job_id)
    assert finished["capture_status"] == "failed"
    assert finished["capture_finished_at"] is not None


@pytest.mark.parametrize(("code", "message"), [("", "message"), ("code", "  ")])
def test_fail_sync_rejects_blank_structured_error(
    sync_components: tuple[SyncModule, Database, ItemStore],
    code: str,
    message: str,
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())

    with pytest.raises(ValueError, match="blank"):
        module.fail_sync(started.job_id, "x", code, message)

    assert module.get_status(started.job_id)["capture_status"] == "running"


def test_fail_sync_does_not_change_completed_or_partial_runs(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    for observed_end, expected in ((True, "completed"), (False, "partial")):
        started = module.start_sync(request())
        module.finish_scan(
            started.job_id,
            "x",
            observed_end=observed_end,
            max_scan_reached=not observed_end,
            visible_total=0,
            frontier_ids=(),
        )

        with pytest.raises(ValueError, match="terminal"):
            module.fail_sync(started.job_id, "x", "late_failure", "too late")

        assert module.get_status(started.job_id)["capture_status"] == expected


def test_replayed_batch_does_not_increment_counts_twice(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())
    items = [item(datetime(2026, 1, 1, tzinfo=UTC))]

    first = module.submit_batch(started.job_id, "x", "batch-1", items)
    replay = module.submit_batch(started.job_id, "x", "batch-1", items)

    assert isinstance(replay, SubmitBatchReceipt)
    assert replay == first
    assert module.get_status(started.job_id)["platforms"][0]["counts"]["scanned"] == 1


def test_batch_receipt_persists_sync_fingerprint_and_rejects_payload_reuse(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request())
    original = item(datetime(2026, 1, 1, tzinfo=UTC), source_id="stable")
    changed = replace(original, body="different body")

    first = module.submit_batch(started.job_id, "x", "same-key", [original])
    replay = module.submit_batch(started.job_id, "x", "same-key", [original])

    assert replay == first
    payload = json.loads(
        database.connection.execute(
            """
            SELECT receipt_json FROM sync_batches
            WHERE job_id = ? AND platform = ? AND idempotency_key = ?
            """,
            (started.job_id, "x", "same-key"),
        ).fetchone()[0]
    )
    metadata = payload["_sync"]
    assert isinstance(metadata["request_fingerprint"], str)
    assert metadata["scanned"] == 1
    assert metadata["out_of_range"] == 0
    assert metadata["counts_applied"] is True

    with pytest.raises(ValueError, match="payload"):
        module.submit_batch(started.job_id, "x", "same-key", [changed])
    assert module.get_status(started.job_id)["platforms"][0]["counts"]["scanned"] == 1


def test_submit_batch_calls_library_inside_caller_transaction(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request())
    original = module.library.ingest_batch
    observed: list[bool] = []

    def wrapped(*args: object, **kwargs: object) -> object:
        observed.append(database.connection.in_transaction)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    module.library.ingest_batch = wrapped  # type: ignore[method-assign]
    module.submit_batch(
        started.job_id,
        "x",
        "transactional",
        [item(datetime(2026, 1, 1, tzinfo=UTC))],
    )

    assert observed == [True]


def test_max_scan_is_enforced_and_finish_stays_partial(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request(max_scan_items=1))
    values = [
        item(datetime(2026, 1, 1, tzinfo=UTC), source_id="first"),
        item(datetime(2026, 1, 2, tzinfo=UTC), source_id="second"),
    ]

    receipt = module.submit_batch(started.job_id, "x", "limited", values)
    assert receipt.added == 1
    assert module.get_status(started.job_id)["platforms"][0]["counts"] == {
        "scanned": 1,
        "added": 1,
        "refreshed": 0,
        "duplicates": 0,
        "out_of_range": 0,
    }
    assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    payload = json.loads(
        database.connection.execute(
            """
            SELECT receipt_json FROM sync_batches
            WHERE job_id = ? AND platform = ? AND idempotency_key = ?
            """,
            (started.job_id, "x", "limited"),
        ).fetchone()[0]
    )
    assert payload["_sync"]["truncated"] is True

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=2,
        frontier_ids=("first",),
    )
    platform = module.get_status(started.job_id)["platforms"][0]
    assert platform["status"] == "partial"
    assert platform["max_scan_reached"] is True
    assert platform["observed_end"] is False


def test_max_scan_equal_boundary_completes_when_batch_was_not_truncated(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request(max_scan_items=1))

    module.submit_batch(
        started.job_id,
        "x",
        "exact-limit",
        [item(datetime(2026, 1, 1, tzinfo=UTC), source_id="only")],
    )
    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=1,
        frontier_ids=("only",),
    )

    platform = module.get_status(started.job_id)["platforms"][0]
    assert platform["status"] == "completed"
    assert platform["max_scan_reached"] is False
    assert platform["observed_end"] is True


def test_later_nonempty_batch_marks_exactly_exhausted_scan_as_partial(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request(max_scan_items=1))
    first = item(datetime(2026, 1, 1, tzinfo=UTC), source_id="first")
    beyond_limit = item(datetime(2026, 1, 2, tzinfo=UTC), source_id="beyond-limit")

    module.submit_batch(started.job_id, "x", "first-batch", [first])
    receipt = module.submit_batch(started.job_id, "x", "beyond-cap", [beyond_limit])
    replay = module.submit_batch(started.job_id, "x", "beyond-cap", [beyond_limit])

    assert replay == receipt
    assert (receipt.added, receipt.refreshed, receipt.duplicates, receipt.out_of_range) == (
        0,
        0,
        0,
        0,
    )
    status = module.get_status(started.job_id)
    assert status["platforms"][0]["counts"] == {
        "scanned": 1,
        "added": 1,
        "refreshed": 0,
        "duplicates": 0,
        "out_of_range": 0,
    }
    assert status["platforms"][0]["max_scan_reached"] is True
    assert database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 1
    payload = json.loads(
        database.connection.execute(
            """
            SELECT receipt_json FROM sync_batches
            WHERE job_id = ? AND platform = ? AND idempotency_key = ?
            """,
            (started.job_id, "x", "beyond-cap"),
        ).fetchone()[0]
    )
    assert payload["_sync"]["counts_applied"] is True
    assert payload["_sync"]["scanned"] == 0
    assert payload["_sync"]["truncated"] is True
    assert isinstance(payload["_sync"]["request_fingerprint"], str)

    with pytest.raises(ValueError, match="payload"):
        module.submit_batch(
            started.job_id,
            "x",
            "beyond-cap",
            [replace(beyond_limit, body="different")],
        )

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=2,
        frontier_ids=("first",),
    )
    platform = module.get_status(started.job_id)["platforms"][0]
    assert platform["status"] == "partial"
    assert platform["max_scan_reached"] is True
    assert platform["observed_end"] is False


def test_finish_scan_at_cap_preserves_last_completed_frontier(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    database.connection.execute(
        "INSERT INTO sync_frontiers(platform, source_ids_json, updated_at) VALUES (?, ?, ?)",
        ("x", json.dumps(["previous"]), "2026-07-18T00:00:00.000000Z"),
    )
    started = module.start_sync(request(max_scan_items=1))
    module.submit_batch(
        started.job_id,
        "x",
        "capped",
        [
            item(datetime(2026, 1, 1, tzinfo=UTC), source_id="first"),
            item(datetime(2026, 1, 2, tzinfo=UTC), source_id="not-scanned"),
        ],
    )

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=False,
        max_scan_reached=False,
        visible_total=2,
        frontier_ids=("first", "not-scanned"),
    )

    persisted = database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(persisted[0]) == ["previous"]


def test_start_sync_validates_request_and_returns_frontiers(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    database.connection.execute(
        "INSERT INTO sync_frontiers(platform, source_ids_json, updated_at) VALUES (?, ?, ?)",
        ("x", json.dumps(["old-1", "old-2"]), "2026-07-18T00:00:00.000000Z"),
    )
    started = module.start_sync(request(platforms=("x", "bilibili"), max_scan_items=10))
    assert started.frontiers == {"x": ("old-1", "old-2"), "bilibili": ()}
    mode = database.connection.execute(
        "SELECT mode FROM sync_jobs WHERE id = ?", (started.job_id,)
    ).fetchone()[0]
    assert mode == "incremental"
    with pytest.raises(ValueError):
        module.start_sync(request(platforms=()))
    with pytest.raises(ValueError):
        module.start_sync(request(platforms=("x", "x")))
    with pytest.raises(ValueError):
        module.start_sync(
            request(
                published_since=datetime(2025, 1, 1),
                published_until=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
    with pytest.raises(ValueError):
        module.start_sync(request(max_scan_items=0))


def test_full_sync_ignores_persisted_frontier_but_incremental_returns_it(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    database.connection.execute(
        "INSERT INTO sync_frontiers(platform, source_ids_json, updated_at) VALUES (?, ?, ?)",
        ("x", json.dumps(["old-1", "old-2"]), "2026-07-18T00:00:00.000000Z"),
    )

    full = module.start_sync(request(mode=SyncMode.FULL))
    incremental = module.start_sync(request(mode=SyncMode.INCREMENTAL))

    assert full.frontiers == {"x": ()}
    assert incremental.frontiers == {"x": ("old-1", "old-2")}


def test_incremental_start_normalizes_legacy_frontier_size_and_duplicates(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    persisted = ["new", "new", *(f"old-{index}" for index in range(25))]
    database.connection.execute(
        "INSERT INTO sync_frontiers(platform, source_ids_json, updated_at) VALUES (?, ?, ?)",
        ("x", json.dumps(persisted), "2026-07-18T00:00:00.000000Z"),
    )

    started = module.start_sync(request(mode=SyncMode.INCREMENTAL))

    assert started.frontiers == {"x": ("new", *(f"old-{index}" for index in range(19)))}


def test_finish_scan_merges_incremental_frontier_with_deduplication_and_limit(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    old_ids = [f"old-{index}" for index in range(25)]
    database.connection.execute(
        "INSERT INTO sync_frontiers(platform, source_ids_json, updated_at) VALUES (?, ?, ?)",
        ("x", json.dumps(old_ids), "2026-07-18T00:00:00.000000Z"),
    )
    started = module.start_sync(request(mode=SyncMode.INCREMENTAL))

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=2,
        frontier_ids=("new", "old-0", "new"),
    )

    persisted = database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(persisted[0]) == ["new", *old_ids[:19]]


def test_finish_scan_full_frontier_replaces_old_values_and_is_bounded(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    database.connection.execute(
        "INSERT INTO sync_frontiers(platform, source_ids_json, updated_at) VALUES (?, ?, ?)",
        ("x", json.dumps(["old"]), "2026-07-18T00:00:00.000000Z"),
    )
    started = module.start_sync(request(mode=SyncMode.FULL))
    scanned = tuple(["new", "new", *(f"item-{index}" for index in range(25))])

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=len(scanned),
        frontier_ids=scanned,
    )

    persisted = database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(persisted[0]) == ["new", *(f"item-{index}" for index in range(19))]


def test_finish_scan_completes_job_only_after_all_platforms_terminal(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request(platforms=("x", "bilibili")))
    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=2,
        frontier_ids=("a",),
    )
    assert module.get_status(started.job_id)["capture_status"] == "running"
    module.finish_scan(
        started.job_id,
        "bilibili",
        observed_end=True,
        max_scan_reached=True,
        visible_total=5,
        frontier_ids=("b",),
    )
    status = module.get_status(started.job_id)
    assert status["capture_status"] == "partial"
    bilibili, x = status["platforms"]
    assert x["status"] == "completed"
    assert bilibili["status"] == "partial"
    assert bilibili["visible_total"] == 5
    frontier = database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(frontier[0]) == ["a"]


def test_submit_rejects_unknown_job_or_platform_before_side_effect(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, store = sync_components
    started = module.start_sync(request())
    with pytest.raises(KeyError):
        module.submit_batch("missing", "x", "batch", [item(datetime(2026, 1, 1, tzinfo=UTC))])
    with pytest.raises(KeyError):
        module.submit_batch(
            started.job_id,
            "bilibili",
            "batch",
            [item(datetime(2026, 1, 1, tzinfo=UTC))],
        )
    assert not (store.items_root / "x").exists()


def test_enrichment_pending_includes_running_until_completed(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())
    module.library.queue.enqueue("x", "pending-item", "enrich_item", "hash-1")
    claimed = module.library.queue.claim_next()
    assert claimed is not None

    assert module.get_status(started.job_id)["enrichment_pending"] == 1
    module.library.queue.complete(claimed.id)
    assert module.get_status(started.job_id)["enrichment_pending"] == 0


def test_submit_rolls_back_library_work_when_batch_transaction_fails(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())
    original_ingest = module.library.ingest_batch

    def ingest_then_fail(
        job_id: str,
        platform: str,
        idempotency_key: str,
        items: list[CapturedItem],
        refresh_existing: bool,
    ) -> object:
        original_ingest(job_id, platform, idempotency_key, items, refresh_existing)
        raise RuntimeError("injected batch failure")

    module.library.ingest_batch = ingest_then_fail  # type: ignore[method-assign]
    values = [item(datetime(2026, 1, 1, tzinfo=UTC))]
    with pytest.raises(RuntimeError, match="injected batch failure"):
        module.submit_batch(started.job_id, "x", "race", values)
    assert module.get_status(started.job_id)["platforms"][0]["counts"]["scanned"] == 0
    assert module.database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert (
        module.database.connection.execute("SELECT COUNT(*) FROM sync_batches").fetchone()[0] == 0
    )


def test_get_status_returns_platforms_as_stably_ordered_list(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request(platforms=("x", "bilibili")))

    platforms = module.get_status(started.job_id)["platforms"]

    assert [entry["platform"] for entry in platforms] == ["bilibili", "x"]
    assert platforms[0]["counts"]["scanned"] == 0


def test_finish_scan_replay_does_not_overwrite_terminal_details(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request())
    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=10,
        frontier_ids=("stable",),
    )
    original_run = tuple(
        database.connection.execute(
            """
            SELECT status, observed_end, max_scan_reached, visible_total
            FROM sync_platform_runs WHERE job_id = ? AND platform = 'x'
            """,
            (started.job_id,),
        ).fetchone()
    )
    original_frontier = database.connection.execute(
        "SELECT source_ids_json, updated_at FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    original_frontier = tuple(original_frontier)

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=False,
        max_scan_reached=True,
        visible_total=1,
        frontier_ids=("changed",),
    )

    replayed_run = tuple(
        database.connection.execute(
            """
            SELECT status, observed_end, max_scan_reached, visible_total
            FROM sync_platform_runs WHERE job_id = ? AND platform = 'x'
            """,
            (started.job_id,),
        ).fetchone()
    )
    replayed_frontier = database.connection.execute(
        "SELECT source_ids_json, updated_at FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert replayed_run == original_run
    assert tuple(replayed_frontier) == original_frontier


def test_finish_scan_clears_pause_error_when_run_becomes_terminal(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())
    module.pause_sync(started.job_id, "x", "temporary", "retry")

    module.finish_scan(
        started.job_id,
        "x",
        observed_end=True,
        max_scan_reached=False,
        visible_total=1,
        frontier_ids=("done",),
    )

    platform = module.get_status(started.job_id)["platforms"][0]
    assert platform["status"] == "completed"
    assert platform["error"] is None


@pytest.mark.parametrize(
    ("observed_end", "max_scan_reached", "visible_total", "frontier_ids"),
    [
        (1, False, 1, ("valid",)),
        (True, 0, 1, ("valid",)),
        (True, False, -1, ("valid",)),
        (True, False, 1, (1,)),
    ],
)
def test_finish_scan_rejects_invalid_terminal_details_without_persisting(
    sync_components: tuple[SyncModule, Database, ItemStore],
    observed_end: object,
    max_scan_reached: object,
    visible_total: object,
    frontier_ids: tuple[object, ...],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(request())

    with pytest.raises(ValueError):
        module.finish_scan(  # type: ignore[arg-type]
            started.job_id,
            "x",
            observed_end=observed_end,
            max_scan_reached=max_scan_reached,
            visible_total=visible_total,
            frontier_ids=frontier_ids,
        )

    run = database.connection.execute(
        "SELECT status FROM sync_platform_runs WHERE job_id = ? AND platform = 'x'",
        (started.job_id,),
    ).fetchone()
    assert run["status"] == "running"
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM sync_frontiers WHERE platform = 'x'"
        ).fetchone()[0]
        == 0
    )


def scope_request(
    scope_ids: tuple[str, ...],
    *,
    mode: SyncMode = SyncMode.INCREMENTAL,
    scope_names: dict[str, str] | None = None,
) -> StartSyncRequest:
    return StartSyncRequest(
        platforms=("bilibili",),
        mode=mode,
        published_since=None,
        published_until=None,
        max_scan_items=None,
        scope_ids=scope_ids,
        scope_names=scope_names,
    )


def bili(source_id: str) -> CapturedItem:
    return item(datetime(2026, 1, 1, tzinfo=UTC), platform="bilibili", source_id=source_id)


def test_start_sync_creates_scope_runs_and_empty_frontiers(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(
        scope_request(("100001", "100002"), scope_names={"100001": "默认收藏夹"})
    )
    assert started.scoped_frontiers == {"100001": (), "100002": ()}
    rows = database.connection.execute(
        "SELECT scope_id, scope_name, status FROM sync_scope_runs "
        "WHERE job_id = ? ORDER BY scope_id",
        (started.job_id,),
    ).fetchall()
    assert [(r["scope_id"], r["scope_name"], r["status"]) for r in rows] == [
        ("100001", "默认收藏夹", "running"),
        ("100002", "100002", "running"),
    ]


def test_submit_batch_tracks_scope_scanned_independently(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001", "100002")))
    module.submit_batch(
        started.job_id,
        "bilibili",
        "batch-1",
        [bili("BV1")],
        scope_scans={"100001": ("BV1",), "100002": ("BV1", "BV2")},
    )
    status = module.get_status(started.job_id)
    scanned = {s["scope_id"]: s["counts"]["scanned"] for s in status["platforms"][0]["scopes"]}
    assert scanned == {"100001": 1, "100002": 2}


def test_finish_scan_advances_only_completed_scope_frontiers(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, database, _ = sync_components
    started = module.start_sync(scope_request(("100001", "100002")))
    module.finish_scan(
        started.job_id,
        "bilibili",
        observed_end=False,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"100001": ("BV1", "BV2")},
    )
    statuses = {
        r["scope_id"]: r["status"]
        for r in database.connection.execute(
            "SELECT scope_id, status FROM sync_scope_runs WHERE job_id = ?", (started.job_id,)
        )
    }
    assert statuses == {"100001": "completed", "100002": "partial"}
    frontier = database.connection.execute(
        "SELECT source_ids_json FROM sync_frontier_scopes "
        "WHERE platform = 'bilibili' AND scope_id = '100001'"
    ).fetchone()
    assert json.loads(frontier["source_ids_json"]) == ["BV1", "BV2"]
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM sync_frontier_scopes WHERE scope_id = '100002'"
        ).fetchone()[0]
        == 0
    )


def test_incremental_resume_loads_persisted_scope_frontier(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    first = module.start_sync(scope_request(("100001", "100002")))
    module.finish_scan(
        first.job_id,
        "bilibili",
        observed_end=False,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"100001": ("BV1",)},
    )
    second = module.start_sync(scope_request(("100001", "100002")))
    assert second.scoped_frontiers == {"100001": ("BV1",), "100002": ()}


def test_scope_rename_preserves_frontier(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    first = module.start_sync(scope_request(("100001",), scope_names={"100001": "旧名"}))
    module.finish_scan(
        first.job_id,
        "bilibili",
        observed_end=False,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"100001": ("BV1",)},
    )
    second = module.start_sync(scope_request(("100001",), scope_names={"100001": "新名"}))
    assert second.scoped_frontiers == {"100001": ("BV1",)}
    status = module.get_status(second.job_id)
    assert status["platforms"][0]["scopes"][0]["scope_name"] == "新名"


def test_full_mode_ignores_persisted_scope_frontier(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    first = module.start_sync(scope_request(("100001",)))
    module.finish_scan(
        first.job_id,
        "bilibili",
        observed_end=False,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"100001": ("BV1",)},
    )
    full = module.start_sync(scope_request(("100001",), mode=SyncMode.FULL))
    assert full.scoped_frontiers == {"100001": ()}


def test_submit_batch_rejects_unknown_scope(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001",)))
    with pytest.raises(KeyError, match="unknown scope"):
        module.submit_batch(
            started.job_id, "bilibili", "b1", [bili("BV1")], scope_scans={"999": ("BV1",)}
        )


def test_finish_scan_rejects_unknown_scope(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001",)))
    with pytest.raises(KeyError, match="unknown scope"):
        module.finish_scan(
            started.job_id,
            "bilibili",
            observed_end=False,
            max_scan_reached=False,
            visible_total=None,
            frontier_ids=(),
            frontier_scopes={"999": ()},
        )


def test_submit_batch_replay_does_not_double_count_scopes(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001",)))
    first = module.submit_batch(
        started.job_id, "bilibili", "b1", [bili("BV1")], scope_scans={"100001": ("BV1",)}
    )
    replay = module.submit_batch(
        started.job_id, "bilibili", "b1", [bili("BV1")], scope_scans={"100001": ("BV1",)}
    )
    assert replay == first
    status = module.get_status(started.job_id)
    assert status["platforms"][0]["scopes"][0]["counts"]["scanned"] == 1


def test_platform_status_includes_empty_scopes_for_legacy_jobs(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(request())
    status = module.get_status(started.job_id)
    assert status["platforms"][0]["scopes"] == []


def test_finish_scan_records_per_scope_results(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001", "100002")))
    module.finish_scan(
        started.job_id,
        "bilibili",
        observed_end=False,
        max_scan_reached=True,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"100002": ("BV1",)},
        scope_results={
            "100001": ScopeFinish(max_scan_reached=True, visible_total=40),
            "100002": ScopeFinish(max_scan_reached=False, visible_total=3),
        },
    )
    scopes = {
        scope["scope_id"]: scope
        for scope in module.get_status(started.job_id)["platforms"][0]["scopes"]
    }
    assert scopes["100001"]["max_scan_reached"] is True
    assert scopes["100001"]["visible_total"] == 40
    assert scopes["100001"]["status"] == "partial"
    assert scopes["100002"]["max_scan_reached"] is False
    assert scopes["100002"]["visible_total"] == 3
    assert scopes["100002"]["status"] == "completed"


def test_finish_scan_rejects_capped_scope_advancing_frontier(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001",)))
    with pytest.raises(ValueError, match="max_scan_reached"):
        module.finish_scan(
            started.job_id,
            "bilibili",
            observed_end=False,
            max_scan_reached=True,
            visible_total=None,
            frontier_ids=(),
            frontier_scopes={"100001": ("BV1",)},
            scope_results={"100001": ScopeFinish(max_scan_reached=True, visible_total=None)},
        )


def test_finish_scan_rejects_unknown_scope_result(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    module, _, _ = sync_components
    started = module.start_sync(scope_request(("100001",)))
    with pytest.raises(KeyError, match="unknown scope"):
        module.finish_scan(
            started.job_id,
            "bilibili",
            observed_end=False,
            max_scan_reached=False,
            visible_total=None,
            frontier_ids=(),
            frontier_scopes=None,
            scope_results={"999": ScopeFinish(max_scan_reached=False, visible_total=None)},
        )


# -- Task 2: resume and browser-discovered scopes ----------------------------


def test_resume_sync_clears_pause_without_losing_counts(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.submit_batch(
        started.job_id,
        "bilibili",
        "b-0001",
        (item(datetime(2026, 3, 1, tzinfo=UTC), platform="bilibili", source_id="BV1"),),
    )
    sync.pause_sync(started.job_id, "bilibili", "browser_unavailable", "closed")
    sync.resume_sync(started.job_id, "bilibili")

    platform = sync.get_status(started.job_id)["platforms"][0]
    assert platform["status"] == "running"
    assert platform["error"] is None
    assert platform["counts"]["scanned"] == 1


def test_resume_sync_replays_on_a_running_run(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.resume_sync(started.job_id, "bilibili")
    sync.resume_sync(started.job_id, "bilibili")
    assert sync.get_status(started.job_id)["platforms"][0]["status"] == "running"


def test_resume_sync_returns_the_job_to_running(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.pause_sync(started.job_id, "bilibili", "rate_limited", "slow down")
    assert sync.get_status(started.job_id)["capture_status"] == "paused"
    sync.resume_sync(started.job_id, "bilibili")
    assert sync.get_status(started.job_id)["capture_status"] == "running"


def test_resume_sync_rejects_a_terminal_run(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.fail_sync(started.job_id, "bilibili", "storage_error", "disk full")
    with pytest.raises(ValueError):
        sync.resume_sync(started.job_id, "bilibili")


def test_resume_sync_rejects_unknown_job_and_platform(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    with pytest.raises(KeyError):
        sync.resume_sync("missing-job", "bilibili")
    with pytest.raises(KeyError):
        sync.resume_sync(started.job_id, "zhihu")


def test_register_scopes_adds_folders_discovered_by_the_browser(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("zhihu",)))
    frontiers = sync.register_scopes(
        started.job_id,
        "zhihu",
        {"42": "默认收藏夹", "99": "技术"},
    )
    assert frontiers == {"42": (), "99": ()}
    scopes = {
        scope["scope_id"]: scope["scope_name"]
        for scope in sync.get_status(started.job_id)["platforms"][0]["scopes"]
    }
    assert scopes == {"42": "默认收藏夹", "99": "技术"}


def test_register_scopes_returns_stored_frontiers_in_incremental_mode(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, database, _ = sync_components
    database.connection.execute(
        """INSERT INTO sync_frontier_scopes(platform, scope_id, source_ids_json, updated_at)
           VALUES ('zhihu', '42', '["a1","a2"]', '2026-08-02T00:00:00Z')""",
    )
    started = sync.start_sync(request(platforms=("zhihu",), mode=SyncMode.INCREMENTAL))
    frontiers = sync.register_scopes(started.job_id, "zhihu", {"42": "默认收藏夹", "99": "技术"})
    assert frontiers == {"42": ("a1", "a2"), "99": ()}


def test_register_scopes_ignores_frontiers_in_full_mode(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, database, _ = sync_components
    database.connection.execute(
        """INSERT INTO sync_frontier_scopes(platform, scope_id, source_ids_json, updated_at)
           VALUES ('zhihu', '42', '["a1"]', '2026-08-02T00:00:00Z')""",
    )
    started = sync.start_sync(request(platforms=("zhihu",), mode=SyncMode.FULL))
    assert sync.register_scopes(started.job_id, "zhihu", {"42": "默认收藏夹"}) == {"42": ()}


def test_register_scopes_replays_the_exact_map_idempotently(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    first = sync.register_scopes(started.job_id, "bilibili", {"1": "看过", "2": "收藏"})
    second = sync.register_scopes(started.job_id, "bilibili", {"1": "看过", "2": "收藏"})
    assert first == second
    assert len(sync.get_status(started.job_id)["platforms"][0]["scopes"]) == 2


def test_register_scopes_rejects_a_renamed_or_dropped_scope(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.register_scopes(started.job_id, "bilibili", {"1": "看过", "2": "收藏"})
    with pytest.raises(ValueError):
        sync.register_scopes(started.job_id, "bilibili", {"1": "改名了", "2": "收藏"})
    with pytest.raises(ValueError):
        sync.register_scopes(started.job_id, "bilibili", {"1": "看过"})


def test_register_scopes_may_add_a_scope_before_scanning_starts(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.register_scopes(started.job_id, "bilibili", {"1": "看过"})
    frontiers = sync.register_scopes(started.job_id, "bilibili", {"1": "看过", "2": "收藏"})
    assert set(frontiers) == {"1", "2"}


def test_register_scopes_is_rejected_after_the_first_scanned_item(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("bilibili",)))
    sync.register_scopes(started.job_id, "bilibili", {"1": "看过"})
    sync.submit_batch(
        started.job_id,
        "bilibili",
        "b-0001",
        (item(datetime(2026, 3, 1, tzinfo=UTC), platform="bilibili", source_id="BV1"),),
        scope_scans={"1": ("BV1",)},
    )
    with pytest.raises(ValueError):
        sync.register_scopes(started.job_id, "bilibili", {"1": "看过", "2": "收藏"})


def test_register_scopes_rejects_unscoped_platforms(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    for platform in ("x", "github"):
        started = sync.start_sync(request(platforms=(platform,)))
        with pytest.raises(ValueError):
            sync.register_scopes(started.job_id, platform, {"1": "nope"})


def test_register_scopes_rejects_a_blank_or_empty_map(
    sync_components: tuple[SyncModule, Database, ItemStore],
) -> None:
    sync, _, _ = sync_components
    started = sync.start_sync(request(platforms=("zhihu",)))
    with pytest.raises(ValueError):
        sync.register_scopes(started.job_id, "zhihu", {})
    with pytest.raises(ValueError):
        sync.register_scopes(started.job_id, "zhihu", {"42": "  "})
