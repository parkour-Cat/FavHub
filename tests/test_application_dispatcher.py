import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from favhub.application import Application
from favhub.application_dispatcher import ApplicationDispatcher
from favhub.browser_capture import BROWSER_PROTOCOL_VERSION


@pytest.fixture
def application(tmp_path: Path) -> Iterator[Application]:
    app = Application.open(tmp_path / "root")
    try:
        yield app
    finally:
        app.close()


def test_operation_is_reentrant() -> None:
    """A pipe handler may call a helper that also takes the lock."""
    dispatcher = ApplicationDispatcher()
    with dispatcher.operation(), dispatcher.operation():
        pass


def test_the_database_is_usable_from_another_thread(application: Application) -> None:
    """Cross-thread use is required: the pipe listener runs off the stdio thread."""
    results: list[int] = []
    errors: list[BaseException] = []

    def read() -> None:
        try:
            row = application.database.connection.execute("SELECT COUNT(*) FROM items").fetchone()
            results.append(int(row[0]))
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()
    assert not errors
    assert results == [0]


def test_operations_from_two_threads_are_serialized(application: Application) -> None:
    """Two callers must never be inside the shared Application at once.

    Without the dispatcher, a pipe write and a stdio read can interleave on one
    SQLite connection; the observed overlap counter is what proves they cannot.
    """
    dispatcher = ApplicationDispatcher()
    inside = 0
    max_inside = 0
    guard = threading.Lock()
    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(job: int) -> None:
        nonlocal inside, max_inside
        try:
            ready.wait(timeout=5)
            for index in range(20):
                with dispatcher.operation():
                    with guard:
                        inside += 1
                        max_inside = max(max_inside, inside)
                    application.database.connection.execute(
                        """INSERT INTO sync_jobs(
                            id, mode, status, options_json, created_at, updated_at
                        ) VALUES (?, 'incremental', 'running', '{}', '2026-08-02', '2026-08-02')""",
                        (f"job-{job}-{index}",),
                    )
                    with guard:
                        inside -= 1
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(job,)) for job in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert max_inside == 1
    count = application.database.connection.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0]
    assert count == 40


def test_a_browser_session_and_a_status_read_do_not_corrupt_each_other(
    application: Application,
) -> None:
    dispatcher = ApplicationDispatcher()
    errors: list[BaseException] = []
    started: list[str] = []
    ready = threading.Barrier(2)

    def start_browser_runs() -> None:
        try:
            ready.wait(timeout=5)
            for index in range(10):
                with dispatcher.operation():
                    assert application.browser_gateway is not None
                    result = application.browser_gateway.start(
                        {"platform": "zhihu", "mode": "incremental"}
                    )
                    started.append(str(result["job_id"]))
                    application.browser_gateway.cancel(
                        {"jobId": result["job_id"], "platform": "zhihu"}
                    )
                    del index
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    def read_status() -> None:
        try:
            ready.wait(timeout=5)
            for _ in range(10):
                with dispatcher.operation():
                    assert application.retrieval is not None
                    application.retrieval.status()
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [threading.Thread(target=start_browser_runs), threading.Thread(target=read_status)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(started) == 10
    sessions = application.database.connection.execute(
        "SELECT COUNT(*) FROM browser_capture_sessions WHERE status = 'cancelled'"
    ).fetchone()[0]
    assert sessions == 10


def test_a_second_application_for_one_root_is_still_forbidden(tmp_path: Path) -> None:
    """The dispatcher relaxes threads, never processes."""
    root = tmp_path / "root"
    first = Application.open(root)
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            Application.open(root)
    finally:
        first.close()


def test_the_dispatcher_does_not_hide_errors(application: Application) -> None:
    dispatcher = ApplicationDispatcher()
    with pytest.raises(ValueError), dispatcher.operation():
        raise ValueError("boom")
    # The lock is released, so the next operation still proceeds.
    with dispatcher.operation():
        assert application.browser_sessions is not None
        assert application.browser_sessions.find_open("x") is None


def test_browser_sessions_are_reachable_from_the_application(application: Application) -> None:
    assert application.browser_sessions is not None
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "x", "mode": "full"})
    session = application.browser_sessions.find_open("x")
    assert session is not None
    assert session.job_id == started["job_id"]
    assert session.protocol_version == BROWSER_PROTOCOL_VERSION
