"""Turn raw browser responses into captured items using the existing parsers.

This module is a router, not a parser. Every platform-specific rule — what a
tweet's text is, which folder name wins, how a subtitle becomes a transcript —
already lives in ``*_parsers`` and ``*_mapping`` and is used here unchanged. The
only new logic is *when* to submit.

That timing differs by platform, and the reason is folders:

- X has one bookmark list, so a mapped tweet is final the moment it is parsed
  and batches can flush as they fill.
- Bilibili and Zhihu keep the same item in several folders, and the collections
  list and earliest favorited time are only known once every folder has been
  seen. Their observations therefore stay in memory until ``finish``.

In-memory means exactly that: raw response bodies are never written to disk or
logged, and a process restart loses pending observations. That is deliberate —
the session pauses and the next run rescans, which is cheaper than persisting
platform responses FavHub has no reason to keep.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from favhub import (
    bilibili_mapping,
    bilibili_parsers,
    x_mapping,
    x_parsers,
    zhihu_mapping,
    zhihu_parsers,
)
from favhub.bilibili_models import BilibiliListEntry, BilibiliSubtitle, BilibiliVideo
from favhub.browser_capture import BrowserCaptureError, BrowserCaptureStatus, BrowserCaptureStore
from favhub.capture import (
    BROWSER_ERROR_CODES,
    CAPTURE_ERROR_CODES,
    INVALID_MESSAGE,
    PAGE_CHANGED,
    CaptureError,
)
from favhub.domain import CapturedItem
from favhub.sync_module import ScopeFinish, SyncModule
from favhub.zhihu_models import ZhihuFavorite

# One batch may carry at most this many items; the extension is also asked to
# stop at 20 so a flush maps one-to-one onto what it just acknowledged.
BATCH_SIZE = 20

_EVENT_KINDS = {
    "x": frozenset({"x.bookmarks_page"}),
    "bilibili": frozenset({"bilibili.video_bundle"}),
    "zhihu": frozenset({"zhihu.items_page"}),
}

_INGEST_ERROR_CODES = BROWSER_ERROR_CODES | CAPTURE_ERROR_CODES


class BrowserIngestError(Exception):
    """A rejected event, carrying a stable code and a redacted message."""

    def __init__(self, code: str, message: str) -> None:
        if code not in _INGEST_ERROR_CODES:
            raise ValueError(f"unknown browser ingest error code: {code}")
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(slots=True)
class _SessionState:
    job_id: str
    platform: str
    batch_ordinal: int = 0
    scope_scans_sent: bool = False
    # X only: mapped items awaiting a flush.
    pending: list[CapturedItem] = field(default_factory=list)
    # Scoped platforms: folder names and per-folder observations.
    folder_names: dict[str, str] = field(default_factory=dict)
    entries_by_scope: dict[str, list[BilibiliListEntry]] = field(default_factory=dict)
    favorites_by_scope: dict[str, list[ZhihuFavorite]] = field(default_factory=dict)
    details: dict[str, BilibiliVideo] = field(default_factory=dict)
    subtitles: dict[str, BilibiliSubtitle] = field(default_factory=dict)
    subtitle_raw: dict[str, str] = field(default_factory=dict)
    subtitle_source: dict[str, str] = field(default_factory=dict)
    subtitle_mismatch: dict[str, str] = field(default_factory=dict)


def _default_clock() -> datetime:
    return datetime.now(UTC)


class BrowserIngestor:
    def __init__(
        self,
        sync: SyncModule,
        sessions: BrowserCaptureStore,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._sync = sync
        self._sessions = sessions
        self._clock = clock
        self._states: dict[str, _SessionState] = {}

    # -- public API ----------------------------------------------------------

    def handle(self, session_id: str, event: Mapping[str, Any]) -> dict[str, Any]:
        state = self._state(session_id)
        platform = _required(event, "platform")
        if platform != state.platform:
            raise BrowserIngestError(
                INVALID_MESSAGE, f"event platform does not match session platform: {platform}"
            )
        kind = _required(event, "kind")
        if kind not in _EVENT_KINDS[platform]:
            raise BrowserIngestError(INVALID_MESSAGE, f"unsupported event kind: {kind}")
        try:
            if platform == "x":
                return self._handle_x(state, event)
            if platform == "bilibili":
                return self._handle_bilibili(state, event)
            return self._handle_zhihu(state, event)
        except CaptureError as error:
            # Parser codes cross unchanged: a logged-out session or a changed
            # page is a platform condition, not a protocol violation.
            raise BrowserIngestError(error.code, error.message) from error

    def finish(
        self,
        session_id: str,
        *,
        observed_end: bool,
        max_scan_reached: bool,
        visible_total: int | None,
        frontier_ids: tuple[str, ...],
        frontier_scopes: Mapping[str, tuple[str, ...]] | None,
        scope_results: Mapping[str, ScopeFinish] | None,
    ) -> None:
        """Flush what is still in memory, then close the scan.

        The order matters: everything mapped must be durably submitted before
        ``finish_scan`` advances a frontier, otherwise a crash between the two
        would skip items the next incremental run believes it already has.
        """
        state = self._states.get(session_id)
        if state is not None:
            self._flush_all(state)
        job_id = state.job_id if state is not None else self._sessions.get(session_id).job_id
        platform = state.platform if state is not None else self._sessions.get(session_id).platform
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
        self._states.pop(session_id, None)

    def drop(self, session_id: str) -> None:
        """Forget pending observations without writing anything."""
        self._states.pop(session_id, None)

    # -- platform routing ----------------------------------------------------

    def _handle_x(self, state: _SessionState, event: Mapping[str, Any]) -> dict[str, Any]:
        page = x_parsers.parse_bookmarks_page(_decoded_body(event))
        observed_at = self._clock()
        for tweet in page.tweets:
            state.pending.append(
                x_mapping.map_captured_item(
                    tweet,
                    image_descriptions=None,
                    observed_at=observed_at,
                )
            )
        submitted: list[str] = []
        while len(state.pending) >= BATCH_SIZE:
            batch = state.pending[:BATCH_SIZE]
            del state.pending[:BATCH_SIZE]
            submitted.append(self._submit(state, batch, scope_scans=None))
        return {
            "mapped": len(page.tweets),
            "has_more": page.has_more,
            "cursor": page.bottom_cursor,
            "submitted": submitted,
        }

    def _handle_bilibili(self, state: _SessionState, event: Mapping[str, Any]) -> dict[str, Any]:
        scope_id, scope_name = self._scope(state, event)
        entry = _parse_single_media(event.get("resource"))
        state.entries_by_scope.setdefault(scope_id, []).append(entry)
        state.folder_names[scope_id] = scope_name

        detail_payload = event.get("detail")
        if detail_payload is not None:
            state.details[entry.bvid] = bilibili_parsers.parse_video_detail(detail_payload)
        subtitle_payload = event.get("subtitle")
        if subtitle_payload is not None:
            state.subtitles[entry.bvid] = bilibili_parsers.parse_subtitle(subtitle_payload)
        raw = event.get("subtitleRaw")
        if isinstance(raw, str):
            state.subtitle_raw[entry.bvid] = raw
        source = event.get("subtitleSource")
        if isinstance(source, str):
            state.subtitle_source[entry.bvid] = source
        mismatch = event.get("subtitleMismatch")
        if isinstance(mismatch, str):
            state.subtitle_mismatch[entry.bvid] = mismatch
        return {"mapped": 1, "submitted": []}

    def _handle_zhihu(self, state: _SessionState, event: Mapping[str, Any]) -> dict[str, Any]:
        scope_id, scope_name = self._scope(state, event)
        page = zhihu_parsers.parse_items_page(_decoded_body(event))
        state.favorites_by_scope.setdefault(scope_id, []).extend(page.favorites)
        state.folder_names[scope_id] = scope_name
        return {
            "mapped": len(page.favorites),
            "is_end": page.is_end,
            "submitted": [],
        }

    # -- flushing ------------------------------------------------------------

    def _flush_all(self, state: _SessionState) -> None:
        if state.platform == "x":
            items = state.pending
            state.pending = []
            self._submit_in_batches(state, items, scope_scans=None)
        elif state.platform == "bilibili":
            self._flush_bilibili(state)
        else:
            self._flush_zhihu(state)

    def _flush_bilibili(self, state: _SessionState) -> None:
        observations = bilibili_mapping.deduplicate(state.entries_by_scope, state.folder_names)
        observed_at = self._clock()
        items = [
            bilibili_mapping.map_captured_item(
                observation,
                detail=state.details.get(bvid),
                subtitle=state.subtitles.get(bvid),
                subtitle_raw=state.subtitle_raw.get(bvid),
                subtitle_source=state.subtitle_source.get(bvid),
                subtitle_mismatch=state.subtitle_mismatch.get(bvid),
                observed_at=observed_at,
            )
            for bvid, observation in observations.items()
        ]
        items = [item for item in items if not self._would_lose_a_transcript(item)]
        scans = {
            scope_id: tuple(entry.bvid for entry in entries)
            for scope_id, entries in state.entries_by_scope.items()
        }
        state.entries_by_scope = {}
        self._submit_in_batches(state, items, scope_scans=scans)

    def _would_lose_a_transcript(self, item: CapturedItem) -> bool:
        """True when storing this capture would drop a transcript already held.

        ``wrong_video`` says Bilibili is handing out another video's transcript
        right now, not that this video has none — the same video served a
        correct one an hour earlier and was refused on the next pass. Storing
        the refusal replaces a good transcript with nothing, so every run made
        while the platform misbehaves strips more of the library, which is worse
        than the fault being guarded against.

        The whole capture is skipped rather than merged. Its metadata is a
        refresh that can wait for a run where the platform answers properly;
        the transcript is not something to re-fetch at will.
        """
        metadata = item.platform_metadata or {}
        if metadata.get("subtitle_status") != "wrong_video":
            return False
        stored = self._sync.library.store.read_source(item.platform, item.source_id)
        if stored is None:
            return False
        return (stored.get("platform_metadata") or {}).get("subtitle_status") == "available"

    def _flush_zhihu(self, state: _SessionState) -> None:
        observations = zhihu_mapping.deduplicate(state.favorites_by_scope, state.folder_names)
        observed_at = self._clock()
        items = [
            zhihu_mapping.map_captured_item(
                observation.favorite,
                collection_titles=observation.collections,
                observed_at=observed_at,
            )
            for observation in observations.values()
        ]
        scans = {
            scope_id: tuple(zhihu_mapping.source_id_for(favorite.content) for favorite in favorites)
            for scope_id, favorites in state.favorites_by_scope.items()
        }
        state.favorites_by_scope = {}
        self._submit_in_batches(state, items, scope_scans=scans)

    def _submit_in_batches(
        self,
        state: _SessionState,
        items: Sequence[CapturedItem],
        *,
        scope_scans: Mapping[str, tuple[str, ...]] | None,
    ) -> None:
        if not items:
            if scope_scans:
                self._submit(state, [], scope_scans=scope_scans)
            return
        for start in range(0, len(items), BATCH_SIZE):
            self._submit(state, list(items[start : start + BATCH_SIZE]), scope_scans=scope_scans)
            # Scanned counts describe the whole scan, not one batch, so they ride
            # along exactly once; repeating them would inflate per-folder totals.
            scope_scans = None

    def _submit(
        self,
        state: _SessionState,
        items: list[CapturedItem],
        *,
        scope_scans: Mapping[str, tuple[str, ...]] | None,
    ) -> str:
        state.batch_ordinal += 1
        batch_id = f"browser-batch-{state.batch_ordinal:04d}"
        self._sync.submit_batch(
            state.job_id,
            state.platform,
            batch_id,
            items,
            scope_scans=scope_scans,
        )
        return batch_id

    # -- helpers -------------------------------------------------------------

    def _state(self, session_id: str) -> _SessionState:
        state = self._states.get(session_id)
        if state is not None:
            return state
        try:
            session = self._sessions.get(session_id)
        except BrowserCaptureError as error:
            raise BrowserIngestError(INVALID_MESSAGE, "unknown browser capture session") from error
        if session.status is not BrowserCaptureStatus.CAPTURING:
            raise BrowserIngestError(
                INVALID_MESSAGE, f"session is not capturing: {session.status.value}"
            )
        state = _SessionState(job_id=session.job_id, platform=session.platform)
        self._states[session_id] = state
        return state

    def _scope(self, state: _SessionState, event: Mapping[str, Any]) -> tuple[str, str]:
        scope_id = _required(event, "scopeId")
        scope_name = _required(event, "scopeName")
        known = {
            str(row["scope_id"])
            for row in self._sync.database.connection.execute(
                "SELECT scope_id FROM sync_scope_runs WHERE job_id = ? AND platform = ?",
                (state.job_id, state.platform),
            )
        }
        if scope_id not in known:
            raise BrowserIngestError(INVALID_MESSAGE, f"scope is not registered: {scope_id}")
        return scope_id, scope_name


def _decoded_body(event: Mapping[str, Any]) -> Any:
    """Decode the response body the browser forwarded.

    The extension sends bodies verbatim, as the text the platform returned: it
    deliberately does not parse them, so that what FavHub stores is decided here
    and not in a page's own world. The parsers, however, are shared with the CDP
    collectors and are handed decoded payloads there, so the decode belongs at
    this boundary — passing the raw text straight through made every X page fail
    as `page_changed`, which reads like the platform changed when nothing had.
    """
    body = event.get("body")
    if not isinstance(body, str):
        # Already decoded (an active-mode adapter, or a test): the parsers do
        # their own shape checking, so nothing more is needed here.
        return body
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError) as error:
        raise BrowserIngestError(PAGE_CHANGED, "response body is not valid JSON") from error


def _parse_single_media(resource: object) -> BilibiliListEntry:
    """Reuse the page parser for one entry rather than duplicating its checks."""
    page = bilibili_parsers.parse_resource_page(
        json.loads(json.dumps({"code": 0, "data": {"medias": [resource], "has_more": False}}))
    )
    return page.entries[0]


def _required(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BrowserIngestError(INVALID_MESSAGE, f"{key} must be a non-blank string")
    return value


__all__ = [
    "BATCH_SIZE",
    "BrowserIngestError",
    "BrowserIngestor",
]
