"""Newline-delimited JSON-RPC adapter for FavHub retrieval and sync tools."""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Protocol, TextIO, cast

from favhub.application import Application
from favhub.application_dispatcher import ApplicationDispatcher
from favhub.browser_gateway import BROWSER_PLATFORMS, BrowserGateway
from favhub.browser_pipe import BrowserPipeServer
from favhub.browser_protocol import BrowserProtocolError, decode_message
from favhub.domain import ENRICHMENT_CONTENT_TYPES, SUPPORTED_PLATFORMS
from favhub.enrich_gateway import SKIP_CODES, EnrichGateway
from favhub.github_sync import TOKEN_ENV as GITHUB_TOKEN_ENV
from favhub.github_sync import run_sync
from favhub.item_store import SourceSnapshotError
from favhub.retrieval import (
    CollectionMap,
    GetItemRequest,
    ItemResponse,
    RetrievalStatus,
    SearchRequest,
    SearchResponse,
)
from favhub.root_lock import DataRootBusyError
from favhub.sync_gateway import PAUSE_CODES, Rejection, SyncArgumentError, SyncGateway
from favhub.sync_module import SyncModule

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "favhub"
SERVER_VERSION = "0.1.0"

type RequestId = str | int | float | None

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_SEARCH_ARGUMENTS = frozenset(
    {
        "query",
        "platforms",
        "contentTypes",
        "collections",
        "publishedSince",
        "publishedUntil",
        "favoritedSince",
        "favoritedUntil",
        "limit",
    }
)
_GET_ITEM_ARGUMENTS = frozenset({"platform", "sourceId", "includeContent"})
_SYNC_TOOL_ARGUMENTS = {
    "favhub.sync_start": frozenset(
        {"platform", "mode", "publishedSince", "publishedUntil", "maxScanItems", "scopes"}
    ),
    "favhub.sync_submit_batch": frozenset({"jobId", "platform", "batchId", "items", "scopeScans"}),
    "favhub.sync_pause": frozenset({"jobId", "platform", "code", "message"}),
    "favhub.sync_finish": frozenset(
        {
            "jobId",
            "platform",
            "observedEnd",
            "maxScanReached",
            "visibleTotal",
            "frontierIds",
            "frontierScopes",
            "scopeResults",
        }
    ),
    "favhub.sync_status": frozenset({"jobId"}),
}
_BROWSER_TOOL_ARGUMENTS = {
    "favhub.browser_start": frozenset(
        {"platform", "mode", "publishedSince", "publishedUntil", "maxScanItems"}
    ),
    "favhub.browser_resume": frozenset({"jobId", "platform"}),
    "favhub.browser_status": frozenset({"jobId"}),
    "favhub.browser_cancel": frozenset({"jobId", "platform"}),
}
_GITHUB_TOOL_ARGUMENTS = {
    "favhub.github_sync": frozenset({"user", "mode", "maxScanItems"}),
}
_ENRICH_TOOL_ARGUMENTS = {
    "favhub.enrich_next": frozenset({"platform"}),
    "favhub.enrich_submit": frozenset(
        {"taskId", "summary", "tags", "contentType", "provider", "model"}
    ),
    "favhub.enrich_skip": frozenset({"taskId", "code", "message"}),
}
_TOOL_ERROR_MESSAGES = {
    "data_root_busy": (
        "Another FavHub is already using this data root. Close the other FavHub "
        "window or wait for it to finish, then reconnect this one."
    ),
    "invalid_argument": "Invalid tool argument.",
    "not_found": "FavHub item was not found.",
    "storage_error": "FavHub storage is unavailable.",
    "index_unavailable": "FavHub index is unavailable.",
}

# One wording for every tool that takes a mode, so the three cannot drift into
# describing the same argument differently.
_MODE_DESCRIPTION = (
    "Defaults to incremental. Use full only to refresh items already stored, or "
    "to reach history behind the frontier; a first run does not need it, because "
    "with no frontier yet incremental already scans to the end."
)

