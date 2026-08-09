from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from favhub.browser_capture import (
    BROWSER_PROTOCOL_VERSION,
    BrowserCaptureStatus,
    BrowserCaptureStore,
)
from favhub.browser_gateway import BROWSER_PLATFORMS, BrowserGateway
from favhub.database import Database
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.sync_gateway import SyncArgumentError, SyncGateway
from favhub.sync_module import SyncModule


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[BrowserGateway, BrowserCaptureStore, SyncModule]]:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    sync = SyncModule(database, library)
    sessions = BrowserCaptureStore(database)
    gateway = BrowserGateway(SyncGateway(sync), sync, sessions)
    try:
        yield gateway, sessions, sync
    finally:
        database.close()


def start_arguments(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {"platform": "zhihu", "mode": "incremental"}
    arguments.update(overrides)
    return arguments


def test_browser_platforms_exclude_github() -> None:
    assert sorted(BROWSER_PLATFORMS) == ["bilibili", "x", "zhihu"]


def test_a_start_without_a_mode_is_incremental(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    """Omitting mode has to mean incremental, not fail.

    The safe default is not symmetric. Incremental on a platform with no
    frontier scans to the end exactly like full, so a caller who meant full
    loses a re-run at worst; full on a library that already has items rewrites
    them. The schema says optional — this is the half that makes it true.
    """
    gateway, _, sync = stack
    started = gateway.start({"platform": "zhihu"})
    assert sync.get_status(started["job_id"])["mode"] == "incremental"


def test_start_creates_a_sync_job_and_an_awaiting_session(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, sync = stack
    started = gateway.start(start_arguments())
    session = started["browser_session"]
    assert session["status"] == "awaiting_browser"
    assert session["platform"] == "zhihu"
    assert session["protocol_version"] == BROWSER_PROTOCOL_VERSION
    assert session["job_id"] == started["job_id"]
    assert sync.get_status(started["job_id"])["platforms"][0]["status"] == "running"


def test_start_opens_the_page_only_after_the_session_exists(
    tmp_path: Path,
) -> None:
    """The browser must never arrive before there is a session to claim."""
    database = Database.open(tmp_path / "ordering.sqlite3")
    try:
        store = ItemStore(tmp_path / "items")
        library = LibraryModule(database, store, EnrichmentQueue(database))
        sync = SyncModule(database, library)
        sessions = BrowserCaptureStore(database)
        opened: list[str] = []

        def open_page(platform: str) -> str | None:
            # A session is already waiting by the time the page is asked for.
            assert sessions.find_open(platform) is not None
            opened.append(platform)
            return f"https://example.invalid/{platform}"

        gateway = BrowserGateway(SyncGateway(sync), sync, sessions, open_page=open_page)
        started = gateway.start(start_arguments(platform="x"))
        assert opened == ["x"]
        assert started["opened_url"] == "https://example.invalid/x"
    finally:
        database.close()


def test_a_browser_that_will_not_open_still_leaves_a_waiting_session(
    tmp_path: Path,
) -> None:
    """Reported, not raised: opening the page by hand still starts the run."""
    database = Database.open(tmp_path / "nobrowser.sqlite3")
    try:
        store = ItemStore(tmp_path / "items")
        library = LibraryModule(database, store, EnrichmentQueue(database))
        sync = SyncModule(database, library)
        sessions = BrowserCaptureStore(database)
        gateway = BrowserGateway(
            SyncGateway(sync), sync, sessions, open_page=lambda _platform: None
        )
        started = gateway.start(start_arguments(platform="x"))
        assert started["opened_url"] is None
        assert started["browser_session"]["status"] == "awaiting_browser"
    finally:
        database.close()


def _versioned_stack(tmp_path: Path, installed: str | None) -> tuple[BrowserGateway, Database]:
    database = Database.open(tmp_path / f"versioned-{installed}.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    sync = SyncModule(database, library)
    gateway = BrowserGateway(
        SyncGateway(sync),
        sync,
        BrowserCaptureStore(database),
        installed_version=lambda: installed,
    )
    return gateway, database


def test_a_stale_loaded_extension_is_refused_before_the_session_is_touched(
    tmp_path: Path,
) -> None:
    gateway, database = _versioned_stack(tmp_path, "0.2.0")
    try:
        started = gateway.start(start_arguments(platform="x"))

        claimed = gateway.claim_for_extension({"platform": "x", "extensionVersion": "0.1.0"})

        # Chrome keeps running what it loaded until someone clicks Reload, so an
        # upgraded install collecting through old adapter code would produce
        # results indistinguishable from good ones.
        assert claimed["session"] is None
        assert claimed["error"]["code"] == "extension_version_mismatch"
        assert "0.1.0" in claimed["error"]["message"]
        assert "0.2.0" in claimed["error"]["message"]
        assert "Reload" in claimed["error"]["message"]
        # The waiting session is untouched, so the click is all the fix needs.
        status = gateway.status({"jobId": started["job_id"]})
        assert status["browser_sessions"][0]["status"] == "awaiting_browser"
    finally:
        database.close()


def test_a_matching_extension_claims_normally(tmp_path: Path) -> None:
    gateway, database = _versioned_stack(tmp_path, "0.2.0")
    try:
        gateway.start(start_arguments(platform="x"))
        claimed = gateway.claim_for_extension({"platform": "x", "extensionVersion": "0.2.0"})
        assert claimed["session"]["status"] == "capturing"
    finally:
        database.close()


def test_an_unknowable_installed_version_never_blocks_a_run(tmp_path: Path) -> None:
    """A source checkout has no installed copy; refusing there helps nobody."""
    gateway, database = _versioned_stack(tmp_path, None)
    try:
        gateway.start(start_arguments(platform="x"))
        claimed = gateway.claim_for_extension({"platform": "x", "extensionVersion": "0.1.0"})
        assert claimed["session"]["status"] == "capturing"
    finally:
        database.close()


def test_start_allows_scopeless_bilibili_and_zhihu(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    for platform in ("bilibili", "zhihu"):
        started = gateway.start(start_arguments(platform=platform))
        assert started["scoped_frontiers"] == {}
        gateway.cancel({"jobId": started["job_id"], "platform": platform})


def test_start_rejects_github_and_unknown_platforms(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    for platform in ("github", "reddit"):
        with pytest.raises(SyncArgumentError):
            gateway.start(start_arguments(platform=platform))


def test_start_rejects_a_second_session_for_the_same_platform(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    gateway.start(start_arguments())
    with pytest.raises(SyncArgumentError):
        gateway.start(start_arguments())


def test_start_fails_the_sync_run_when_the_session_cannot_be_created(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    """A job with no browser session would sit running forever; fail it instead."""
    gateway, _, sync = stack
    first = gateway.start(start_arguments())
    with pytest.raises(SyncArgumentError):
        gateway.start(start_arguments())
    # The first run is untouched; only the rejected one is cleaned up.
    assert sync.get_status(first["job_id"])["platforms"][0]["status"] == "running"
    failed = [
        row
        for row in sync.database.connection.execute(
            "SELECT job_id, status FROM sync_platform_runs WHERE status = 'failed'"
        )
    ]
    assert len(failed) == 1
    assert str(failed[0]["job_id"]) != first["job_id"]


def test_resume_lifts_the_pause_and_leaves_the_session_claimable(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, sessions, sync = stack
    started = gateway.start(start_arguments())
    session_id = str(started["browser_session"]["session_id"])
    sessions.claim(session_id, "0.1.0", lease_seconds=60)
    gateway.pause_for_browser(started["job_id"], "zhihu", "rate_limited", "slow down")

    resumed = gateway.resume({"jobId": started["job_id"], "platform": "zhihu"})
    assert resumed["browser_session"]["status"] == "paused"
    assert sync.get_status(started["job_id"])["platforms"][0]["status"] == "running"
    assert sessions.claim(session_id, "0.1.0", lease_seconds=60).status is (
        BrowserCaptureStatus.CAPTURING
    )


def test_resume_rejects_a_platform_with_no_open_session(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    started = gateway.start(start_arguments())
    gateway.cancel({"jobId": started["job_id"], "platform": "zhihu"})
    with pytest.raises(SyncArgumentError):
        gateway.resume({"jobId": started["job_id"], "platform": "zhihu"})


def test_resume_leaves_a_terminated_session_alone(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    """A terminal session is caught before the run is touched, so its pause stands."""
    gateway, sessions, sync = stack
    started = gateway.start(start_arguments())
    session_id = str(started["browser_session"]["session_id"])
    gateway.pause_for_browser(started["job_id"], "zhihu", "rate_limited", "slow down")
    sessions.cancel(session_id)
    with pytest.raises(SyncArgumentError):
        gateway.resume({"jobId": started["job_id"], "platform": "zhihu"})
    platform = sync.get_status(started["job_id"])["platforms"][0]
    assert platform["status"] == "paused"
    assert platform["error"]["code"] == "rate_limited"


def test_resume_repauses_the_run_when_the_session_is_already_capturing(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    """Resuming must not leave the run running with no claimable session.

    A session already in ``capturing`` is not resumable, so the run is put back
    to paused rather than reporting progress nothing is making.
    """
    gateway, sessions, sync = stack
    started = gateway.start(start_arguments())
    session_id = str(started["browser_session"]["session_id"])
    sessions.claim(session_id, "0.1.0", lease_seconds=60)
    sync.pause_sync(started["job_id"], "zhihu", "rate_limited", "slow down")

    with pytest.raises(SyncArgumentError):
        gateway.resume({"jobId": started["job_id"], "platform": "zhihu"})
    platform = sync.get_status(started["job_id"])["platforms"][0]
    assert platform["status"] == "paused"
    assert platform["error"]["code"] == "browser_unavailable"


def test_status_reports_the_sync_job_and_its_session(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    started = gateway.start(start_arguments())
    status = gateway.status({"jobId": started["job_id"]})
    assert status["job_id"] == started["job_id"]
    assert status["browser_sessions"][0]["platform"] == "zhihu"
    assert status["platforms"][0]["platform"] == "zhihu"


def test_status_recovers_expired_leases_before_reporting(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, sessions, _ = stack
    started = gateway.start(start_arguments())
    session_id = str(started["browser_session"]["session_id"])
    sessions.claim(session_id, "0.1.0", lease_seconds=-1)
    status = gateway.status({"jobId": started["job_id"]})
    assert status["browser_sessions"][0]["status"] == "paused"
    assert status["browser_sessions"][0]["error"]["code"] == "browser_unavailable"


def test_cancel_marks_the_session_and_pauses_without_advancing_frontiers(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, sessions, sync = stack
    started = gateway.start(start_arguments())
    cancelled = gateway.cancel({"jobId": started["job_id"], "platform": "zhihu"})
    assert cancelled["browser_session"]["status"] == "cancelled"
    platform = sync.get_status(started["job_id"])["platforms"][0]
    assert platform["status"] == "paused"
    assert platform["error"]["code"] == "cancelled_by_user"
    assert sessions.find_open("zhihu") is None
    frontiers = sync.database.connection.execute("SELECT COUNT(*) FROM sync_frontiers").fetchone()[
        0
    ]
    assert frontiers == 0


def test_cancel_replays_without_error(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    started = gateway.start(start_arguments())
    gateway.cancel({"jobId": started["job_id"], "platform": "zhihu"})
    replayed = gateway.cancel({"jobId": started["job_id"], "platform": "zhihu"})
    assert replayed["browser_session"]["status"] == "cancelled"


def test_arguments_are_validated(
    stack: tuple[BrowserGateway, BrowserCaptureStore, SyncModule],
) -> None:
    gateway, _, _ = stack
    with pytest.raises(SyncArgumentError):
        gateway.start({"mode": "incremental"})
    with pytest.raises(SyncArgumentError):
        gateway.start(start_arguments(mode="sideways"))
    with pytest.raises(SyncArgumentError):
        gateway.resume({"platform": "zhihu"})
    with pytest.raises(SyncArgumentError):
        gateway.status({})
