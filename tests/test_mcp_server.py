import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import favhub.mcp_server as mcp_module
from favhub.application import Application
from favhub.browser_gateway import BrowserGateway
from favhub.cli import main as cli_main
from favhub.database import Database
from favhub.domain import sha256_text
from favhub.enrich_gateway import EnrichGateway
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore, SourceSnapshotError
from favhub.library import LibraryModule
from favhub.mcp_server import PROTOCOL_VERSION, _UnavailableRetrieval, main, run_stdio
from favhub.retrieval import (
    CollectionMap,
    CollectionSummary,
    GetItemRequest,
    ItemResponse,
    PlatformSummary,
    RetrievalStatus,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from favhub.sync_gateway import Rejection, SyncGateway
from favhub.sync_module import SyncModule

FIXTURE = Path(__file__).parent / "fixtures" / "captured-items.json"
PRIVATE_PATH = r"D:\private\root\items\x\42\source.json"


@dataclass
class _RetrievalStub:
    search_request: SearchRequest | None = None
    item_request: GetItemRequest | None = None
    status_error: Exception | None = None

    def search(self, request: SearchRequest) -> SearchResponse:
        self.search_request = request
        if request.query == "invalid":
            raise ValueError("query is invalid")
        if request.query == "rejected":
            raise Rejection("queries must name a field the index carries")
        if request.query == "offline":
            raise RuntimeError(f"index unavailable at {PRIVATE_PATH}")
        return SearchResponse(
            found=True,
            hits=(
                SearchHit(
                    platform="x",
                    source_id="42",
                    title="A result",
                    author="Author",
                    published_at="2026-01-01T00:00:00Z",
                    content_type="text",
                    excerpt="matching text",
                    canonical_url="https://example.com/42",
                    local_path="items/x/42/content.md",
                    line_start=1,
                    line_end=2,
                    citation_id="favhub:x/42#chunk-0",
                ),
            ),
            index_summary={"indexed_items": 1, "indexed_chunks": 1},
            total_returned=1,
        )

    def get_item(self, request: GetItemRequest) -> ItemResponse:
        self.item_request = request
        if request.source_id == "missing":
            raise KeyError("item not found: x/missing")
        return ItemResponse(
            platform=request.platform,
            source_id=request.source_id,
            source={"title": "A result"},
            files=("source.json", "content.md"),
            system_content={"content.md": "matching text"} if request.include_content else {},
        )

    def status(self) -> RetrievalStatus:
        if self.status_error is not None:
            raise self.status_error
        return RetrievalStatus(1, 2, 3, 4)

    def collections(self) -> CollectionMap:
        return CollectionMap(
            (CollectionSummary("bilibili", "钢琴", 8),),
            (
                PlatformSummary("bilibili", 9, 1),
                PlatformSummary("github", 296, 296),
            ),
        )


def _request(
    request_id: str | int | float, method: str, params: object | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        payload["params"] = params
    return payload


def _initialized_messages(*requests: dict[str, Any]) -> str:
    messages: list[dict[str, Any]] = [
        _request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        ),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        *requests,
    ]
    return "".join(json.dumps(message) + "\n" for message in messages)


def _run(payload: str, service: Any | None = None) -> tuple[list[dict[str, Any]], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    run_stdio(service or _RetrievalStub(), io.StringIO(payload), stdout, stderr)
    lines = stdout.getvalue().splitlines()
    return [json.loads(line) for line in lines], stderr.getvalue()


def test_initialize_negotiates_supported_version_and_initialized_is_silent() -> None:
    responses, stderr = _run(_initialized_messages())

    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "favhub", "version": "0.1.0"},
            },
        }
    ]
    assert stderr == ""


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_constants_are_parse_errors_without_initializing(
    constant: str,
) -> None:
    invalid_initialize = (
        '{"jsonrpc":"2.0","id":99,"method":"initialize","params":'
        f'{{"protocolVersion":{constant},"capabilities":{{}},'
        '"clientInfo":{"name":"pytest","version":"1"}}}\n'
    )
    valid_initialize = json.dumps(
        _request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1"},
            },
        )
    )

    responses, _ = _run(invalid_initialize + valid_initialize + "\n")

    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "parse error"},
    }
    assert responses[1]["id"] == 1
    assert responses[1]["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_finite_fractional_request_id_is_returned_unchanged() -> None:
    responses, _ = _run(_initialized_messages(_request(1.5, "tools/list", {})))

    assert responses[1]["id"] == 1.5
    assert "tools" in responses[1]["result"]


def test_initialize_notification_does_not_advance_lifecycle() -> None:
    payload = "".join(
        json.dumps(message) + "\n"
        for message in (
            {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _request(2, "tools/list", {}),
        )
    )

    responses, _ = _run(payload)

    assert len(responses) == 1
    assert responses[0]["id"] == 2
    assert responses[0]["error"] == {
        "code": -32600,
        "message": "server is not initialized",
    }


def test_initialized_request_is_rejected_without_advancing_lifecycle() -> None:
    payload = (
        _initialized_messages().replace(
            '{"jsonrpc": "2.0", "method": "notifications/initialized"}',
            '{"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized"}',
        )
        + json.dumps(_request(3, "tools/list", {}))
        + "\n"
    )

    responses, _ = _run(payload)

    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[1]["error"] == {
        "code": -32600,
        "message": "notifications/initialized must be a notification",
    }
    assert responses[2]["error"] == {
        "code": -32600,
        "message": "server is not initialized",
    }


def test_tools_list_advertises_exact_closed_schemas() -> None:
    responses, _ = _run(_initialized_messages(_request(2, "tools/list", {})))

    tools = responses[1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "favhub.search",
        "favhub.get_item",
        "favhub.collections",
        "favhub.status",
    ]
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["favhub.search"] == {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "platforms": {"type": "array", "items": {"type": "string"}},
            "contentTypes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Filter by the saved item's primary media type; omit for topic "
                    "or subject searches."
                ),
            },
            "collections": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Restrict to the user's own folder names. A named folder is a "
                    "deliberate choice and a strong signal; a platform's default "
                    "folder collects one-click saves and is a weak one."
                ),
            },
            "publishedSince": {"type": "string", "format": "date-time"},
            "publishedUntil": {"type": "string", "format": "date-time"},
            "favoritedSince": {"type": "string", "format": "date-time"},
            "favoritedUntil": {"type": "string", "format": "date-time"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    assert schemas["favhub.get_item"] == {
        "type": "object",
        "properties": {
            "platform": {"type": "string"},
            "sourceId": {"type": "string"},
            "includeContent": {"type": "boolean"},
        },
        "required": ["platform", "sourceId"],
        "additionalProperties": False,
    }
    assert schemas["favhub.status"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def test_tools_call_maps_requests_and_returns_text_and_structured_content() -> None:
    service = _RetrievalStub()
    responses, _ = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {
                    "name": "favhub.search",
                    "arguments": {
                        "query": "matching",
                        "platforms": ["x"],
                        "contentTypes": ["text"],
                        "publishedSince": "2026-01-01T00:00:00Z",
                        "publishedUntil": "2026-02-01T00:00:00+00:00",
                        "limit": 5,
                    },
                },
            ),
            _request(
                3,
                "tools/call",
                {
                    "name": "favhub.get_item",
                    "arguments": {"platform": "x", "sourceId": "42", "includeContent": False},
                },
            ),
            _request(4, "tools/call", {"name": "favhub.status", "arguments": {}}),
        ),
        service,
    )

    assert service.search_request == SearchRequest(
        query="matching",
        platforms=("x",),
        content_types=("text",),
        published_since="2026-01-01T00:00:00Z",
        published_until="2026-02-01T00:00:00+00:00",
        limit=5,
    )
    assert service.item_request == GetItemRequest("x", "42", include_content=False)
    for response in responses[1:]:
        result = response["result"]
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"]
        assert isinstance(result["structuredContent"], dict)
        assert "isError" not in result
    assert responses[1]["result"]["structuredContent"]["hits"][0]["source_id"] == "42"
    assert responses[2]["result"]["structuredContent"]["system_content"] == {}
    assert responses[3]["result"]["structuredContent"] == {
        "indexed_items": 1,
        "indexed_chunks": 2,
        "pending_index_tasks": 3,
        "failed_index_tasks": 4,
        "index_state": "available",
        "unavailable_items": 0,
    }


