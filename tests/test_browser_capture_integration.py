"""End-to-end browser capture: framed native messages -> pipe -> MCP -> disk.

A fake extension is enough to prove the plumbing, and nothing more. It does not
prove that X, Bilibili, or Zhihu still answer the way the adapters expect —
only a real logged-in smoke can do that.
"""

import io
import json
import struct
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from favhub.application import Application
from favhub.application_dispatcher import ApplicationDispatcher
from favhub.browser_capture import BROWSER_PROTOCOL_VERSION, BrowserCaptureStatus
from favhub.browser_ingest import BrowserIngestor
from favhub.browser_pipe import BrowserPipeServer
from favhub.config import packaged_extension_version
from favhub.mcp_server import _handle_pipe_request
from favhub.native_host import run_native_host

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="AF_PIPE is a Windows-only transport"
)

FIXTURES = Path(__file__).parent / "fixtures"

# The fake extension reports the version this tree actually ships. A literal
# here rots at the next bump: the claim would be refused as a stale extension,
# and the failure would look like a broken pipe rather than a stale fixture.
EXTENSION_VERSION = packaged_extension_version()


def encode_native(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def decode_native(raw: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    offset = 0
    while offset < len(raw):
        (length,) = struct.unpack("<I", raw[offset : offset + 4])
        offset += 4
        messages.append(json.loads(raw[offset : offset + length].decode("utf-8")))
        offset += length
    return messages


@pytest.fixture
def running(tmp_path: Path) -> Iterator[tuple[Application, BrowserPipeServer]]:
    application = Application.open(tmp_path / "root")
    dispatcher = ApplicationDispatcher()
    assert application.browser_gateway is not None

    def handle(request: dict[str, Any]) -> dict[str, Any]:
        with dispatcher.operation():
            assert application.browser_gateway is not None
            return _handle_pipe_request(application.browser_gateway, request)

    server = BrowserPipeServer(application.paths, handle)
    server.start()
    try:
        yield application, server
    finally:
        server.stop()
        application.close()


def relay(server: BrowserPipeServer, *messages: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive the real relay over the real pipe, exactly as Chrome would."""
    stdout = io.BytesIO()
    run_native_host(
        io.BytesIO(b"".join(encode_native(message) for message in messages)),
        stdout,
        io.StringIO(),
        descriptor_loader=lambda: server.descriptor,
    )
    return decode_native(stdout.getvalue())


def envelope(request_id: str, message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": BROWSER_PROTOCOL_VERSION,
        "requestId": request_id,
        "type": message_type,
        "payload": payload,
    }


def test_a_cancel_travels_the_whole_path_and_lands_in_sqlite(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "x", "mode": "full"})
    job_id = str(started["job_id"])

    replies = relay(
        server,
        envelope("r-0001", "session.cancel", {"jobId": job_id, "platform": "x"}),
    )
    assert len(replies) == 1
    assert replies[0]["requestId"] == "r-0001"
    assert replies[0]["result"]["browser_session"]["status"] == "cancelled"

    assert application.browser_sessions is not None
    session = application.browser_sessions.for_job(job_id)[0]
    assert session.status is BrowserCaptureStatus.CANCELLED
    platform = application.sync.get_status(job_id)["platforms"][0]
    assert platform["status"] == "paused"
    assert platform["error"]["code"] == "cancelled_by_user"
    # Cancelling must never advance a frontier.
    assert (
        application.database.connection.execute("SELECT COUNT(*) FROM sync_frontiers").fetchone()[0]
        == 0
    )


def test_a_pause_from_the_browser_stops_both_halves(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "zhihu", "mode": "incremental"})
    job_id = str(started["job_id"])

    replies = relay(
        server,
        envelope(
            "r-0001",
            "session.pause",
            {
                "jobId": job_id,
                "platform": "zhihu",
                "code": "rate_limited",
                "message": "backing off",
            },
        ),
    )
    session = replies[0]["result"]["browser_session"]
    assert session["status"] == "paused"
    assert session["error"]["code"] == "rate_limited"
    assert application.sync.get_status(job_id)["platforms"][0]["status"] == "paused"


def test_a_credential_shaped_field_is_refused_at_the_boundary(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "x", "mode": "full"})

    replies = relay(
        server,
        envelope(
            "r-0001",
            "session.cancel",
            {
                "jobId": str(started["job_id"]),
                "platform": "x",
                "headers": {"cookie": "auth_token=secret"},
            },
        ),
    )
    assert replies[0]["error"]["code"] == "credential_field_rejected"
    assert "secret" not in json.dumps(replies[0])
    # The session is untouched: a rejected message changes nothing.
    assert application.browser_sessions is not None
    assert application.browser_sessions.find_open("x") is not None


def test_a_wrong_protocol_version_is_refused(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    _application, server = running
    replies = relay(
        server,
        {
            "protocolVersion": BROWSER_PROTOCOL_VERSION + 1,
            "requestId": "r-0001",
            "type": "session.cancel",
            "payload": {"jobId": "whatever", "platform": "x"},
        },
    )
    assert replies[0]["error"]["code"] == "protocol_mismatch"


def test_ingested_x_items_reach_disk_sqlite_and_the_frontier(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    """The ingest half, driven directly: the pipe carries it, this proves it lands."""
    application, _server = running
    assert application.browser_gateway is not None
    assert application.browser_sessions is not None
    started = application.browser_gateway.start({"platform": "x", "mode": "incremental"})
    job_id = str(started["job_id"])
    session_id = str(started["browser_session"]["session_id"])
    application.browser_sessions.claim(session_id, "0.1.0", lease_seconds=600)

    ingestor = BrowserIngestor(application.sync, application.browser_sessions)
    body = json.loads((FIXTURES / "x" / "bookmarks-page-1.json").read_text(encoding="utf-8"))
    ingestor.handle(
        session_id,
        {"type": "capture.response", "platform": "x", "kind": "x.bookmarks_page", "body": body},
    )
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=("1011048574412526505",),
        frontier_scopes=None,
        scope_results=None,
    )
    application.browser_sessions.complete(session_id)

    rows = [
        (str(row["source_id"]), str(row["item_dir"]))
        for row in application.database.connection.execute(
            "SELECT source_id, item_dir FROM items WHERE platform = 'x' ORDER BY source_id"
        )
    ]
    assert [source_id for source_id, _ in rows] == [
        "1011048574412526505",
        "1112444407014444922",
        "1816024121722501770",
    ]
    manifest = application.paths.root / rows[0][1] / "source.json"
    assert manifest.is_file()
    assert json.loads(manifest.read_text(encoding="utf-8"))["source_id"] == rows[0][0]

    receipts = application.database.connection.execute(
        "SELECT idempotency_key FROM sync_batches WHERE job_id = ?", (job_id,)
    ).fetchall()
    assert [str(row["idempotency_key"]) for row in receipts] == ["browser-batch-0001"]

    frontier = application.database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(str(frontier["source_ids_json"])) == ["1011048574412526505"]
    assert application.browser_sessions.get(session_id).status is BrowserCaptureStatus.COMPLETED


def test_the_relay_survives_a_pipe_that_went_away(tmp_path: Path) -> None:
    application = Application.open(tmp_path / "root")
    dispatcher = ApplicationDispatcher()

    def handle(request: dict[str, Any]) -> dict[str, Any]:
        with dispatcher.operation():
            assert application.browser_gateway is not None
            return _handle_pipe_request(application.browser_gateway, request)

    server = BrowserPipeServer(application.paths, handle)
    server.start()
    descriptor = server.descriptor
    server.stop()
    application.close()

    stdout = io.BytesIO()
    code = run_native_host(
        io.BytesIO(encode_native(envelope("r-0001", "session.cancel", {"jobId": "x"}))),
        stdout,
        io.StringIO(),
        descriptor_loader=lambda: descriptor,
    )
    assert code == 1
    assert decode_native(stdout.getvalue())[0]["error"]["code"] == "mcp_unavailable"


def test_the_full_extension_conversation_lands_items_on_disk(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    """claim -> capture.bundle -> finish, exactly as the extension speaks it."""
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start(
        {"platform": "x", "mode": "incremental", "maxScanItems": 10}
    )
    job_id = str(started["job_id"])

    claim = relay(
        server,
        envelope(
            "r-0001",
            "session.claim",
            {"platform": "x", "extensionVersion": EXTENSION_VERSION},
        ),
    )[0]
    session = claim["result"]["session"]
    assert session["status"] == "capturing"
    assert claim["result"]["maxScanItems"] == 10
    assert claim["result"]["frontier"] == []

    body = (FIXTURES / "x" / "bookmarks-page-1.json").read_text(encoding="utf-8")
    replies = relay(
        server,
        envelope(
            "r-0002",
            "capture.bundle",
            {
                "sessionId": session["session_id"],
                "platform": "x",
                "events": [
                    {
                        "type": "capture.response",
                        "platform": "x",
                        "kind": "x.bookmarks_page",
                        "body": json.loads(body),
                    }
                ],
            },
        ),
        envelope(
            "r-0003",
            "session.finish",
            {
                "sessionId": session["session_id"],
                "observedEnd": True,
                "maxScanReached": False,
                "frontierIds": ["1011048574412526505"],
            },
        ),
    )
    assert replies[0]["result"]["accepted"] is True
    assert replies[1]["result"]["browser_session"]["status"] == "completed"

    rows = [
        str(row["source_id"])
        for row in application.database.connection.execute(
            "SELECT source_id FROM items WHERE platform = 'x' ORDER BY source_id"
        )
    ]
    assert rows == [
        "1011048574412526505",
        "1112444407014444922",
        "1816024121722501770",
    ]
    frontier = application.database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(str(frontier["source_ids_json"])) == ["1011048574412526505"]
    assert application.sync.get_status(job_id)["platforms"][0]["status"] == "completed"


def test_a_claim_with_nothing_waiting_returns_no_session(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    _application, server = running
    reply = relay(
        server,
        envelope(
            "r-0001",
            "session.claim",
            {"platform": "x", "extensionVersion": EXTENSION_VERSION},
        ),
    )[0]
    assert reply["result"]["session"] is None


def test_a_bundle_for_a_session_that_is_not_open_is_refused(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "x", "mode": "full"})
    session_id = str(started["browser_session"]["session_id"])
    application.browser_gateway.cancel({"jobId": started["job_id"], "platform": "x"})

    reply = relay(
        server,
        envelope(
            "r-0001",
            "capture.bundle",
            {"sessionId": session_id, "platform": "x", "events": [{"kind": "x.bookmarks_page"}]},
        ),
    )[0]
    assert reply["error"]["code"] == "invalid_message"


def test_a_platform_error_envelope_pauses_the_run_from_the_pipe(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "x", "mode": "full"})
    job_id = str(started["job_id"])
    claim = relay(
        server,
        envelope(
            "r-0001",
            "session.claim",
            {"platform": "x", "extensionVersion": EXTENSION_VERSION},
        ),
    )[0]
    session_id = claim["result"]["session"]["session_id"]

    reply = relay(
        server,
        envelope(
            "r-0002",
            "capture.bundle",
            {
                "sessionId": session_id,
                "platform": "x",
                "events": [
                    {
                        "type": "capture.response",
                        "platform": "x",
                        "kind": "x.bookmarks_page",
                        "body": json.loads(
                            (FIXTURES / "x" / "logged-out.json").read_text(encoding="utf-8")
                        ),
                    }
                ],
            },
        ),
    )[0]
    assert reply["result"]["accepted"] is False
    assert reply["result"]["error"]["code"] == "login_required"
    platform = application.sync.get_status(job_id)["platforms"][0]
    assert platform["status"] == "paused"
    assert platform["error"]["code"] == "login_required"


def test_a_heartbeat_keeps_the_session_capturing(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    application.browser_gateway.start({"platform": "zhihu", "mode": "full"})
    claim = relay(
        server,
        envelope(
            "r-0001",
            "session.claim",
            {"platform": "zhihu", "extensionVersion": EXTENSION_VERSION},
        ),
    )[0]
    session_id = claim["result"]["session"]["session_id"]
    granted = claim["result"]["session"]["lease_expires_at"]
    # Long enough for the clock to advance past its own granularity. Windows
    # can hand out the same timestamp to two operations this close together,
    # and then "strictly later" fails for a heartbeat that worked perfectly.
    time.sleep(0.05)
    reply = relay(server, envelope("r-0002", "session.heartbeat", {"sessionId": session_id}))[0]
    assert reply["result"]["session"]["status"] == "capturing"
    # Strictly later, not merely present: a heartbeat that returned the session
    # unchanged also satisfied "not None", and every run died one lease after
    # it was claimed while this test stayed green.
    assert reply["result"]["session"]["lease_expires_at"] > granted


def test_scopes_declared_by_the_browser_come_back_with_frontiers(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    application, server = running
    assert application.browser_gateway is not None
    application.browser_gateway.start({"platform": "zhihu", "mode": "incremental"})
    claim = relay(
        server,
        envelope(
            "r-0001",
            "session.claim",
            {"platform": "zhihu", "extensionVersion": EXTENSION_VERSION},
        ),
    )[0]
    session_id = claim["result"]["session"]["session_id"]

    reply = relay(
        server,
        envelope(
            "r-0002",
            "scope.declare",
            {
                "sessionId": session_id,
                "scopes": [
                    {"scopeId": "42", "scopeName": "默认收藏夹"},
                    {"scopeId": "99", "scopeName": "技术"},
                ],
            },
        ),
    )[0]
    assert reply["result"]["frontiers"] == {"42": [], "99": []}


def test_a_scoped_finish_advances_only_the_folders_that_reached_their_end(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    """One truncated folder must not hold back the folders that completed.

    `finish_for_extension` used to send `frontier_scopes=None`, which collapsed
    every folder into the unscoped pair and made per-folder progress
    unreportable — the thing scoped platforms exist to keep.
    """
    application, server = running
    assert application.browser_gateway is not None
    started = application.browser_gateway.start({"platform": "bilibili", "mode": "incremental"})
    claim = relay(
        server,
        envelope(
            "r-0001",
            "session.claim",
            {"platform": "bilibili", "extensionVersion": EXTENSION_VERSION},
        ),
    )[0]
    session_id = claim["result"]["session"]["session_id"]

    relay(
        server,
        envelope(
            "r-0002",
            "scope.declare",
            {
                "sessionId": session_id,
                "scopes": [
                    {"scopeId": "7", "scopeName": "默认收藏夹"},
                    {"scopeId": "8", "scopeName": "技术"},
                ],
            },
        ),
    )

    reply = relay(
        server,
        envelope(
            "r-0003",
            "session.finish",
            {
                "sessionId": session_id,
                "jobId": started["job_id"],
                "platform": "bilibili",
                "observedEnd": False,
                "maxScanReached": True,
                "frontierIds": [],
                # Folder 8 is absent, not empty: FavHub refuses a scope that
                # reports a cap and names a frontier at the same time.
                "frontierScopes": {"7": ["BV1", "BV2"]},
                "scopeResults": {
                    "7": {"maxScanReached": False, "visibleTotal": None},
                    "8": {"maxScanReached": True, "visibleTotal": None},
                },
            },
        ),
    )[0]

    assert "error" not in reply, reply
    assert reply["result"]["browser_session"]["status"] == "completed"
    scopes = {
        scope["scope_id"]: scope
        for entry in reply["result"]["status"]["platforms"]
        if entry["platform"] == "bilibili"
        for scope in entry.get("scopes", [])
    }
    assert scopes["7"]["status"] == "completed"
    assert scopes["8"]["status"] == "partial"


def test_an_unserved_message_type_is_refused_by_name(
    running: tuple[Application, BrowserPipeServer],
) -> None:
    _application, server = running
    reply = relay(server, envelope("r-0001", "session.pause", {"jobId": "", "platform": ""}))[0]
    assert "error" in reply
