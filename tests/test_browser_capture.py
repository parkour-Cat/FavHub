from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from favhub.browser_capture import (
    BrowserCaptureError,
    BrowserCaptureSession,
    BrowserCaptureStatus,
    BrowserCaptureStore,
)
from favhub.database import Database

PROTOCOL_VERSION = 1


class FakeClock:
    """Advanceable clock so lease expiry tests never sleep."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def _seed_platform_run(database: Database, job_id: str, platform: str) -> None:
    timestamp = "2026-08-02T00:00:00Z"
    database.connection.execute(
        """INSERT INTO sync_jobs(id, mode, status, options_json, created_at, updated_at)
           VALUES (?, 'incremental', 'running', '{}', ?, ?)""",
        (job_id, timestamp, timestamp),
    )
    database.connection.execute(
        """INSERT INTO sync_platform_runs(job_id, platform, status, counts_json, error_json)
           VALUES (?, ?, 'running', '{}', NULL)""",
        (job_id, platform),
    )


@pytest.fixture
def store(tmp_path: Path) -> BrowserCaptureStore:
    database = Database.open(tmp_path / "favhub.sqlite3")
    _seed_platform_run(database, "job-a", "x")
    _seed_platform_run(database, "job-b", "x")
    _seed_platform_run(database, "job-c", "zhihu")
    return BrowserCaptureStore(database, clock=FakeClock())


def test_create_returns_an_awaiting_session(store: BrowserCaptureStore) -> None:
    session = store.create("job-a", "x", PROTOCOL_VERSION)
    assert isinstance(session, BrowserCaptureSession)
    assert session.status is BrowserCaptureStatus.AWAITING_BROWSER
    assert session.job_id == "job-a"
    assert session.platform == "x"
    assert session.protocol_version == PROTOCOL_VERSION
    assert session.extension_version is None
    assert session.lease_expires_at is None
    assert session.error is None
    assert session.finished_at is None
    assert len(session.id) == 36


def test_create_rejects_a_second_open_session_for_the_same_platform(
    store: BrowserCaptureStore,
) -> None:
    store.create("job-a", "x", PROTOCOL_VERSION)
    with pytest.raises(BrowserCaptureError):
        store.create("job-b", "x", PROTOCOL_VERSION)


def test_create_allows_another_platform_at_the_same_time(store: BrowserCaptureStore) -> None:
    store.create("job-a", "x", PROTOCOL_VERSION)
    other = store.create("job-c", "zhihu", PROTOCOL_VERSION)
    assert other.platform == "zhihu"


def test_create_allows_a_new_session_after_the_previous_one_completed(
    store: BrowserCaptureStore,
) -> None:
    first = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(first.id, "0.1.0", lease_seconds=60)
    store.complete(first.id)
    second = store.create("job-b", "x", PROTOCOL_VERSION)
    assert second.status is BrowserCaptureStatus.AWAITING_BROWSER


def test_find_open_returns_only_non_terminal_sessions(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    assert store.find_open("x") == store.get(created.id)
    store.cancel(created.id)
    assert store.find_open("x") is None


def test_claim_moves_awaiting_to_capturing_and_sets_a_lease(
    store: BrowserCaptureStore,
) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    claimed = store.claim(created.id, "0.1.0", lease_seconds=60)
    assert claimed.status is BrowserCaptureStatus.CAPTURING
    assert claimed.extension_version == "0.1.0"
    assert claimed.lease_expires_at == "2026-08-02T12:01:00.000000Z"


def test_claim_resumes_a_paused_session_and_clears_the_previous_error(
    store: BrowserCaptureStore,
) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)
    paused = store.pause(created.id, "rate_limited", "backing off")
    assert paused.error == ("rate_limited", "backing off")

    resumed = store.claim(created.id, "0.2.0", lease_seconds=30)
    assert resumed.status is BrowserCaptureStatus.CAPTURING
    assert resumed.error is None
    assert resumed.extension_version == "0.2.0"


def test_renew_pushes_a_capturing_lease_out_so_a_long_run_survives(
    store: BrowserCaptureStore,
) -> None:
    clock = store.clock
    assert isinstance(clock, FakeClock)
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)

    clock.advance(45)
    renewed = store.renew(created.id, lease_seconds=60)
    assert renewed.status is BrowserCaptureStatus.CAPTURING
    assert renewed.lease_expires_at == "2026-08-02T12:01:45.000000Z"

    # The whole point: a run that keeps renewing is never swept up, however
    # long it takes. Claiming cannot do this job — it refuses a capturing
    # session — so a heartbeat that only claimed left the lease where it was
    # and every run died one lease after it started.
    for _beat in range(5):
        clock.advance(45)
        store.renew(created.id, lease_seconds=60)
        assert store.recover_expired() == ()
    # Five minutes of collecting, well past the single lease that used to be
    # the hard ceiling on any run.
    assert store.get(created.id).status is BrowserCaptureStatus.CAPTURING


def test_renew_refuses_a_session_that_is_not_capturing(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    with pytest.raises(BrowserCaptureError, match="awaiting_browser"):
        store.renew(created.id, lease_seconds=60)

    store.claim(created.id, "0.1.0", lease_seconds=60)
    store.pause(created.id, "rate_limited", "backing off")
    with pytest.raises(BrowserCaptureError, match="paused"):
        store.renew(created.id, lease_seconds=60)


def test_claim_rejects_a_terminal_session(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.cancel(created.id)
    with pytest.raises(BrowserCaptureError):
        store.claim(created.id, "0.1.0", lease_seconds=60)


def test_pause_rejects_an_unknown_code(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)
    with pytest.raises(ValueError):
        store.pause(created.id, "something_went_wrong", "nope")


def test_pause_truncates_long_messages(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)
    paused = store.pause(created.id, "page_changed", "x" * 500)
    assert paused.error is not None
    assert len(paused.error[1]) == 200


def test_complete_sets_finished_at_once_and_replays_idempotently(
    store: BrowserCaptureStore,
) -> None:
    clock = FakeClock()
    store.clock = clock
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)
    completed = store.complete(created.id)
    assert completed.status is BrowserCaptureStatus.COMPLETED
    assert completed.finished_at is not None

    clock.advance(120)
    replayed = store.complete(created.id)
    assert replayed.finished_at == completed.finished_at


def test_complete_rejects_a_session_that_is_not_capturing(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    with pytest.raises(BrowserCaptureError):
        store.complete(created.id)


def test_cancel_is_allowed_from_any_non_terminal_state(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    cancelled = store.cancel(created.id)
    assert cancelled.status is BrowserCaptureStatus.CANCELLED
    assert cancelled.finished_at is not None


def test_one_terminal_state_cannot_become_another(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)
    store.complete(created.id)
    with pytest.raises(BrowserCaptureError):
        store.cancel(created.id)


def test_fail_records_a_terminal_error(store: BrowserCaptureStore) -> None:
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)
    failed = store.fail(created.id, "storage_error", "disk full")
    assert failed.status is BrowserCaptureStatus.FAILED
    assert failed.error == ("storage_error", "disk full")
    assert failed.finished_at is not None


def test_recover_expired_pauses_only_sessions_past_their_lease(
    store: BrowserCaptureStore,
) -> None:
    clock = FakeClock()
    store.clock = clock
    created = store.create("job-a", "x", PROTOCOL_VERSION)
    store.claim(created.id, "0.1.0", lease_seconds=60)

    clock.advance(30)
    assert store.recover_expired() == ()
    assert store.get(created.id).status is BrowserCaptureStatus.CAPTURING

    clock.advance(31)
    assert store.recover_expired() == (created.id,)
    recovered = store.get(created.id)
    assert recovered.status is BrowserCaptureStatus.PAUSED
    assert recovered.error == ("browser_unavailable", "capture lease expired")


def test_recover_expired_ignores_awaiting_and_terminal_sessions(
    store: BrowserCaptureStore,
) -> None:
    clock = FakeClock()
    store.clock = clock
    awaiting = store.create("job-a", "x", PROTOCOL_VERSION)
    clock.advance(10_000)
    assert store.recover_expired() == ()
    assert store.get(awaiting.id).status is BrowserCaptureStatus.AWAITING_BROWSER


def test_get_raises_for_an_unknown_session(store: BrowserCaptureStore) -> None:
    with pytest.raises(BrowserCaptureError):
        store.get("00000000-0000-0000-0000-000000000000")


def test_status_enum_has_exactly_the_designed_members() -> None:
    assert {status.value for status in BrowserCaptureStatus} == {
        "awaiting_browser",
        "capturing",
        "paused",
        "completed",
        "failed",
        "cancelled",
    }