def test_the_collection_map_carries_the_folders_and_what_they_leave_out() -> None:
    responses, _ = _run(
        _initialized_messages(_request(2, "tools/call", {"name": "favhub.collections"})),
    )

    result = responses[1]["result"]
    assert result["structuredContent"] == {
        "collections": [{"platform": "bilibili", "name": "钢琴", "items": 8}],
        "platforms": [
            {"platform": "bilibili", "items": 9, "unfiled": 1},
            {"platform": "github", "items": 296, "unfiled": 296},
        ],
    }
    # An agent that reads only the folder list concludes this library holds
    # eight piano videos. The summary has to say out loud that 297 saved items
    # are not described by any folder name.
    assert "297 item(s) no folder describes" in result["content"][0]["text"]


@pytest.mark.parametrize(
    ("line", "code", "response_id"),
    [
        ("{not-json", -32700, None),
        (json.dumps([]), -32600, None),
        (json.dumps({"jsonrpc": "1.0", "id": 9, "method": "tools/list"}), -32600, 9),
        (json.dumps({"jsonrpc": "2.0", "id": True, "method": "tools/list"}), -32600, None),
        ('{"jsonrpc":"2.0","id":1e999,"method":"tools/list"}', -32600, None),
    ],
)
def test_malformed_json_and_invalid_envelopes(line: str, code: int, response_id: object) -> None:
    responses, _ = _run(line + "\n")

    assert responses[0]["error"]["code"] == code
    assert responses[0]["id"] == response_id


def test_unknown_method_is_method_not_found_and_notification_has_no_response() -> None:
    responses, _ = _run(
        _initialized_messages(
            _request(2, "unknown/method", {}),
            {"jsonrpc": "2.0", "method": "unknown/notification"},
        )
    )

    assert len(responses) == 2
    assert responses[1]["id"] == 2
    assert responses[1]["error"]["code"] == -32601


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"name": "favhub.unknown", "arguments": {}}, "unknown tool"),
        ({"name": "favhub.search", "arguments": {}}, "query"),
        ({"name": "favhub.search", "arguments": {"query": 3}}, "query"),
        ({"name": "favhub.search", "arguments": {"query": "q", "limit": True}}, "limit"),
        ({"name": "favhub.search", "arguments": {"query": "q", "path": "C:/secret"}}, "path"),
        ({"name": "favhub.search", "arguments": {"query": "q", "dataRoot": "root"}}, "dataRoot"),
        ({"name": "favhub.get_item", "arguments": {"platform": "x"}}, "sourceId"),
        (
            {
                "name": "favhub.get_item",
                "arguments": {"platform": "x", "sourceId": "42", "cookie": "secret"},
            },
            "cookie",
        ),
        ({"name": "favhub.status", "arguments": {"remoteUrl": "https://example.com"}}, "remoteUrl"),
        ({"name": "favhub.status", "arguments": []}, "arguments"),
    ],
)
def test_unknown_tool_and_invalid_or_unknown_arguments_are_invalid_params(
    params: dict[str, object], message: str
) -> None:
    responses, _ = _run(_initialized_messages(_request(2, "tools/call", params)))

    error = responses[1]["error"]
    assert error["code"] == -32602
    assert message in error["message"]


