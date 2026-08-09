"""Typed gateway between MCP sync tool arguments and the sync module.

The gateway receives already schema-checked MCP dictionaries (camelCase keys)
and translates them into typed :mod:`favhub.sync_module` calls. It never
accepts a local filesystem path and never performs network access: item and
asset contents arrive inline as validated UTF-8 text and all persistence goes
through the existing ``SyncModule``/``LibraryModule`` transaction boundary.

Error contract:

- :class:`SyncArgumentError` (a ``ValueError`` subclass) marks a malformed
  top-level argument; the MCP layer maps it to JSON-RPC ``invalid params``.
- Plain ``ValueError`` marks invalid item/asset content and maps to the
  sanitized ``invalid_argument`` tool error.
- ``KeyError`` marks an unknown job/platform/scope and maps to ``not_found``.
"""

import unicodedata
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
from typing import Any

from favhub.domain import (
    SCOPED_PLATFORMS,
    SUPPORTED_PLATFORMS,
    CapturedAsset,
    CapturedItem,
    SyncMode,
)
from favhub.sync_module import ScopeFinish, StartSyncRequest, SyncModule

# Stable platform-level pause causes (design §6). Anything else is rejected so
# logs and status payloads only ever contain known, sanitized codes.
PAUSE_CODES = frozenset(
    {
        "login_required",
        "captcha_required",
        "rate_limited",
        "page_changed",
        "browser_unavailable",
    }
)

_SYNC_PLATFORMS = SUPPORTED_PLATFORMS
_SCOPED_PLATFORMS = SCOPED_PLATFORMS
_MAX_BATCH_ITEMS = 50
_MAX_SCAN_IDS_PER_SCOPE = 200
_MAX_FRONTIER_IDS = 100
_MAX_SCOPES = 100
_MAX_PAUSE_MESSAGE = 200

_ITEM_KEYS = frozenset(
    {
        "sourceId",
        "canonicalUrl",
        "title",
        "author",
        "publishedAt",
        "observedAt",
        "body",
        "collections",
        "extractorVersion",
        "platformMetadata",
        "assets",
    }
)
_ASSET_KEYS = frozenset({"relativePath", "mediaType", "text", "sha256"})


class SyncArgumentError(ValueError):
    """A malformed top-level MCP argument for a sync tool."""


class Rejection(ValueError):
    """A refusal whose text the caller is meant to read and act on.

    Tool errors are sanitized to a constant before they leave the process,
    because a ``ValueError`` from deep in the stack can carry a file path or a
    fragment of the user's library and the caller is an Agent that will repeat
    whatever it is told. That is the right default for an error nobody planned.

    It is the wrong default for a rule. Enrichment's rules — a summary must be
    shorter than its source, Chinese content needs a Chinese tag — are refusals
    the caller can satisfy on the next try, and each one is raised with a
    sentence saying how. Sanitizing those to "Invalid tool argument." cost one
    real run fifteen identical retries against an item with an empty body,
    after which the Agent concluded the tool was broken and said so. It had no
    way to learn otherwise.

    Messages raised as this type are authored constants, so they are returned
    verbatim. Nothing derived from item content may be raised through it.
    """