_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "favhub.search",
        "description": "Search the local FavHub content index.",
        "inputSchema": {
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
        },
    },
    {
        "name": "favhub.get_item",
        "description": "Read one locally stored FavHub item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string"},
                "sourceId": {"type": "string"},
                "includeContent": {"type": "boolean"},
            },
            "required": ["platform", "sourceId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.collections",
        "description": (
            "Map the library: the user's own folders with live item counts, "
            "largest first, and per-platform counts beside them. A named "
            "folder was a deliberate choice and marks a topic they actually "
            "care about; a platform's default folder collects one-click "
            "saves. A platform whose items are all 'unfiled' has no folders "
            "at all, so no folder name describes what it holds. Read this to "
            "decide whether their library is worth consulting for a topic, "
            "rather than searching to find out."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.status",
        "description": "Report the local FavHub retrieval index status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
)

_SYNC_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "favhub.sync_start",
        "description": ("Start a FavHub sync job; bilibili and zhihu accept per-folder scopes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["bilibili", "github", "x", "zhihu"]},
                "mode": {
                    "type": "string",
                    "enum": ["full", "incremental"],
                    "description": _MODE_DESCRIPTION,
                },
                "publishedSince": {"type": "string", "format": "date-time"},
                "publishedUntil": {"type": "string", "format": "date-time"},
                "maxScanItems": {"type": "integer", "minimum": 1},
                "scopes": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "scopeId": {"type": "string"},
                            "scopeName": {"type": "string"},
                        },
                        "required": ["scopeId"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["platform"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.sync_submit_batch",
        "description": "Submit one idempotent batch of normalized captured items.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string"},
                "platform": {"type": "string", "enum": ["bilibili", "github", "x", "zhihu"]},
                "batchId": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"}, "maxItems": 50},
                "scopeScans": {"type": "object"},
            },
            "required": ["jobId", "platform", "batchId", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.sync_pause",
        "description": "Pause a FavHub sync platform with a stable error code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string"},
                "platform": {"type": "string", "enum": ["bilibili", "github", "x", "zhihu"]},
                "code": {"type": "string", "enum": sorted(PAUSE_CODES)},
                "message": {"type": "string"},
            },
            "required": ["jobId", "platform", "code", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.sync_finish",
        "description": "Record scan completion and advance confirmed frontiers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string"},
                "platform": {"type": "string", "enum": ["bilibili", "github", "x", "zhihu"]},
                "observedEnd": {"type": "boolean"},
                "maxScanReached": {"type": "boolean"},
                "visibleTotal": {"type": ["integer", "null"], "minimum": 0},
                "frontierIds": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
                "frontierScopes": {"type": "object"},
                "scopeResults": {"type": "object"},
            },
            "required": ["jobId", "platform", "observedEnd", "maxScanReached"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.sync_status",
        "description": "Report FavHub sync job status including per-folder scopes.",
        "inputSchema": {
            "type": "object",
            "properties": {"jobId": {"type": "string"}},
            "required": ["jobId"],
            "additionalProperties": False,
        },
    },
)

_BROWSER_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "favhub.browser_start",
        "description": "Start a browser-driven FavHub collection run for one platform.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": sorted(BROWSER_PLATFORMS)},
                "mode": {
                    "type": "string",
                    "enum": ["full", "incremental"],
                    "description": _MODE_DESCRIPTION,
                },
                "publishedSince": {"type": "string"},
                "publishedUntil": {"type": "string"},
                "maxScanItems": {"type": "integer", "minimum": 1},
            },
            "required": ["platform"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.browser_resume",
        "description": "Resume a paused browser collection run without rescanning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string"},
                "platform": {"type": "string", "enum": sorted(BROWSER_PLATFORMS)},
            },
            "required": ["jobId", "platform"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.browser_status",
        "description": "Report a browser collection job with its capture sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {"jobId": {"type": "string"}},
            "required": ["jobId"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.browser_cancel",
        "description": "Cancel a browser collection run without advancing any frontier.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "jobId": {"type": "string"},
                "platform": {"type": "string", "enum": sorted(BROWSER_PLATFORMS)},
            },
            "required": ["jobId", "platform"],
            "additionalProperties": False,
        },
    },
)

_GITHUB_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "favhub.github_sync",
        "description": (
            "Collect the user's public GitHub stars. No browser and no Agent-side "
            "requests; FavHub reads an optional token from its own environment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["full", "incremental"],
                    "description": _MODE_DESCRIPTION,
                },
                "maxScanItems": {"type": "integer", "minimum": 1},
            },
            "required": ["user"],
            "additionalProperties": False,
        },
    },
)