def test_protocol_metadata_on_a_call_is_ignored_rather_than_refused() -> None:
    """`_meta` is reserved by MCP and real clients attach it to every call.

    Every test in this file sent bare params, so refusing it passed the suite
    while failing against an actual client on the very first tool call.
    """
    responses, _ = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {
                    "name": "favhub.status",
                    "arguments": {},
                    "_meta": {"progressToken": "p-1"},
                },
            )
        )
    )
    assert "error" not in responses[1], responses[1].get("error")


def test_metadata_is_tolerated_at_the_protocol_level_only() -> None:
    """A stray `_meta` among a tool's own arguments is still a caller error."""
    responses, _ = _run(
        _initialized_messages(
            _request(2, "tools/call", {"name": "favhub.status", "arguments": {"_meta": {}}})
        )
    )
    assert responses[1]["error"]["code"] == -32602


@pytest.mark.parametrize(
    ("service", "rpc_request", "error_code", "private_detail", "public_message"),
    [
        (
            _RetrievalStub(),
            _request(
                2,
                "tools/call",
                {"name": "favhub.search", "arguments": {"query": "invalid"}},
            ),
            "invalid_argument",
            "query is invalid",
            "Invalid tool argument.",
        ),
        (
            _RetrievalStub(),
            _request(
                2,
                "tools/call",
                {"name": "favhub.get_item", "arguments": {"platform": "x", "sourceId": "missing"}},
            ),
            "not_found",
            "item not found: x/missing",
            "FavHub item was not found.",
        ),
        (
            _RetrievalStub(status_error=OSError(f"failed to read {PRIVATE_PATH}")),
            _request(2, "tools/call", {"name": "favhub.status", "arguments": {}}),
            "storage_error",
            PRIVATE_PATH,
            "FavHub storage is unavailable.",
        ),
        (
            _RetrievalStub(),
            _request(
                2,
                "tools/call",
                {"name": "favhub.search", "arguments": {"query": "offline"}},
            ),
            "index_unavailable",
            PRIVATE_PATH,
            "FavHub index is unavailable.",
        ),
    ],
)
def test_expected_service_errors_are_stable_tool_results(
    service: _RetrievalStub,
    rpc_request: dict[str, Any],
    error_code: str,
    private_detail: str,
    public_message: str,
) -> None:
    responses, stderr = _run(_initialized_messages(rpc_request), service)

    result = responses[1]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": error_code,
        "message": public_message,
    }
    assert result["content"] == [{"type": "text", "text": public_message}]
    assert private_detail not in json.dumps(result)
    assert private_detail in stderr
    assert "Traceback" not in stderr


def test_a_rejection_reaches_the_caller_in_its_own_words() -> None:
    """The sanitized constant is right for a bug and wrong for a rule.

    An Agent told only "Invalid tool argument." cannot tell a malformed field
    from a rule it broke, so it varies the fields and retries. One run spent
    fifteen attempts that way before concluding the tool itself was broken.
    Rules are raised as Rejection with authored text and are returned verbatim.
    """
    responses, stderr = _run(
        _initialized_messages(
            _request(2, "tools/call", {"name": "favhub.search", "arguments": {"query": "rejected"}})
        ),
        _RetrievalStub(),
    )

    result = responses[1]["result"]
    spoken = "queries must name a field the index carries"
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "invalid_argument",
        "message": spoken,
    }
    assert result["content"] == [{"type": "text", "text": spoken}]
    # Still logged, so a rejection reads the same way in stderr as anything else.
    assert spoken in stderr
    assert "Traceback" not in stderr


def test_source_snapshot_error_is_sanitized_as_storage_error() -> None:
    service = _RetrievalStub(
        status_error=SourceSnapshotError(Path(PRIVATE_PATH), "invalid private snapshot")
    )

    responses, stderr = _run(
        _initialized_messages(
            _request(2, "tools/call", {"name": "favhub.status", "arguments": {}})
        ),
        service,
    )

    result = responses[1]["result"]
    assert result["structuredContent"]["error"] == {
        "code": "storage_error",
        "message": "FavHub storage is unavailable.",
    }
    assert PRIVATE_PATH not in json.dumps(result)
    assert PRIVATE_PATH in stderr


def test_real_missing_fts_search_is_sanitized_as_index_unavailable(tmp_path: Path) -> None:
    with Application.open(tmp_path / "root") as application:
        assert application.retrieval is not None
        application.database.connection.execute("DROP TABLE content_chunks_fts")

        responses, stderr = _run(
            _initialized_messages(
                _request(
                    2,
                    "tools/call",
                    {"name": "favhub.search", "arguments": {"query": "anything"}},
                )
            ),
            application.retrieval,
        )

    result = responses[1]["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "index_unavailable",
        "message": "FavHub index is unavailable.",
    }
    assert result["content"] == [{"type": "text", "text": "FavHub index is unavailable."}]
    assert "no such table" not in json.dumps(result)
    assert "index_unavailable" in stderr


def test_unknown_service_error_is_sanitized_json_rpc_internal_error() -> None:
    service = _RetrievalStub(status_error=AssertionError(f"private failure at {PRIVATE_PATH}"))

    responses, stderr = _run(
        _initialized_messages(
            _request(2, "tools/call", {"name": "favhub.status", "arguments": {}})
        ),
        service,
    )

    assert responses[1]["error"] == {"code": -32603, "message": "internal error"}
    assert PRIVATE_PATH not in json.dumps(responses[1])
    assert PRIVATE_PATH in stderr