class SyncGateway:
    def __init__(self, sync: SyncModule) -> None:
        self._sync = sync

    def start(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        platform = _required_platform(arguments)
        mode = _required_string(arguments, "mode")
        if mode not in {SyncMode.FULL.value, SyncMode.INCREMENTAL.value}:
            raise SyncArgumentError("mode must be 'full' or 'incremental'")
        published_since = _optional_aware_datetime(arguments, "publishedSince")
        published_until = _optional_aware_datetime(arguments, "publishedUntil")
        max_scan_items = arguments.get("maxScanItems")
        if max_scan_items is not None and (
            isinstance(max_scan_items, bool)
            or not isinstance(max_scan_items, int)
            or max_scan_items < 1
        ):
            raise SyncArgumentError("maxScanItems must be an integer of at least 1")
        _reject_scoped_arguments(platform, arguments, "scopes")
        scope_ids, scope_names = _parse_scopes(arguments.get("scopes"))
        try:
            request = StartSyncRequest(
                platforms=(platform,),
                mode=SyncMode(mode),
                published_since=published_since,
                published_until=published_until,
                max_scan_items=max_scan_items,
                scope_ids=scope_ids,
                scope_names=scope_names,
            )
        except ValueError as exc:
            raise SyncArgumentError(str(exc)) from exc
        result = self._sync.start_sync(request)
        return {
            "job_id": result.job_id,
            "frontiers": {name: list(ids) for name, ids in result.frontiers.items()},
            "scoped_frontiers": {
                scope: list(ids) for scope, ids in result.scoped_frontiers.items()
            },
        }

    def submit_batch(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _required_string(arguments, "jobId")
        platform = _required_platform(arguments)
        batch_id = _required_string(arguments, "batchId")
        raw_items = arguments.get("items")
        if not isinstance(raw_items, list):
            raise SyncArgumentError("items must be an array")
        if len(raw_items) > _MAX_BATCH_ITEMS:
            raise SyncArgumentError(f"items must contain at most {_MAX_BATCH_ITEMS} entries")
        _reject_scoped_arguments(platform, arguments, "scopeScans")
        scope_scans = _parse_scope_map(
            arguments.get("scopeScans"), "scopeScans", _MAX_SCAN_IDS_PER_SCOPE
        )
        items: list[CapturedItem] = []
        seen_ids: set[str] = set()
        for raw in raw_items:
            item = _captured_item(platform, raw)
            if item.source_id in seen_ids:
                raise ValueError(f"duplicate item source id in batch: {item.source_id}")
            seen_ids.add(item.source_id)
            items.append(item)
        receipt = self._sync.submit_batch(job_id, platform, batch_id, items, scope_scans)
        return asdict(receipt)

    def pause(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _required_string(arguments, "jobId")
        platform = _required_platform(arguments)
        code = _required_string(arguments, "code")
        if code not in PAUSE_CODES:
            raise SyncArgumentError(f"code must be one of {sorted(PAUSE_CODES)}")
        message = _sanitized_message(_required_string(arguments, "message"))
        self._sync.pause_sync(job_id, platform, code, message)
        return {
            "job_id": job_id,
            "platform": platform,
            "status": "paused",
            "error": {"code": code, "message": message},
        }

    def finish(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _required_string(arguments, "jobId")
        platform = _required_platform(arguments)
        observed_end = arguments.get("observedEnd")
        max_scan_reached = arguments.get("maxScanReached")
        if type(observed_end) is not bool or type(max_scan_reached) is not bool:
            raise SyncArgumentError("observedEnd and maxScanReached must be booleans")
        visible_total = arguments.get("visibleTotal")
        if visible_total is not None and (
            isinstance(visible_total, bool)
            or not isinstance(visible_total, int)
            or visible_total < 0
        ):
            raise SyncArgumentError("visibleTotal must be a non-negative integer or null")
        _reject_scoped_arguments(platform, arguments, "frontierScopes", "scopeResults")
        frontier_ids = _parse_id_list(
            arguments.get("frontierIds"), "frontierIds", _MAX_FRONTIER_IDS
        )
        frontier_scopes = _parse_scope_map(
            arguments.get("frontierScopes"), "frontierScopes", _MAX_FRONTIER_IDS
        )
        scope_results = _parse_scope_results(arguments.get("scopeResults"))
        self._sync.finish_scan(
            job_id,
            platform,
            observed_end=observed_end,
            max_scan_reached=max_scan_reached,
            visible_total=visible_total,
            frontier_ids=frontier_ids,
            frontier_scopes=frontier_scopes,
            scope_results=scope_results,
        )
        status = self._sync.get_status(job_id)
        platform_entry = next(
            entry for entry in status["platforms"] if entry["platform"] == platform
        )
        return {
            "job_id": job_id,
            "capture_status": status["capture_status"],
            "platform": platform_entry,
        }

    def status(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _required_string(arguments, "jobId")
        return self._sync.get_status(job_id)

    # -- internal browser helpers --------------------------------------------
    #
    # These take typed arguments instead of an untrusted argument Mapping and
    # are reachable only from in-process browser code. They are deliberately not
    # registered as favhub.sync_* tools: the browser is not an Agent-facing
    # caller, and exposing resume/registration over MCP would widen the public
    # schema for no user-visible gain.

    def resume_run(self, job_id: str, platform: str) -> None:
        if platform not in _SYNC_PLATFORMS:
            raise SyncArgumentError(f"unsupported platform: {platform}")
        try:
            self._sync.resume_sync(job_id, platform)
        except (KeyError, ValueError) as error:
            raise SyncArgumentError(str(error)) from error

    def register_browser_scopes(
        self,
        job_id: str,
        platform: str,
        scopes: Mapping[str, str],
    ) -> dict[str, tuple[str, ...]]:
        if platform not in _SCOPED_PLATFORMS:
            raise SyncArgumentError(
                f"scopes are only supported for {' and '.join(sorted(_SCOPED_PLATFORMS))}"
            )
        if len(scopes) > _MAX_SCOPES:
            raise SyncArgumentError(f"at most {_MAX_SCOPES} scopes may be registered")
        try:
            return self._sync.register_scopes(job_id, platform, scopes)
        except (KeyError, ValueError) as error:
            raise SyncArgumentError(str(error)) from error


def _reject_scoped_arguments(platform: str, arguments: Mapping[str, Any], *names: str) -> None:
    """Folder scopes exist only on folder platforms; others must not send them."""
    if platform in _SCOPED_PLATFORMS:
        return
    present = [name for name in names if arguments.get(name) is not None]
    if present:
        raise SyncArgumentError(
            f"{present[0]} is only supported for {' and '.join(sorted(_SCOPED_PLATFORMS))}"
            " (bilibili-style folder scopes)"
        )


def _required_platform(arguments: Mapping[str, Any]) -> str:
    platform = _required_string(arguments, "platform")
    if platform not in _SYNC_PLATFORMS:
        raise SyncArgumentError(f"platform must be one of {sorted(_SYNC_PLATFORMS)}")
    return platform


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SyncArgumentError(f"{name} must be a non-blank string")
    return value


def _optional_aware_datetime(arguments: Mapping[str, Any], name: str) -> datetime | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SyncArgumentError(f"{name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncArgumentError(f"{name} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SyncArgumentError(f"{name} must include a timezone")
    return parsed


def _parse_scopes(
    raw: object,
) -> tuple[tuple[str, ...] | None, dict[str, str] | None]:
    if raw is None:
        return None, None
    if not isinstance(raw, list) or not raw:
        raise SyncArgumentError("scopes must be a non-empty array")
    if len(raw) > _MAX_SCOPES:
        raise SyncArgumentError(f"scopes must contain at most {_MAX_SCOPES} entries")
    scope_ids: list[str] = []
    scope_names: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise SyncArgumentError("scopes entries must be objects")
        unknown = sorted(set(entry) - {"scopeId", "scopeName"})
        if unknown:
            raise SyncArgumentError(f"unknown scope field: {unknown[0]}")
        scope_id = entry.get("scopeId")
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise SyncArgumentError("scopeId must be a non-blank string")
        if scope_id in scope_ids:
            raise SyncArgumentError(f"duplicate scopeId: {scope_id}")
        scope_ids.append(scope_id)
        if "scopeName" in entry:
            name = entry["scopeName"]
            if not isinstance(name, str) or not name.strip():
                raise SyncArgumentError("scopeName must be a non-blank string")
            scope_names[scope_id] = name
    return tuple(scope_ids), (scope_names or None)


def _parse_id_list(raw: object, field: str, limit: int) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SyncArgumentError(f"{field} must be an array of strings")
    if len(raw) > limit:
        raise SyncArgumentError(f"{field} must contain at most {limit} entries")
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise SyncArgumentError(f"{field} must contain non-blank strings")
    return tuple(raw)


def _parse_scope_map(raw: object, field: str, limit: int) -> dict[str, tuple[str, ...]] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SyncArgumentError(f"{field} must be an object mapping scope ids to source ids")
    if len(raw) > _MAX_SCOPES:
        raise SyncArgumentError(f"{field} must contain at most {_MAX_SCOPES} scopes")
    parsed: dict[str, tuple[str, ...]] = {}
    for scope_id, values in raw.items():
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise SyncArgumentError(f"{field} keys must be non-blank strings")
        if not isinstance(values, list):
            raise SyncArgumentError(f"{field} values must be arrays of strings")
        if len(values) > limit:
            raise SyncArgumentError(f"{field}[{scope_id}] must contain at most {limit} entries")
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise SyncArgumentError(f"{field} values must contain non-blank strings")
        parsed[scope_id] = tuple(values)
    return parsed


def _parse_scope_results(raw: object) -> dict[str, ScopeFinish] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SyncArgumentError("scopeResults must be an object mapping scope ids to results")
    if len(raw) > _MAX_SCOPES:
        raise SyncArgumentError(f"scopeResults must contain at most {_MAX_SCOPES} scopes")
    parsed: dict[str, ScopeFinish] = {}
    for scope_id, value in raw.items():
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise SyncArgumentError("scopeResults keys must be non-blank strings")
        if not isinstance(value, Mapping):
            raise SyncArgumentError("scopeResults values must be objects")
        unknown = sorted(set(value) - {"maxScanReached", "visibleTotal"})
        if unknown:
            raise SyncArgumentError(f"unknown scopeResults field: {unknown[0]}")
        flag = value.get("maxScanReached", False)
        if type(flag) is not bool:
            raise SyncArgumentError("scopeResults maxScanReached must be a boolean")
        total = value.get("visibleTotal")
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise SyncArgumentError(
                "scopeResults visibleTotal must be a non-negative integer or null"
            )
        parsed[scope_id] = ScopeFinish(max_scan_reached=flag, visible_total=total)
    return parsed


def _sanitized_message(value: str) -> str:
    cleaned = "".join(
        character for character in value if unicodedata.category(character) != "Cc"
    ).strip()
    if not cleaned:
        raise SyncArgumentError("message must not be blank")
    return cleaned[:_MAX_PAUSE_MESSAGE]


def _captured_item(platform: str, raw: object) -> CapturedItem:
    if not isinstance(raw, Mapping):
        raise ValueError("batch items must be objects")
    unknown = sorted(set(raw) - _ITEM_KEYS)
    if unknown:
        raise ValueError(f"unknown item field: {unknown[0]}")
    author = raw.get("author")
    if author is not None and not isinstance(author, str):
        raise ValueError("item author must be a string or null")
    body = raw.get("body")
    if not isinstance(body, str):
        raise ValueError("item body must be a string")
    collections_raw = raw.get("collections", [])
    if not isinstance(collections_raw, list) or not all(
        isinstance(value, str) for value in collections_raw
    ):
        raise ValueError("item collections must be an array of strings")
    metadata = raw.get("platformMetadata")
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ValueError("item platformMetadata must be an object")
    return CapturedItem(
        platform=platform,
        source_id=_item_string(raw, "sourceId"),
        canonical_url=_item_string(raw, "canonicalUrl"),
        title=_item_string(raw, "title"),
        author=author,
        published_at=_item_datetime(raw, "publishedAt"),
        observed_at=_item_datetime(raw, "observedAt"),
        body=body,
        collections=tuple(collections_raw),
        extractor_version=_item_string(raw, "extractorVersion"),
        platform_metadata=dict(metadata) if metadata is not None else None,
        assets=_captured_assets(raw.get("assets")),
    )


def _captured_assets(raw: object) -> tuple[CapturedAsset, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("item assets must be an array")
    assets: list[CapturedAsset] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ValueError("item assets entries must be objects")
        unknown = sorted(set(entry) - _ASSET_KEYS)
        if unknown:
            raise ValueError(f"unknown asset field: {unknown[0]}")
        for key in ("relativePath", "mediaType", "text", "sha256"):
            if not isinstance(entry.get(key), str):
                raise ValueError(f"asset {key} must be a string")
        assets.append(
            CapturedAsset(
                relative_path=str(entry["relativePath"]),
                media_type=str(entry["mediaType"]),
                text=str(entry["text"]),
                sha256=str(entry["sha256"]),
            )
        )
    return tuple(assets)


def _item_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"item {key} must be a non-blank string")
    return value


def _item_datetime(raw: Mapping[str, Any], key: str) -> datetime:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"item {key} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"item {key} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"item {key} must include a timezone")
    return parsed


__all__ = ["PAUSE_CODES", "Rejection", "SyncArgumentError", "SyncGateway"]