_ENRICH_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "favhub.enrich_next",
        "description": (
            "Claim the next pending summarize task with its readable content. "
            "Optionally scope the claim to one platform, so a run can spend its "
            "budget where content is short before committing to where it is long."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": sorted(SUPPORTED_PLATFORMS)},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.enrich_submit",
        "description": "Persist an Agent-generated summary, tags, and content type.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "taskId": {"type": "string"},
                "summary": {"type": "string", "maxLength": 2000},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 40},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "contentType": {"type": "string", "enum": sorted(ENRICHMENT_CONTENT_TYPES)},
                "provider": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["taskId", "summary", "tags", "contentType", "provider", "model"],
            "additionalProperties": False,
        },
    },
    {
        "name": "favhub.enrich_skip",
        "description": "Record a failed enrichment attempt; the task stays retryable.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "taskId": {"type": "string"},
                "code": {"type": "string", "enum": sorted(SKIP_CODES)},
                "message": {"type": "string"},
            },
            "required": ["taskId", "code", "message"],
            "additionalProperties": False,
        },
    },
)


class Retrieval(Protocol):
    def search(self, request: SearchRequest) -> SearchResponse: ...

    def get_item(self, request: GetItemRequest) -> ItemResponse: ...

    def collections(self) -> CollectionMap: ...

    def status(self) -> RetrievalStatus: ...


class InvalidParams(ValueError):
    """A request reached a known method but its parameters are invalid."""