def test_stdout_contains_only_one_json_response_per_request_id() -> None:
    payload = (
        _initialized_messages(
            _request(2, "tools/list", {}),
            _request(3, "tools/call", {"name": "favhub.status", "arguments": {}}),
        )
        + "{bad-json\n"
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    run_stdio(_RetrievalStub(), io.StringIO(payload), stdout, stderr)

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 4
    assert [json.loads(line)["id"] for line in lines] == [1, 2, 3, None]
    assert all(line.startswith("{") and line.endswith("}") for line in lines)
    assert "Traceback" not in stdout.getvalue()


class _ApplicationStub:
    def __enter__(self) -> "_ApplicationStub":
        self.retrieval = _RetrievalStub()
        self.sync = None
        self.database = None
        self.queue = None
        self.library = None
        self.store = None
        self.browser_gateway = None
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        return None


def test_main_returns_zero_on_clean_eof_and_nonzero_on_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Application, "open", classmethod(lambda _cls, _root: _ApplicationStub()))
    stderr = io.StringIO()
    assert main(["--root", "root"], stdin=io.StringIO(""), stdout=io.StringIO(), stderr=stderr) == 0
    assert stderr.getvalue() == ""

    def fail_open(_cls: type[Application], _root: Path) -> Application:
        raise OSError("unable to open root")

    monkeypatch.setattr(Application, "open", classmethod(fail_open))
    stderr = io.StringIO()
    assert main(["--root", "root"], stdin=io.StringIO(""), stdout=io.StringIO(), stderr=stderr) != 0
    assert "unable to open root" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_main_serves_a_degraded_session_when_the_data_root_is_busy(tmp_path: Path) -> None:
    root = tmp_path / "root"
    payload = _initialized_messages(
        _request(2, "tools/list"),
        _request(3, "tools/call", {"name": "favhub.status", "arguments": {}}),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    holder = Application.open(root)
    try:
        exit_code = main(
            ["--root", str(root)], stdin=io.StringIO(payload), stdout=stdout, stderr=stderr
        )
    finally:
        holder.close()

    # A busy root is not a crash: the client keeps a usable session, so the
    # reason reaches the Agent instead of a server that simply vanished.
    assert exit_code == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    listed = {tool["name"] for tool in responses[1]["result"]["tools"]}
    # Nothing that needs the root is advertised; a session that cannot collect
    # must not claim it can.
    assert listed == {
        "favhub.search",
        "favhub.get_item",
        "favhub.collections",
        "favhub.status",
    }
    status = responses[2]["result"]
    assert status["isError"] is True
    assert status["structuredContent"]["error"]["code"] == "data_root_busy"
    assert "already using this data root" in status["content"][0]["text"]
    assert str(root) not in json.dumps(responses)
    assert "already in use" in stderr.getvalue()


def test_subprocess_serves_real_temporary_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    assert cli_main(["--root", str(root), "import-fixture", str(FIXTURE), "--mode", "full"]) == 0
    capsys.readouterr()
    with Application.open(root) as application:
        assert application.indexer is not None
        assert application.indexer.index_next() is not None

    requests = _initialized_messages(
        _request(
            2,
            "tools/call",
            {"name": "favhub.search", "arguments": {"query": "fixture"}},
        )
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).parents[1] / "src"), environment.get("PYTHONPATH")])
    )
    completed = subprocess.run(
        [sys.executable, "-m", "favhub.mcp_server", "--root", str(root)],
        input=requests,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2]
    result = responses[1]["result"]
    assert "isError" not in result
    assert result["structuredContent"]["found"] is True
    assert result["structuredContent"]["hits"]


