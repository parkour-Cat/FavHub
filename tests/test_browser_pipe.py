import json
import os
import sys
import threading
from collections.abc import Iterator
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client
from pathlib import Path
from typing import Any

import pytest

from favhub.browser_pipe import (
    DESCRIPTOR_SCHEMA_VERSION,
    MAX_PIPE_REQUEST_BYTES,
    BrowserPipeServer,
    PipeDescriptor,
)
from favhub.config import FavHubPaths

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="AF_PIPE is a Windows-only transport"
)


def echo(request: dict[str, Any]) -> dict[str, Any]:
    return {"echoed": request.get("type")}


@pytest.fixture
def paths(tmp_path: Path) -> FavHubPaths:
    built = FavHubPaths.from_root(tmp_path / "root")
    built.ensure()
    return built


@pytest.fixture
def server(paths: FavHubPaths) -> Iterator[BrowserPipeServer]:
    started = BrowserPipeServer(paths, echo)
    started.start()
    try:
        yield started
    finally:
        started.stop()


def call(descriptor: PipeDescriptor, payload: bytes, *, auth_key: bytes | None = None) -> bytes:
    client = Client(
        descriptor.pipe,
        family="AF_PIPE",
        authkey=auth_key if auth_key is not None else descriptor.auth_key,
    )
    try:
        client.send_bytes(payload)
        return bytes(client.recv_bytes())
    finally:
        client.close()


def test_start_publishes_a_descriptor_only_once_it_is_accepting(
    server: BrowserPipeServer, paths: FavHubPaths
) -> None:
    written = json.loads(paths.browser_pipe_descriptor.read_text(encoding="utf-8"))
    assert written["schemaVersion"] == DESCRIPTOR_SCHEMA_VERSION
    assert written["pid"] == os.getpid()
    assert written["pipe"].startswith(r"\\.\pipe\favhub-")
    assert len(written["authKey"]) >= 40
    # The descriptor is only useful if the listener already answers.
    response = call(server.descriptor, json.dumps({"type": "session.claim"}).encode("utf-8"))
    assert json.loads(response) == {"echoed": "session.claim"}


def test_a_request_and_response_round_trip_as_bytes(server: BrowserPipeServer) -> None:
    response = call(server.descriptor, json.dumps({"type": "session.heartbeat"}).encode("utf-8"))
    assert json.loads(response)["echoed"] == "session.heartbeat"


def test_a_wrong_auth_key_is_refused(server: BrowserPipeServer) -> None:
    with pytest.raises((AuthenticationError, OSError, EOFError)):
        call(server.descriptor, b'{"type": "session.claim"}', auth_key=b"wrong-key-entirely")


def test_malformed_json_gets_a_stable_error_not_a_crash(server: BrowserPipeServer) -> None:
    response = json.loads(call(server.descriptor, b"not json at all"))
    assert response["error"]["code"] == "invalid_message"
    # The server stays up for the next caller.
    assert json.loads(call(server.descriptor, b'{"type": "session.cancel"}'))["echoed"] == (
        "session.cancel"
    )


def test_a_non_object_request_is_rejected(server: BrowserPipeServer) -> None:
    response = json.loads(call(server.descriptor, b"[1, 2, 3]"))
    assert response["error"]["code"] == "invalid_message"


def test_an_oversize_request_is_rejected(
    server: BrowserPipeServer,
) -> None:
    oversize = b'{"padding": "' + b"x" * (MAX_PIPE_REQUEST_BYTES + 16) + b'"}'
    response = json.loads(call(server.descriptor, oversize))
    assert response["error"]["code"] == "message_too_large"


def test_a_handler_failure_becomes_a_stable_error(paths: FavHubPaths) -> None:
    def explode(_request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("handler blew up with secret detail")

    started = BrowserPipeServer(paths, explode)
    started.start()
    try:
        response = json.loads(call(started.descriptor, b'{"type": "session.claim"}'))
        assert response["error"]["code"] == "storage_error"
        assert "secret detail" not in json.dumps(response)
    finally:
        started.stop()


def test_two_clients_are_served_one_after_another(server: BrowserPipeServer) -> None:
    results: list[str] = []
    errors: list[BaseException] = []
    ready = threading.Barrier(2)

    def worker(name: str) -> None:
        try:
            ready.wait(timeout=5)
            for _ in range(5):
                response = call(server.descriptor, json.dumps({"type": name}).encode("utf-8"))
                results.append(json.loads(response)["echoed"])
        except BaseException as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=("session.claim",)),
        threading.Thread(target=worker, args=("session.finish",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert sorted(results) == ["session.claim"] * 5 + ["session.finish"] * 5


def test_stop_removes_the_descriptor_and_closes_the_listener(paths: FavHubPaths) -> None:
    started = BrowserPipeServer(paths, echo)
    started.start()
    descriptor = started.descriptor
    assert paths.browser_pipe_descriptor.is_file()
    started.stop()
    assert not paths.browser_pipe_descriptor.exists()
    with pytest.raises(OSError):
        call(descriptor, b'{"type": "session.claim"}')


def test_stop_is_idempotent(paths: FavHubPaths) -> None:
    started = BrowserPipeServer(paths, echo)
    started.start()
    started.stop()
    started.stop()


def test_stop_leaves_a_descriptor_written_by_another_process(paths: FavHubPaths) -> None:
    """Only remove what this server published; a live sibling keeps its own."""
    started = BrowserPipeServer(paths, echo)
    started.start()
    foreign = {
        "schemaVersion": DESCRIPTOR_SCHEMA_VERSION,
        "pipe": r"\\.\pipe\favhub-someone-else",
        "authKey": "b3RoZXI=",
        "pid": os.getpid() + 99_999,
        "protocolVersion": 1,
    }
    paths.browser_pipe_descriptor.write_text(json.dumps(foreign), encoding="utf-8")
    started.stop()
    assert json.loads(paths.browser_pipe_descriptor.read_text(encoding="utf-8")) == foreign


def test_the_context_manager_starts_and_stops(paths: FavHubPaths) -> None:
    with BrowserPipeServer(paths, echo) as running:
        assert json.loads(call(running.descriptor, b'{"type": "scope.declare"}'))["echoed"] == (
            "scope.declare"
        )
    assert not paths.browser_pipe_descriptor.exists()


def test_each_run_generates_a_fresh_pipe_name_and_key(paths: FavHubPaths) -> None:
    first = BrowserPipeServer(paths, echo)
    first.start()
    first_descriptor = first.descriptor
    first.stop()

    second = BrowserPipeServer(paths, echo)
    second.start()
    try:
        assert second.descriptor.pipe != first_descriptor.pipe
        assert second.descriptor.auth_key != first_descriptor.auth_key
    finally:
        second.stop()