def _error(request_id: RequestId, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _success(request_id: RequestId, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _valid_request_id(value: object) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _request_id(payload: object) -> RequestId:
    if not isinstance(payload, dict) or "id" not in payload:
        return None
    value = payload["id"]
    return cast(RequestId, value) if _valid_request_id(value) else None


def _valid_envelope(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return False
    if "id" in payload and not _valid_request_id(payload["id"]):
        return False
    return "params" not in payload or isinstance(payload["params"], dict | list)


def _params_object(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params", {})
    if not isinstance(params, dict):
        raise InvalidParams("params must be an object")
    return cast(dict[str, Any], params)


def _reject_unknown(values: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = sorted(set(values).difference(allowed))
    if unknown:
        raise InvalidParams(f"unknown argument: {unknown[0]}")


def _nonblank_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams(f"{name} must be a non-blank string")
    return value


def _optional_strings(arguments: dict[str, Any], name: str) -> tuple[str, ...] | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise InvalidParams(f"{name} must be a non-empty array of strings")
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        raise InvalidParams(f"{name} must contain non-blank strings")
    return tuple(cast(list[str], value))


def _optional_datetime(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidParams(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidParams(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidParams(f"{name} must include a timezone")
    return value


def _search_request(arguments: dict[str, Any]) -> SearchRequest:
    _reject_unknown(arguments, _SEARCH_ARGUMENTS)
    query = _nonblank_string(arguments, "query")
    platforms = _optional_strings(arguments, "platforms")
    if platforms is not None and any(value not in SUPPORTED_PLATFORMS for value in platforms):
        raise InvalidParams("platforms contains an unsupported value")
    content_types = _optional_strings(arguments, "contentTypes")
    collections = _optional_strings(arguments, "collections")
    published_since = _optional_datetime(arguments, "publishedSince")
    published_until = _optional_datetime(arguments, "publishedUntil")
    favorited_since = _optional_datetime(arguments, "favoritedSince")
    favorited_until = _optional_datetime(arguments, "favoritedUntil")
    if (
        favorited_since is not None
        and favorited_until is not None
        and datetime.fromisoformat(favorited_since.replace("Z", "+00:00"))
        > datetime.fromisoformat(favorited_until.replace("Z", "+00:00"))
    ):
        raise InvalidParams("favoritedSince must not be later than favoritedUntil")
    if (
        published_since is not None
        and published_until is not None
        and datetime.fromisoformat(published_since.replace("Z", "+00:00"))
        > datetime.fromisoformat(published_until.replace("Z", "+00:00"))
    ):
        raise InvalidParams("publishedSince must not be later than publishedUntil")
    limit = arguments.get("limit", 10)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise InvalidParams("limit must be an integer between 1 and 50")
    return SearchRequest(
        query=query,
        platforms=platforms,
        content_types=content_types,
        collections=collections,
        published_since=published_since,
        published_until=published_until,
        favorited_since=favorited_since,
        favorited_until=favorited_until,
        limit=limit,
    )


def _get_item_request(arguments: dict[str, Any]) -> GetItemRequest:
    _reject_unknown(arguments, _GET_ITEM_ARGUMENTS)
    platform = _nonblank_string(arguments, "platform")
    if platform not in SUPPORTED_PLATFORMS:
        raise InvalidParams("platform contains an unsupported value")
    source_id = _nonblank_string(arguments, "sourceId")
    include_content = arguments.get("includeContent", True)
    if not isinstance(include_content, bool):
        raise InvalidParams("includeContent must be a boolean")
    return GetItemRequest(platform, source_id, include_content=include_content)


def _structured(value: SearchResponse | ItemResponse) -> dict[str, Any]:
    return asdict(value)


def _tool_result(payload: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": payload,
    }


def _exception_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    return str(exc).strip() or type(exc).__name__


def _tool_error(code: str, exc: Exception, stderr: TextIO) -> dict[str, Any]:
    # A Rejection states a rule the caller can satisfy, in words chosen for the
    # caller to read. Every other error is replaced with a constant, because it
    # may quote a path or a piece of the library back at whoever asked.
    message = _exception_message(exc) if isinstance(exc, Rejection) else _TOOL_ERROR_MESSAGES[code]
    print(f"favhub-mcp: tool error [{code}]: {_exception_message(exc)}", file=stderr)
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {"error": {"code": code, "message": message}},
        "isError": True,
    }


def _call_sync_tool(sync: SyncGateway, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, _SYNC_TOOL_ARGUMENTS[name])
    if name == "favhub.sync_start":
        started = sync.start(arguments)
        return _tool_result(started, f"Started FavHub sync job {started['job_id']}.")
    if name == "favhub.sync_submit_batch":
        receipt = sync.submit_batch(arguments)
        summary = (
            f"Accepted FavHub batch: {receipt['added']} added, "
            f"{receipt['refreshed']} refreshed, {receipt['duplicates']} duplicate(s), "
            f"{receipt['out_of_range']} out of range."
        )
        return _tool_result(receipt, summary)
    if name == "favhub.sync_pause":
        paused = sync.pause(arguments)
        return _tool_result(
            paused,
            f"Paused FavHub sync for {paused['platform']} [{paused['error']['code']}].",
        )
    if name == "favhub.sync_finish":
        finished = sync.finish(arguments)
        return _tool_result(
            finished,
            f"Finished FavHub scan for {finished['platform']['platform']}.",
        )
    status = sync.status(arguments)
    return _tool_result(status, f"FavHub sync job is {status['capture_status']}.")


def _call_browser_tool(
    browser: BrowserGateway, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    _reject_unknown(arguments, _BROWSER_TOOL_ARGUMENTS[name])
    if name == "favhub.browser_start":
        started = browser.start(arguments)
        session = started["browser_session"]
        # Whether the page was opened decides what, if anything, the user still
        # has to do, so it belongs in the sentence the Agent reads back.
        opened = started.get("opened_url")
        remaining = (
            "the saved-items page is opening now"
            if opened
            else "open the platform's saved-items page so the extension can take over"
        )
        return _tool_result(
            started,
            f"Started FavHub browser collection {started['job_id']} "
            f"for {session['platform']}; {remaining}.",
        )
    if name == "favhub.browser_resume":
        resumed = browser.resume(arguments)
        return _tool_result(
            resumed,
            f"Resumed FavHub browser collection for {resumed['platform']}.",
        )
    if name == "favhub.browser_cancel":
        cancelled = browser.cancel(arguments)
        return _tool_result(
            cancelled,
            f"Cancelled FavHub browser collection for {cancelled['platform']}; "
            "no frontier was advanced.",
        )
    status = browser.status(arguments)
    return _tool_result(status, f"FavHub browser job is {status['capture_status']}.")


def _call_github_tool(sync: SyncModule, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(arguments, _GITHUB_TOOL_ARGUMENTS[name])
    login = _nonblank_string(arguments, "user")
    mode = arguments.get("mode", "incremental")
    if mode not in {"full", "incremental"}:
        raise InvalidParams("mode must be full or incremental")
    max_scan_items = arguments.get("maxScanItems")
    if max_scan_items is not None and (
        not isinstance(max_scan_items, int)
        or isinstance(max_scan_items, bool)
        or max_scan_items < 1
    ):
        raise InvalidParams("maxScanItems must be a positive integer")
    # Read here, in the process that owns the data root. The token never
    # reaches the Agent: it is not an argument, and the result reports only
    # whether one was used.
    result = run_sync(
        sync,
        login=login,
        mode=str(mode),
        max_scan_items=max_scan_items,
        token=os.environ.get(GITHUB_TOKEN_ENV) or None,
    )
    error = result.get("error")
    if error is not None:
        return _tool_result(
            result, f"GitHub collection paused [{error['code']}]: {error['message']}"
        )
    counts = result["status"]["platforms"][0]["counts"]
    return _tool_result(
        result,
        f"Collected GitHub stars for {login}: {counts['added']} added, "
        f"{counts['duplicates']} already known.",
    )


def _call_enrich_tool(
    enrich: EnrichGateway, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    _reject_unknown(arguments, _ENRICH_TOOL_ARGUMENTS[name])
    if name == "favhub.enrich_next":
        claimed = enrich.next(arguments)
        summary = (
            "No pending FavHub enrichment task."
            if claimed["task"] is None
            else f"Claimed enrichment task {claimed['task']['task_id']}."
        )
        return _tool_result(claimed, summary)
    if name == "favhub.enrich_submit":
        submitted = enrich.submit(arguments)
        return _tool_result(
            submitted,
            f"Enrichment {submitted['outcome']} for task {submitted['task_id']}.",
        )
    skipped = enrich.skip(arguments)
    return _tool_result(
        skipped,
        f"Enrichment task {skipped['task_id']} skipped [{skipped['code']}].",
    )


def _call_tool(
    service: Retrieval,
    sync: SyncGateway | None,
    enrich: EnrichGateway | None,
    params: dict[str, Any],
    stderr: TextIO,
    browser: BrowserGateway | None = None,
    sync_module: SyncModule | None = None,
) -> dict[str, Any]:
    # `_meta` is reserved by MCP for protocol metadata — progress tokens and the
    # like — and clients attach it to ordinary calls. Rejecting it made every
    # tool call from a real client fail with "unknown argument: _meta" while the
    # test suite, which sends bare params, passed. Unknown *arguments* are still
    # refused below: a typo in a tool's own argument is a caller error, whereas
    # protocol metadata is simply not this layer's business.
    _reject_unknown(params, frozenset({"name", "arguments", "_meta"}))
    name = _nonblank_string(params, "name")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise InvalidParams("arguments must be an object")
    typed_arguments = cast(dict[str, Any], arguments)
    known_tools = {tool["name"] for tool in _TOOLS}
    if sync is not None:
        known_tools |= {tool["name"] for tool in _SYNC_TOOLS}
    if enrich is not None:
        known_tools |= {tool["name"] for tool in _ENRICH_TOOLS}
    if browser is not None:
        known_tools |= {tool["name"] for tool in _BROWSER_TOOLS}
    if sync_module is not None:
        known_tools |= {tool["name"] for tool in _GITHUB_TOOLS}
    if name not in known_tools:
        raise InvalidParams(f"unknown tool: {name}")
    try:
        if sync is not None and name in _SYNC_TOOL_ARGUMENTS:
            return _call_sync_tool(sync, name, typed_arguments)
        if enrich is not None and name in _ENRICH_TOOL_ARGUMENTS:
            return _call_enrich_tool(enrich, name, typed_arguments)
        if browser is not None and name in _BROWSER_TOOL_ARGUMENTS:
            return _call_browser_tool(browser, name, typed_arguments)
        if sync_module is not None and name in _GITHUB_TOOL_ARGUMENTS:
            return _call_github_tool(sync_module, name, typed_arguments)
        if name == "favhub.search":
            search_response = service.search(_search_request(typed_arguments))
            summary = (
                f"Found {search_response.total_returned} FavHub result(s)."
                if search_response.found
                else (f"No FavHub results: {search_response.reason or 'no matching content'}.")
            )
            return _tool_result(_structured(search_response), summary)
        if name == "favhub.collections":
            _reject_unknown(typed_arguments, frozenset())
            collection_map = service.collections().as_dict()
            unfiled = sum(int(row["unfiled"]) for row in collection_map["platforms"])
            return _tool_result(
                collection_map,
                f"The library has {len(collection_map['collections'])} folder(s) the user "
                f"made themselves, and {unfiled} item(s) no folder describes.",
            )
        if name == "favhub.get_item":
            item_response = service.get_item(_get_item_request(typed_arguments))
            return _tool_result(
                _structured(item_response),
                f"Loaded FavHub item {item_response.platform}/{item_response.source_id}.",
            )
        _reject_unknown(typed_arguments, frozenset())
        status = service.status()
        status_payload: dict[str, Any] = dict(status.as_dict())
        shared_summary = getattr(service, "embedding_summary", None)
        if callable(shared_summary):
            status_payload["embedding_summary"] = shared_summary()
            return _tool_result(
                status_payload,
                (
                    f"FavHub index has {status.indexed_items} item(s) and "
                    f"{status.indexed_chunks} chunk(s); {status.pending_index_tasks} pending, "
                    f"{status.failed_index_tasks} failed."
                ),
            )
        profiles = getattr(service, "embedding_profiles", None)
        if profiles is not None:
            try:
                embedding = profiles.summary()
                status_payload["embedding_summary"] = {
                    "state": embedding.state,
                    "active_profile": embedding.active_profile_metadata,
                    "current_chunks": embedding.current_chunks,
                    "embedded_chunks": embedding.embedded_chunks,
                    "pending_tasks": embedding.pending_tasks,
                    "failed_tasks": embedding.failed_tasks,
                    "corrupt_vectors": embedding.corrupt_vectors,
                    "last_build_report": embedding.last_build_report,
                }
            except Exception as exc:
                print(f"favhub-mcp: embedding status unavailable: {exc}", file=stderr)
                status_payload["embedding_summary"] = {"state": "unavailable"}
        return _tool_result(
            status_payload,
            (
                f"FavHub index has {status.indexed_items} item(s) and "
                f"{status.indexed_chunks} chunk(s); {status.pending_index_tasks} pending, "
                f"{status.failed_index_tasks} failed."
            ),
        )
    except InvalidParams:
        raise
    # Before RuntimeError, which it subclasses: a busy root is not a broken
    # index, and calling it one sends the reader looking in the wrong place.
    except DataRootBusyError as exc:
        return _tool_error("data_root_busy", exc, stderr)
    except SyncArgumentError as exc:
        raise InvalidParams(str(exc)) from exc
    except SourceSnapshotError as exc:
        return _tool_error("storage_error", exc, stderr)
    except KeyError as exc:
        return _tool_error("not_found", exc, stderr)
    except (sqlite3.Error, OSError, UnicodeError) as exc:
        return _tool_error("storage_error", exc, stderr)
    except (ValueError, TypeError) as exc:
        return _tool_error("invalid_argument", exc, stderr)
    except RuntimeError as exc:
        return _tool_error("index_unavailable", exc, stderr)


class _Lifecycle(Enum):
    NEW = auto()
    INITIALIZED = auto()
    READY = auto()


class _Session:
    def __init__(
        self,
        service: Retrieval,
        stderr: TextIO,
        sync: SyncGateway | None = None,
        enrich: EnrichGateway | None = None,
        browser: BrowserGateway | None = None,
        dispatcher: ApplicationDispatcher | None = None,
        sync_module: SyncModule | None = None,
    ) -> None:
        self.service = service
        self.stderr = stderr
        self.sync = sync
        self.enrich = enrich
        self.browser = browser
        self.sync_module = sync_module
        # Without a dispatcher this session owns the application outright, which
        # is the case for the unit tests and for a stdio-only run.
        self.dispatcher = dispatcher or ApplicationDispatcher()
        self.lifecycle = _Lifecycle.NEW

    def handle(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        has_id = "id" in payload
        request_id = _request_id(payload)
        method = cast(str, payload["method"])
        try:
            with self.dispatcher.operation():
                result = self._dispatch(method, _params_object(payload), has_id=has_id)
        except InvalidParams as exc:
            return _error(request_id, _INVALID_PARAMS, str(exc)) if has_id else None
        except Exception as exc:
            message = _exception_message(exc)
            print(f"favhub-mcp: internal error: {message}", file=self.stderr)
            return _error(request_id, _INTERNAL_ERROR, "internal error") if has_id else None
        if not has_id:
            return None
        if result is _METHOD_MISSING:
            return _error(request_id, _METHOD_NOT_FOUND, f"method not found: {method}")
        if result is _SERVER_NOT_READY:
            return _error(request_id, _INVALID_REQUEST, "server is not initialized")
        if result is _INITIALIZED_REQUIRES_NOTIFICATION:
            return _error(
                request_id,
                _INVALID_REQUEST,
                "notifications/initialized must be a notification",
            )
        return _success(request_id, result)

    def _dispatch(self, method: str, params: dict[str, Any], *, has_id: bool) -> Any:
        if method == "initialize":
            if not has_id:
                return None
            if self.lifecycle is not _Lifecycle.NEW:
                return _SERVER_NOT_READY
            protocol_version = params.get("protocolVersion")
            if not isinstance(protocol_version, str) or not protocol_version:
                raise InvalidParams("protocolVersion must be a non-blank string")
            if not isinstance(params.get("capabilities"), dict):
                raise InvalidParams("capabilities must be an object")
            if not isinstance(params.get("clientInfo"), dict):
                raise InvalidParams("clientInfo must be an object")
            self.lifecycle = _Lifecycle.INITIALIZED
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "notifications/initialized":
            if has_id:
                return _INITIALIZED_REQUIRES_NOTIFICATION
            if self.lifecycle is _Lifecycle.INITIALIZED:
                self.lifecycle = _Lifecycle.READY
            return None
        if self.lifecycle is not _Lifecycle.READY:
            return _SERVER_NOT_READY
        if method == "tools/list":
            if params:
                raise InvalidParams("tools/list params must be empty")
            tools = list(_TOOLS)
            if self.sync is not None:
                tools.extend(_SYNC_TOOLS)
            if self.enrich is not None:
                tools.extend(_ENRICH_TOOLS)
            if self.browser is not None:
                tools.extend(_BROWSER_TOOLS)
            if self.sync_module is not None:
                tools.extend(_GITHUB_TOOLS)
            return {"tools": tools}
        if method == "tools/call":
            return _call_tool(
                self.service,
                self.sync,
                self.enrich,
                params,
                self.stderr,
                browser=self.browser,
                sync_module=self.sync_module,
            )
        return _METHOD_MISSING


_METHOD_MISSING = object()
_SERVER_NOT_READY = object()
_INITIALIZED_REQUIRES_NOTIFICATION = object()


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def run_stdio(
    service: Retrieval,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    sync: SyncGateway | None = None,
    enrich: EnrichGateway | None = None,
    browser: BrowserGateway | None = None,
    dispatcher: ApplicationDispatcher | None = None,
    sync_module: SyncModule | None = None,
) -> None:
    """Serve one JSON-RPC object per input line until clean EOF."""
    session = _Session(service, stderr, sync, enrich, browser, dispatcher, sync_module)
    for raw_line in stdin:
        response: dict[str, Any] | None
        try:
            payload = json.loads(raw_line, parse_constant=_reject_nonstandard_constant)
        except (json.JSONDecodeError, ValueError):
            response = _error(None, _PARSE_ERROR, "parse error")
        else:
            if not _valid_envelope(payload):
                response = _error(_request_id(payload), _INVALID_REQUEST, "invalid request")
            else:
                response = session.handle(cast(dict[str, Any], payload))
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n")
            stdout.flush()


class _UnavailableRetrieval:
    """Serve sync-only sessions when the retrieval service is absent."""

    def search(self, request: SearchRequest) -> SearchResponse:
        raise RuntimeError("retrieval service is unavailable")

    def get_item(self, request: GetItemRequest) -> ItemResponse:
        raise RuntimeError("retrieval service is unavailable")

    def collections(self) -> CollectionMap:
        raise RuntimeError("retrieval service is unavailable")

    def status(self) -> RetrievalStatus:
        raise RuntimeError("retrieval service is unavailable")


class _BusyRetrieval:
    """Answer every tool with the reason this data root could not be opened."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def search(self, request: SearchRequest) -> SearchResponse:
        raise DataRootBusyError(self._reason)

    def get_item(self, request: GetItemRequest) -> ItemResponse:
        raise DataRootBusyError(self._reason)

    def collections(self) -> CollectionMap:
        raise DataRootBusyError(self._reason)

    def status(self) -> RetrievalStatus:
        raise DataRootBusyError(self._reason)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="favhub-mcp")
    parser.add_argument("--root", type=Path, required=True)
    return parser


def _start_browser_pipe(
    application: Application,
    dispatcher: ApplicationDispatcher,
    stderr: TextIO,
) -> BrowserPipeServer | None:
    """Serve the browser extension alongside stdio, or carry on without it.

    A pipe that will not start is a degraded browser feature, never a reason to
    take down the MCP session an Agent is already using: the Skill will report
    ``extension_missing`` when it cannot reach FavHub.
    """
    browser = application.browser_gateway
    if browser is None:
        return None

    def handle(request: dict[str, Any]) -> dict[str, Any]:
        with dispatcher.operation():
            return _handle_pipe_request(browser, request)

    server = BrowserPipeServer(application.paths, handle)
    try:
        server.start()
    except OSError as exc:
        print(f"favhub-mcp: browser pipe unavailable: {exc}", file=stderr)
        return None
    return server


def _handle_pipe_request(browser: BrowserGateway, request: dict[str, Any]) -> dict[str, Any]:
    """Decode one relayed message and run it against the shared application.

    Everything here arrived from a browser extension, so the closed protocol
    check runs first and an unknown type is refused rather than guessed at.
    """
    try:
        message = decode_message(json.dumps(request, ensure_ascii=False).encode("utf-8"))
    except BrowserProtocolError as error:
        return {"error": {"code": error.code, "message": error.message}}

    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        "session.claim": browser.claim_for_extension,
        "session.heartbeat": browser.heartbeat,
        "scope.declare": browser.declare_scopes,
        "capture.bundle": browser.submit_bundle,
        "capture.response": browser.submit_bundle,
        "session.finish": browser.finish_for_extension,
        "session.cancel": browser.cancel,
    }
    payload = message.payload
    try:
        if message.type == "session.pause":
            result = browser.pause_for_browser(
                str(payload.get("jobId", "")),
                str(payload.get("platform", "")),
                str(payload.get("code", "")),
                str(payload.get("message", "")),
            )
        else:
            handler = handlers.get(message.type)
            if handler is None:
                return {
                    "requestId": message.request_id,
                    "error": {
                        "code": "invalid_message",
                        "message": "The browser message type is not served.",
                    },
                }
            result = handler(dict(payload))
    except SyncArgumentError as error:
        return {
            "requestId": message.request_id,
            "error": {"code": "invalid_message", "message": str(error)},
        }
    return {"requestId": message.request_id, "result": result}


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    arguments = _build_parser().parse_args(argv)
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr

    try:
        application = Application.open(arguments.root)
    except DataRootBusyError as exc:
        # A second FavHub on the same root is an ordinary thing to have — one
        # per editor window — and only one of them can hold the root. Exiting
        # here would make the client report a server that failed to start,
        # which is both alarming and silent about the window that is actually
        # holding the root. Serving a session whose every tool answers
        # `data_root_busy` keeps the reason where the person can act on it.
        reason = _exception_message(exc)
        print(f"favhub-mcp: {reason}", file=error_stream)
        run_stdio(_BusyRetrieval(reason), input_stream, output_stream, error_stream)
        return 0
    except Exception as exc:
        print(f"favhub-mcp: failed to start: {_exception_message(exc)}", file=error_stream)
        return 1

    try:
        with application:
            sync_module = application.sync
            gateway = SyncGateway(sync_module) if sync_module is not None else None
            enrich: EnrichGateway | None = None
            if (
                application.database is not None
                and application.queue is not None
                and application.library is not None
                and application.store is not None
            ):
                enrich = EnrichGateway(
                    application.database,
                    application.queue,
                    application.library,
                    application.store,
                    indexer=application.indexer,
                )
            service: Retrieval
            if application.retrieval is not None:
                service = application.retrieval
            elif gateway is not None:
                # Browser-collected data can still be submitted through the
                # sync tools; retrieval tools report index_unavailable.
                service = _UnavailableRetrieval()
            else:
                raise RuntimeError("retrieval service is unavailable")
            dispatcher = ApplicationDispatcher()
            pipe = _start_browser_pipe(application, dispatcher, error_stream)
            try:
                run_stdio(
                    service,
                    input_stream,
                    output_stream,
                    error_stream,
                    sync=gateway,
                    enrich=enrich,
                    browser=application.browser_gateway,
                    dispatcher=dispatcher,
                    sync_module=sync_module,
                )
            finally:
                if pipe is not None:
                    pipe.stop()
    except Exception as exc:
        print(f"favhub-mcp: failed to start: {_exception_message(exc)}", file=error_stream)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PROTOCOL_VERSION", "main", "run_stdio"]