@pytest.fixture
def sync_gateway(tmp_path: Path) -> Iterator[SyncGateway]:
    database = Database.open(tmp_path / "sync.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    gateway = SyncGateway(SyncModule(database, library))
    try:
        yield gateway
    finally:
        database.close()


def _run_sync(payload: str, gateway: SyncGateway) -> tuple[list[dict[str, Any]], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    run_stdio(_RetrievalStub(), io.StringIO(payload), stdout, stderr, sync=gateway)
    lines = stdout.getvalue().splitlines()
    return [json.loads(line) for line in lines], stderr.getvalue()


def _sync_call(gateway: SyncGateway, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    responses, _ = _run_sync(
        _initialized_messages(_request(2, "tools/call", {"name": name, "arguments": arguments})),
        gateway,
    )
    return responses[1]


TRANSCRIPT_TEXT = "# Transcript\n\n[00:00] 大家好\n"


def _sync_item(source_id: str = "BV1aa411c7mD", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sourceId": source_id,
        "canonicalUrl": f"https://www.bilibili.com/video/{source_id}",
        "title": "本地知识库设计漫谈",
        "author": "示例UP主",
        "publishedAt": "2026-01-02T00:00:00Z",
        "observedAt": "2026-07-26T00:00:00Z",
        "body": "简介",
        "collections": ["技术分享"],
        "extractorVersion": "bilibili-browser-v1",
        "assets": [
            {
                "relativePath": "transcript/0001.md",
                "mediaType": "text/markdown",
                "text": TRANSCRIPT_TEXT,
                "sha256": sha256_text(TRANSCRIPT_TEXT),
            }
        ],
    }
    payload.update(overrides)
    return payload


def _sync_start(gateway: SyncGateway) -> str:
    response = _sync_call(
        gateway,
        "favhub.sync_start",
        {
            "platform": "bilibili",
            "mode": "incremental",
            "scopes": [
                {"scopeId": "100001", "scopeName": "默认收藏夹"},
                {"scopeId": "100002", "scopeName": "技术分享"},
            ],
        },
    )
    payload = response["result"]["structuredContent"]
    assert payload["scoped_frontiers"] == {"100001": [], "100002": []}
    return str(payload["job_id"])


def test_tools_list_includes_sync_tools_with_closed_schemas(
    sync_gateway: SyncGateway,
) -> None:
    responses, _ = _run_sync(_initialized_messages(_request(2, "tools/list", {})), sync_gateway)

    tools = responses[1]["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "favhub.search",
        "favhub.get_item",
        "favhub.collections",
        "favhub.status",
        "favhub.sync_start",
        "favhub.sync_submit_batch",
        "favhub.sync_pause",
        "favhub.sync_finish",
        "favhub.sync_status",
    ]
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["favhub.sync_start"]["properties"]["platform"]["enum"] == [
        "bilibili",
        "github",
        "x",
        "zhihu",
    ]
    assert schemas["favhub.sync_submit_batch"]["properties"]["platform"]["enum"] == [
        "bilibili",
        "github",
        "x",
        "zhihu",
    ]
    # mode is optional and defaults to incremental, matching favhub.github_sync.
    assert schemas["favhub.sync_start"]["required"] == ["platform"]
    assert schemas["favhub.sync_submit_batch"]["required"] == [
        "jobId",
        "platform",
        "batchId",
        "items",
    ]
    assert schemas["favhub.sync_pause"]["required"] == ["jobId", "platform", "code", "message"]
    assert schemas["favhub.sync_finish"]["required"] == [
        "jobId",
        "platform",
        "observedEnd",
        "maxScanReached",
    ]
    assert schemas["favhub.sync_status"]["required"] == ["jobId"]


def test_every_tool_taking_a_mode_treats_it_the_same_way() -> None:
    """One argument, one contract, across all three entry points.

    `favhub.github_sync` defaulted to incremental while the two start tools
    required the argument, so the same word meant "optional" or "mandatory"
    depending on which tool an Agent reached for. Asserting the three together
    is what keeps a fourth from inventing a third answer.
    """
    declared = (
        mcp_module._TOOLS
        + mcp_module._SYNC_TOOLS
        + mcp_module._BROWSER_TOOLS
        + mcp_module._GITHUB_TOOLS
        + mcp_module._ENRICH_TOOLS
    )
    schemas = {tool["name"]: tool["inputSchema"] for tool in declared}
    with_mode = sorted(name for name, schema in schemas.items() if "mode" in schema["properties"])
    assert with_mode == [
        "favhub.browser_start",
        "favhub.github_sync",
        "favhub.sync_start",
    ]
    for name in with_mode:
        schema = schemas[name]
        assert "mode" not in schema["required"], name
        assert schema["properties"]["mode"]["enum"] == ["full", "incremental"], name
        assert "Defaults to incremental" in schema["properties"]["mode"]["description"], name


def test_sync_tools_are_unknown_without_gateway() -> None:
    responses, _ = _run(
        _initialized_messages(
            _request(2, "tools/call", {"name": "favhub.sync_status", "arguments": {"jobId": "j"}}),
            _request(3, "tools/list", {}),
        )
    )

    assert responses[1]["error"]["code"] == -32602
    assert "unknown tool" in responses[1]["error"]["message"]
    names = [tool["name"] for tool in responses[2]["result"]["tools"]]
    assert names == [
        "favhub.search",
        "favhub.get_item",
        "favhub.collections",
        "favhub.status",
    ]


def test_sync_lifecycle_start_submit_replay_pause_finish_status(
    sync_gateway: SyncGateway,
) -> None:
    job_id = _sync_start(sync_gateway)

    submit_arguments = {
        "jobId": job_id,
        "platform": "bilibili",
        "batchId": "batch-1",
        "items": [_sync_item()],
        "scopeScans": {"100002": ["BV1aa411c7mD"]},
    }
    first = _sync_call(sync_gateway, "favhub.sync_submit_batch", submit_arguments)
    receipt = first["result"]["structuredContent"]
    assert receipt["added"] == 1

    replay = _sync_call(sync_gateway, "favhub.sync_submit_batch", submit_arguments)
    assert replay["result"]["structuredContent"] == receipt

    paused = _sync_call(
        sync_gateway,
        "favhub.sync_pause",
        {
            "jobId": job_id,
            "platform": "bilibili",
            "code": "rate_limited",
            "message": "请稍后再试",
        },
    )
    assert paused["result"]["structuredContent"]["status"] == "paused"
    assert paused["result"]["structuredContent"]["error"]["code"] == "rate_limited"

    status = _sync_call(sync_gateway, "favhub.sync_status", {"jobId": job_id})
    status_payload = status["result"]["structuredContent"]
    assert status_payload["capture_status"] == "paused"
    scanned = {
        scope["scope_id"]: scope["counts"]["scanned"]
        for scope in status_payload["platforms"][0]["scopes"]
    }
    assert scanned == {"100001": 0, "100002": 1}

    finished = _sync_call(
        sync_gateway,
        "favhub.sync_finish",
        {
            "jobId": job_id,
            "platform": "bilibili",
            "observedEnd": False,
            "maxScanReached": False,
            "frontierScopes": {"100002": ["BV1aa411c7mD"]},
        },
    )
    scope_statuses = {
        scope["scope_id"]: scope["status"]
        for scope in finished["result"]["structuredContent"]["platform"]["scopes"]
    }
    assert scope_statuses == {"100001": "partial", "100002": "completed"}


def test_sync_submit_rejects_unsafe_payloads(sync_gateway: SyncGateway) -> None:
    job_id = _sync_start(sync_gateway)

    def submit(items: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "jobId": job_id,
            "platform": "bilibili",
            "batchId": "reject-batch",
            "items": items,
        }
        arguments.update(extra)
        return _sync_call(sync_gateway, "favhub.sync_submit_batch", arguments)

    absolute = submit(
        [
            _sync_item(
                assets=[
                    {
                        "relativePath": "/etc/passwd",
                        "mediaType": "text/plain",
                        "text": "x",
                        "sha256": sha256_text("x"),
                    }
                ]
            )
        ]
    )
    assert absolute["result"]["structuredContent"]["error"]["code"] == "invalid_argument"

    binary = submit(
        [
            _sync_item(
                assets=[
                    {
                        "relativePath": "assets/cover.bin",
                        "mediaType": "application/octet-stream",
                        "text": "x",
                        "sha256": sha256_text("x"),
                    }
                ]
            )
        ]
    )
    assert binary["result"]["structuredContent"]["error"]["code"] == "invalid_argument"

    oversized_text = "a" * (2 * 1024 * 1024 + 1)
    oversized = submit(
        [
            _sync_item(
                assets=[
                    {
                        "relativePath": "assets/subtitles/big.json",
                        "mediaType": "application/json",
                        "text": oversized_text,
                        "sha256": sha256_text(oversized_text),
                    }
                ]
            )
        ]
    )
    assert oversized["result"]["structuredContent"]["error"]["code"] == "invalid_argument"

    duplicates = submit([_sync_item(), _sync_item()])
    assert duplicates["result"]["structuredContent"]["error"]["code"] == "invalid_argument"

    smuggled = submit([_sync_item(cookie="secret")])
    assert smuggled["result"]["structuredContent"]["error"]["code"] == "invalid_argument"
    assert "secret" not in json.dumps(smuggled)

    unknown_scope = submit([_sync_item()], scopeScans={"999999": ["BV1aa411c7mD"]})
    assert unknown_scope["result"]["structuredContent"]["error"]["code"] == "not_found"


def test_sync_submit_missing_job_id_is_invalid_params(sync_gateway: SyncGateway) -> None:
    response = _sync_call(
        sync_gateway,
        "favhub.sync_submit_batch",
        {"platform": "bilibili", "batchId": "b", "items": []},
    )
    assert response["error"]["code"] == -32602
    assert "jobId" in response["error"]["message"]


def test_sync_start_rejects_platform_and_naive_dates(sync_gateway: SyncGateway) -> None:
    wrong_platform = _sync_call(
        sync_gateway, "favhub.sync_start", {"platform": "gitlab", "mode": "full"}
    )
    assert wrong_platform["error"]["code"] == -32602

    naive = _sync_call(
        sync_gateway,
        "favhub.sync_start",
        {"platform": "bilibili", "mode": "full", "publishedSince": "2026-01-01T00:00:00"},
    )
    assert naive["error"]["code"] == -32602


def test_sync_pause_rejects_unknown_code(sync_gateway: SyncGateway) -> None:
    job_id = _sync_start(sync_gateway)
    response = _sync_call(
        sync_gateway,
        "favhub.sync_pause",
        {"jobId": job_id, "platform": "bilibili", "code": "made_up", "message": "x"},
    )
    assert response["error"]["code"] == -32602


def test_sync_status_unknown_job_is_not_found(sync_gateway: SyncGateway) -> None:
    response = _sync_call(sync_gateway, "favhub.sync_status", {"jobId": "no-such-job"})
    result = response["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "not_found"


def test_sync_only_session_serves_sync_and_reports_index_unavailable(
    sync_gateway: SyncGateway,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    payload = _initialized_messages(
        _request(2, "tools/call", {"name": "favhub.search", "arguments": {"query": "q"}}),
        _request(
            3,
            "tools/call",
            {"name": "favhub.sync_start", "arguments": {"platform": "bilibili", "mode": "full"}},
        ),
    )

    run_stdio(_UnavailableRetrieval(), io.StringIO(payload), stdout, stderr, sync=sync_gateway)

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    search_error = responses[1]["result"]["structuredContent"]["error"]
    assert search_error["code"] == "index_unavailable"
    started = responses[2]["result"]["structuredContent"]
    assert started["job_id"]
    assert started["scoped_frontiers"] == {}


@pytest.fixture
def enrich_gateway(tmp_path: Path) -> Iterator[tuple[EnrichGateway, LibraryModule, Database]]:
    database = Database.open(tmp_path / "enrich.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    library = LibraryModule(database, store, queue)
    gateway = EnrichGateway(database, queue, library, store)
    try:
        yield gateway, library, database
    finally:
        database.close()


def _run_enrich(payload: str, gateway: EnrichGateway) -> tuple[list[dict[str, Any]], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    run_stdio(_RetrievalStub(), io.StringIO(payload), stdout, stderr, enrich=gateway)
    lines = stdout.getvalue().splitlines()
    return [json.loads(line) for line in lines], stderr.getvalue()


def _enrich_call(gateway: EnrichGateway, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    responses, _ = _run_enrich(
        _initialized_messages(_request(2, "tools/call", {"name": name, "arguments": arguments})),
        gateway,
    )
    return responses[1]


def _seed_enrich_item(library: LibraryModule, database: Database) -> None:
    timestamp = "2026-07-26T00:00:00Z"
    database.connection.execute(
        """INSERT INTO sync_jobs(id, mode, status, options_json, created_at, updated_at)
           VALUES ('enrich-job', 'full', 'running', '{}', ?, ?)""",
        (timestamp, timestamp),
    )
    from datetime import UTC, datetime

    from favhub.domain import CapturedItem

    item = CapturedItem(
        platform="x",
        source_id="77",
        canonical_url="https://x.com/example/status/77",
        title="Enrich me",
        author="example",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="body about retrieval pipelines",
        collections=(),
        extractor_version="fixture-v1",
    )
    library.ingest_batch("enrich-job", "x", "b1", [item], True)


def test_tools_list_includes_enrich_tools_with_closed_schemas(
    enrich_gateway: tuple[EnrichGateway, LibraryModule, Database],
) -> None:
    gateway, _, _ = enrich_gateway
    responses, _ = _run_enrich(_initialized_messages(_request(2, "tools/list", {})), gateway)
    tools = responses[1]["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "favhub.search",
        "favhub.get_item",
        "favhub.collections",
        "favhub.status",
        "favhub.enrich_next",
        "favhub.enrich_submit",
        "favhub.enrich_skip",
    ]
    schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["favhub.enrich_next"] == {
        "type": "object",
        "properties": {
            "platform": {"type": "string", "enum": ["bilibili", "github", "x", "zhihu"]},
        },
        "additionalProperties": False,
    }
    assert schemas["favhub.enrich_submit"]["required"] == [
        "taskId",
        "summary",
        "tags",
        "contentType",
        "provider",
        "model",
    ]


def test_enrich_round_trip_over_json_rpc(
    enrich_gateway: tuple[EnrichGateway, LibraryModule, Database],
) -> None:
    gateway, library, database = enrich_gateway
    _seed_enrich_item(library, database)

    claimed = _enrich_call(gateway, "favhub.enrich_next", {})
    task = claimed["result"]["structuredContent"]["task"]
    assert task["source_id"] == "77"
    assert any("retrieval pipelines" in entry["text"] for entry in task["content"])

    submitted = _enrich_call(
        gateway,
        "favhub.enrich_submit",
        {
            "taskId": task["task_id"],
            "summary": "介绍 retrieval pipeline 的收藏内容。",
            "tags": ["retrieval"],
            "contentType": "text",
            "provider": "agent",
            "model": "claude-fable-5",
        },
    )
    assert submitted["result"]["structuredContent"] == {
        "outcome": "applied",
        "task_id": task["task_id"],
    }

    empty = _enrich_call(gateway, "favhub.enrich_next", {})
    assert empty["result"]["structuredContent"] == {"task": None}


def test_enrich_submit_argument_errors_are_invalid_params(
    enrich_gateway: tuple[EnrichGateway, LibraryModule, Database],
) -> None:
    gateway, library, database = enrich_gateway
    _seed_enrich_item(library, database)
    task = _enrich_call(gateway, "favhub.enrich_next", {})["result"]["structuredContent"]["task"]

    bad = _enrich_call(
        gateway,
        "favhub.enrich_submit",
        {
            "taskId": task["task_id"],
            "summary": "s",
            "tags": [],
            "contentType": "text",
            "provider": "agent",
            "model": "m",
        },
    )
    assert bad["error"]["code"] == -32602

    unknown = _enrich_call(gateway, "favhub.enrich_next", {"cookie": "x"})
    assert unknown["error"]["code"] == -32602

    unsupported = _enrich_call(gateway, "favhub.enrich_next", {"platform": "myspace"})
    assert unsupported["error"]["code"] == -32602


def test_enrich_tools_absent_without_gateway() -> None:
    responses, _ = _run(
        _initialized_messages(
            _request(2, "tools/call", {"name": "favhub.enrich_next", "arguments": {}})
        )
    )
    assert responses[1]["error"]["code"] == -32602
    assert "unknown tool" in responses[1]["error"]["message"]


def test_search_accepts_favorited_window_and_validates_order() -> None:
    service = _RetrievalStub()
    responses, _ = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {
                    "name": "favhub.search",
                    "arguments": {
                        "query": "matching",
                        "favoritedSince": "2026-06-01T00:00:00Z",
                        "favoritedUntil": "2026-07-01T00:00:00Z",
                    },
                },
            ),
            _request(
                3,
                "tools/call",
                {
                    "name": "favhub.search",
                    "arguments": {
                        "query": "matching",
                        "favoritedSince": "2026-07-01T00:00:00Z",
                        "favoritedUntil": "2026-06-01T00:00:00Z",
                    },
                },
            ),
            _request(
                4,
                "tools/call",
                {
                    "name": "favhub.search",
                    "arguments": {"query": "matching", "favoritedSince": "2026-06-01T00:00:00"},
                },
            ),
        ),
        service,
    )

    assert "result" in responses[1]
    assert service.search_request is not None
    assert service.search_request.favorited_since == "2026-06-01T00:00:00Z"
    assert service.search_request.favorited_until == "2026-07-01T00:00:00Z"
    assert responses[2]["error"]["code"] == -32602
    assert responses[3]["error"]["code"] == -32602


# -- Task 3: browser tools ---------------------------------------------------


def _browser_stack(tmp_path: Path) -> tuple[BrowserGateway, SyncGateway, Database]:
    from favhub.browser_capture import BrowserCaptureStore
    from favhub.enrichment_queue import EnrichmentQueue
    from favhub.item_store import ItemStore
    from favhub.library import LibraryModule
    from favhub.sync_module import SyncModule

    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    sync = SyncModule(database, library)
    sync_gateway = SyncGateway(sync)
    browser = BrowserGateway(sync_gateway, sync, BrowserCaptureStore(database))
    return browser, sync_gateway, database


def _run_browser(
    payload: str, browser: BrowserGateway, sync: SyncGateway
) -> tuple[list[dict[str, Any]], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    run_stdio(
        _RetrievalStub(),
        io.StringIO(payload),
        stdout,
        stderr,
        sync=sync,
        browser=browser,
    )
    return [json.loads(line) for line in stdout.getvalue().splitlines()], stderr.getvalue()


def test_browser_tools_are_absent_without_a_browser_gateway() -> None:
    responses, _ = _run(_initialized_messages(_request(2, "tools/list", {})))
    names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert not {name for name in names if name.startswith("favhub.browser_")}


def test_browser_tools_advertise_exact_closed_schemas(tmp_path: Path) -> None:
    browser, sync, database = _browser_stack(tmp_path)
    try:
        responses, _ = _run_browser(
            _initialized_messages(_request(2, "tools/list", {})), browser, sync
        )
        tools = responses[1]["result"]["tools"]
        names = [tool["name"] for tool in tools]
        # Existing tools keep their exact order and identity.
        assert names[:9] == [
            "favhub.search",
            "favhub.get_item",
            "favhub.collections",
            "favhub.status",
            "favhub.sync_start",
            "favhub.sync_submit_batch",
            "favhub.sync_pause",
            "favhub.sync_finish",
            "favhub.sync_status",
        ]
        assert names[9:] == [
            "favhub.browser_start",
            "favhub.browser_resume",
            "favhub.browser_status",
            "favhub.browser_cancel",
        ]
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        assert all(schema["additionalProperties"] is False for schema in schemas.values())
        assert schemas["favhub.browser_start"]["required"] == ["platform"]
        assert schemas["favhub.browser_start"]["properties"]["platform"]["enum"] == [
            "bilibili",
            "x",
            "zhihu",
        ]
        assert "scopes" not in schemas["favhub.browser_start"]["properties"]
        assert schemas["favhub.browser_resume"]["required"] == ["jobId", "platform"]
        assert schemas["favhub.browser_status"]["required"] == ["jobId"]
        assert schemas["favhub.browser_cancel"]["required"] == ["jobId", "platform"]
    finally:
        database.close()


def test_browser_start_creates_a_session_and_status_reports_it(tmp_path: Path) -> None:
    browser, sync, database = _browser_stack(tmp_path)
    try:
        responses, _ = _run_browser(
            _initialized_messages(
                _request(
                    2,
                    "tools/call",
                    {
                        "name": "favhub.browser_start",
                        "arguments": {"platform": "zhihu", "mode": "incremental"},
                    },
                )
            ),
            browser,
            sync,
        )
        started = responses[1]["result"]["structuredContent"]
        assert started["browser_session"]["status"] == "awaiting_browser"
        job_id = started["job_id"]

        responses, _ = _run_browser(
            _initialized_messages(
                _request(
                    2,
                    "tools/call",
                    {"name": "favhub.browser_status", "arguments": {"jobId": job_id}},
                )
            ),
            browser,
            sync,
        )
        status = responses[1]["result"]["structuredContent"]
        assert status["browser_sessions"][0]["platform"] == "zhihu"
    finally:
        database.close()


def test_browser_start_rejects_github_and_unknown_arguments(tmp_path: Path) -> None:
    browser, sync, database = _browser_stack(tmp_path)
    try:
        responses, _ = _run_browser(
            _initialized_messages(
                _request(
                    2,
                    "tools/call",
                    {
                        "name": "favhub.browser_start",
                        "arguments": {"platform": "github", "mode": "full"},
                    },
                ),
                _request(
                    3,
                    "tools/call",
                    {
                        "name": "favhub.browser_start",
                        "arguments": {
                            "platform": "x",
                            "mode": "full",
                            "scopes": [{"scopeId": "1", "scopeName": "nope"}],
                        },
                    },
                ),
            ),
            browser,
            sync,
        )
        assert responses[1]["error"]["code"] == -32602
        assert responses[2]["error"]["code"] == -32602
    finally:
        database.close()


class _SyncModuleStub:
    """Only needs an identity: the tool forwards, the collector is tested apart."""


def test_the_github_tool_is_offered_only_when_a_sync_module_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run_sync(_sync: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "job_id": "j-1",
            "authenticated": kwargs["token"] is not None,
            "readmes_missing": 0,
            "status": {"platforms": [{"counts": {"added": 3, "duplicates": 1}}]},
        }

    monkeypatch.setattr(mcp_module, "run_sync", fake_run_sync)
    monkeypatch.setenv(mcp_module.GITHUB_TOKEN_ENV, "ghp_pretend")

    payload = _initialized_messages(
        _request(2, "tools/list"),
        _request(
            3,
            "tools/call",
            {"name": "favhub.github_sync", "arguments": {"user": "someone", "mode": "full"}},
        ),
    )
    stdout = io.StringIO()
    run_stdio(
        _RetrievalStub(),
        io.StringIO(payload),
        stdout,
        io.StringIO(),
        sync_module=_SyncModuleStub(),
    )

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert "favhub.github_sync" in {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert calls == [
        {"login": "someone", "mode": "full", "max_scan_items": None, "token": "ghp_pretend"}
    ]
    # The token is read by this process and reported only as a fact about
    # itself; the value must never travel back to the caller.
    assert "ghp_pretend" not in stdout.getvalue()
    assert responses[2]["result"]["structuredContent"]["authenticated"] is True


def test_the_github_tool_is_absent_without_a_sync_module() -> None:
    responses, _stderr = _run(_initialized_messages(_request(2, "tools/list")))
    assert "favhub.github_sync" not in {tool["name"] for tool in responses[1]["result"]["tools"]}


def test_the_github_tool_rejects_an_unusable_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_module, "run_sync", lambda *_a, **_k: {})
    payload = _initialized_messages(
        _request(
            2,
            "tools/call",
            {"name": "favhub.github_sync", "arguments": {"user": "someone", "mode": "sideways"}},
        )
    )
    stdout = io.StringIO()
    run_stdio(
        _RetrievalStub(),
        io.StringIO(payload),
        stdout,
        io.StringIO(),
        sync_module=_SyncModuleStub(),
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[1]["error"]["code"] == -32602
