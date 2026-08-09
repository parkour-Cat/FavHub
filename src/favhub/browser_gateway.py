"""Agent-facing browser capture operations.

The Agent starts, resumes, inspects, and cancels a browser collection run; the
extension never talks to these entry points. Two pieces of state move together
here — the sync platform run and the browser capture session — so this module
keeps them consistent: a run is never left ``running`` with nothing driving it,
and a cancelled session never advances a frontier.

Argument validation mirrors ``SyncGateway``: camelCase input, snake_case
results, no local paths, no arbitrary URLs, no credentials.
"""

import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any

from favhub.browser_capture import (
    BROWSER_PROTOCOL_VERSION,
    BrowserCaptureError,
    BrowserCaptureSession,
    BrowserCaptureStatus,
    BrowserCaptureStore,
)
from favhub.browser_ingest import BrowserIngestError, BrowserIngestor
from favhub.capture import BROWSER_UNAVAILABLE, CANCELLED_BY_USER, EXTENSION_VERSION_MISMATCH
from favhub.sync_gateway import SyncArgumentError, SyncGateway
from favhub.sync_module import ScopeFinish, SyncModule

# GitHub is collected through its public API and needs no browser, so it is not
# a browser platform even though it is a sync platform.
BROWSER_PLATFORMS = frozenset({"bilibili", "x", "zhihu"})

# Cancelling is deliberately absent from SyncGateway.PAUSE_CODES: that set is
# part of the public favhub.sync_pause schema, and cancelling is not something
# an Agent reports through that tool. The code itself lives in the shared
# capture contract so every layer spells it the same way.
SESSION_UNAVAILABLE_MESSAGE = "browser session is not resumable"

# Long enough to survive a slow page or a rate-limit backoff, short enough that
# a browser that vanished is noticed within one status check.
LEASE_SECONDS = 180


class BrowserGateway:
    def __init__(
        self,
        sync_gateway: SyncGateway,
        sync: SyncModule,
        sessions: BrowserCaptureStore,
        ingestor: BrowserIngestor | None = None,
        # Opening a browser window is a side effect on the user's actual desktop,
        # so it is never the default: only the production wiring in
        # `Application.open` asks for it. Defaulting to the real launcher meant
        # every test that started a run through an Application opened a real tab,
        # and a full suite run buried the user's browser in them.
        open_page: Callable[[str], str | None] = lambda _platform: None,
        # Read per claim rather than captured at startup: `favhub setup` can run
        # while this process is serving, and the answer must follow the files.
        # Defaulting to "unknown" keeps every test that does not care about the
        # installed copy from having to describe one.
        installed_version: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._sync_gateway = sync_gateway
        self._sync = sync
        self._sessions = sessions
        self._ingestor = ingestor
        self._open_page = open_page
        self._installed_version = installed_version

    def start(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        platform = _browser_platform(arguments)
        started = self._sync_gateway.start(arguments)
        job_id = str(started["job_id"])
        try:
            session = self._sessions.create(job_id, platform, BROWSER_PROTOCOL_VERSION)
        except BrowserCaptureError as error:
            # The sync run exists but has no browser session, so nothing will
            # ever drive it. Fail it rather than leaving a phantom running job.
            self._sync.fail_sync(
                job_id,
                platform,
                BROWSER_UNAVAILABLE,
                "unable to prepare browser session",
            )
            raise SyncArgumentError(str(error)) from error

        # Opening the page is what starts the run, because a page load is the
        # only event FavHub can cause that wakes the extension. It happens after
        # the session exists so the browser never arrives before there is
        # anything to claim, and a browser that will not open is reported rather
        # than raised: the session is waiting either way.
        opened = self._open_page(platform)
        return {
            **started,
            "browser_session": session.as_dict(),
            "opened_url": opened,
        }

    def resume(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _required_string(arguments, "jobId")
        platform = _browser_platform(arguments)
        session = self._sessions.find_open(platform)
        if session is None or session.job_id != job_id:
            raise SyncArgumentError(f"no resumable browser session for {platform}")
        self._sync_gateway.resume_run(job_id, platform)
        try:
            refreshed = self._sessions.get(session.id)
            if refreshed.status not in _RESUMABLE:
                raise BrowserCaptureError(SESSION_UNAVAILABLE_MESSAGE)
        except BrowserCaptureError as error:
            # Undo the resume: a running sync run with no claimable session
            # would report progress that nothing is making.
            self._sync.pause_sync(job_id, platform, BROWSER_UNAVAILABLE, str(error))
            raise SyncArgumentError(str(error)) from error
        return {
            "job_id": job_id,
            "platform": platform,
            "browser_session": refreshed.as_dict(),
        }

    def status(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        # Reading status is the natural moment to notice a browser that went
        # away without saying so, so expired leases are swept here.
        self._sessions.recover_expired()
        status = self._sync_gateway.status(arguments)
        job_id = str(status["job_id"])
        sessions = [session.as_dict() for session in self._sessions.for_job(job_id)]
        return {**status, "browser_sessions": sessions}

    def cancel(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        job_id = _required_string(arguments, "jobId")
        platform = _browser_platform(arguments)
        session = self._find_for_job(job_id, platform)
        try:
            cancelled = self._sessions.cancel(session.id)
        except BrowserCaptureError as error:
            raise SyncArgumentError(str(error)) from error
        # Pausing rather than finishing is the point: no frontier moves, so the
        # next run rescans whatever this one did not confirm. A terminal run
        # needs no pause; the session is what mattered here.
        with suppress(ValueError):
            self._sync.pause_sync(job_id, platform, CANCELLED_BY_USER, "cancelled by user")
        return {
            "job_id": job_id,
            "platform": platform,
            "browser_session": cancelled.as_dict(),
        }

    # -- extension-facing operations -----------------------------------------
    #
    # These arrive over the named pipe, so every argument is untrusted. The
    # session id is never taken on faith: the platform's own open session is
    # looked up and the caller must match it.

    def claim_for_extension(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Hand the waiting session to the extension, with where to stop.

        The frontier and scan cap travel with the claim so the adapter can stop
        on its own rather than asking again after every page.
        """
        platform = _browser_platform(arguments)
        extension_version = _required_string(arguments, "extensionVersion")
        # Before the session is touched: Chrome keeps running whatever it loaded
        # until someone clicks Reload, so an upgrade leaves a stale extension
        # collecting with old adapter code and no sign that anything is wrong.
        # A run built by the wrong adapter is worse than no run — its results
        # look exactly like good ones.
        stale = self._version_mismatch(extension_version)
        if stale is not None:
            return {"session": None, "error": stale}
        session = self._sessions.find_open(platform)
        if session is None:
            # Nothing waiting is the normal answer: the extension stays dormant
            # unless the user started a run.
            return {"session": None}
        try:
            claimed = self._sessions.claim(session.id, extension_version, LEASE_SECONDS)
        except BrowserCaptureError as error:
            raise SyncArgumentError(str(error)) from error
        status = self._sync.get_status(claimed.job_id)
        options = status.get("options", {})
        return {
            "session": claimed.as_dict(),
            "mode": status.get("mode"),
            "maxScanItems": options.get("max_scan_items"),
            "frontier": list(self._frontier(claimed.platform, str(status.get("mode", "")))),
            "scopes": [
                {"scopeId": scope["scope_id"], "scopeName": scope["scope_name"]}
                for entry in status.get("platforms", [])
                if entry["platform"] == claimed.platform
                for scope in entry.get("scopes", [])
            ],
        }

    def heartbeat(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Renew the lease so a live run is not swept up as abandoned."""
        session = self._session_for(arguments)
        try:
            if session.status is BrowserCaptureStatus.CAPTURING:
                renewed = self._sessions.renew(session.id, LEASE_SECONDS)
            else:
                # A heartbeat for a session that is not capturing means the
                # browser believes it owns a run FavHub has since paused, so
                # claiming is right: it hands the session back and clears the
                # stale pause reason.
                renewed = self._sessions.claim(
                    session.id, session.extension_version or "unknown", LEASE_SECONDS
                )
        except BrowserCaptureError:
            # A terminal session cannot be renewed or claimed; reporting its
            # real state is what lets the run notice it is over.
            renewed = self._sessions.get(session.id)
        return {"session": renewed.as_dict()}

    def declare_scopes(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Register folders the browser discovered, returning their frontiers."""
        session = self._session_for(arguments)
        raw = arguments.get("scopes")
        if not isinstance(raw, list) or not raw:
            raise SyncArgumentError("scopes must be a non-empty list")
        mapping: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise SyncArgumentError("each scope must be an object")
            scope_id = entry.get("scopeId")
            scope_name = entry.get("scopeName")
            if not isinstance(scope_id, str) or not isinstance(scope_name, str):
                raise SyncArgumentError("scopeId and scopeName must be strings")
            mapping[scope_id] = scope_name
        frontiers = self._sync_gateway.register_browser_scopes(
            session.job_id, session.platform, mapping
        )
        return {"frontiers": {scope: list(ids) for scope, ids in frontiers.items()}}

    def submit_bundle(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Route one batch of raw responses through the existing parsers."""
        session = self._session_for(arguments)
        events = arguments.get("events")
        if not isinstance(events, list) or not events:
            raise SyncArgumentError("events must be a non-empty list")
        if self._ingestor is None:
            raise SyncArgumentError("browser ingest is unavailable")
        results = []
        for event in events:
            if not isinstance(event, Mapping):
                raise SyncArgumentError("each event must be an object")
            try:
                results.append(self._ingestor.handle(session.id, event))
            except BrowserIngestError as error:
                # A platform condition pauses the run; the extension learns the
                # stable code rather than a stack trace.
                self.pause_for_browser(session.job_id, session.platform, error.code, error.message)
                return {"accepted": False, "error": {"code": error.code}}
        return {"accepted": True, "results": results}

    def finish_for_extension(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Flush what is buffered, close the scan, and complete the session."""
        session = self._session_for(arguments)
        if self._ingestor is None:
            raise SyncArgumentError("browser ingest is unavailable")
        frontier_ids = _string_tuple(arguments.get("frontierIds"), "frontierIds")
        try:
            self._ingestor.finish(
                session.id,
                observed_end=bool(arguments.get("observedEnd", False)),
                max_scan_reached=bool(arguments.get("maxScanReached", False)),
                visible_total=None,
                frontier_ids=frontier_ids,
                # Scoped platforms report per folder: a run that finished one
                # folder and was cut short in another must advance only the
                # first, so these cannot collapse into the flat pair above.
                frontier_scopes=_frontier_scopes(arguments.get("frontierScopes")),
                scope_results=_scope_results(arguments.get("scopeResults")),
            )
        except (BrowserIngestError, ValueError, KeyError) as error:
            raise SyncArgumentError(str(error)) from error
        completed = self._sessions.complete(session.id)
        return {
            "browser_session": completed.as_dict(),
            "status": self._sync.get_status(session.job_id),
        }

    def _session_for(self, arguments: Mapping[str, Any]) -> BrowserCaptureSession:
        """Resolve the session by id, but only if it is the platform's open one."""
        session_id = _required_string(arguments, "sessionId")
        try:
            session = self._sessions.get(session_id)
        except BrowserCaptureError as error:
            raise SyncArgumentError(str(error)) from error
        open_session = self._sessions.find_open(session.platform)
        if open_session is None or open_session.id != session.id:
            raise SyncArgumentError(f"session is no longer open: {session_id}")
        return session

    def _version_mismatch(self, reported: str) -> dict[str, str] | None:
        """Describe a stale loaded extension, or None when it is current."""
        try:
            installed = self._installed_version()
        except OSError:
            installed = None
        if installed is None or installed == reported:
            return None
        return {
            "code": EXTENSION_VERSION_MISMATCH,
            # Names both versions and the one action that fixes it: the click
            # is manual by Chrome's design, so the message has to ask for it.
            "message": (
                f"Chrome is running FavHub extension {reported}, but {installed} is "
                "installed. Open chrome://extensions and click Reload on the FavHub "
                "Collector card."
            ),
        }

    def _frontier(self, platform: str, mode: str) -> tuple[str, ...]:
        if mode != "incremental":
            return ()
        row = self._sync.database.connection.execute(
            "SELECT source_ids_json FROM sync_frontiers WHERE platform = ?",
            (platform,),
        ).fetchone()
        if row is None:
            return ()
        parsed = json.loads(str(row["source_ids_json"]))
        return tuple(str(value) for value in parsed) if isinstance(parsed, list) else ()

    def pause_for_browser(
        self,
        job_id: str,
        platform: str,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        """Pause both halves at once when the browser reports a stoppage."""
        session = self._find_for_job(job_id, platform)
        try:
            paused = self._sessions.pause(session.id, code, message)
        except (BrowserCaptureError, ValueError) as error:
            raise SyncArgumentError(str(error)) from error
        self._sync.pause_sync(job_id, platform, code, message)
        return {
            "job_id": job_id,
            "platform": platform,
            "browser_session": paused.as_dict(),
        }

    def _find_for_job(self, job_id: str, platform: str) -> BrowserCaptureSession:
        session = self._sessions.find_for_job(job_id, platform)
        if session is None:
            raise SyncArgumentError(f"no browser session for {platform} on job {job_id}")
        return session


_RESUMABLE = frozenset({"awaiting_browser", "paused"})


def _browser_platform(arguments: Mapping[str, Any]) -> str:
    platform = _required_string(arguments, "platform")
    if platform not in BROWSER_PLATFORMS:
        raise SyncArgumentError(
            f"browser collection supports {', '.join(sorted(BROWSER_PLATFORMS))}"
        )
    return platform


def _frontier_scopes(raw: object) -> dict[str, tuple[str, ...]] | None:
    """Per-folder frontier ids, or None when the platform has no folders."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SyncArgumentError("frontierScopes must be an object")
    return {
        str(scope): _string_tuple(ids, f"frontierScopes[{scope}]") for scope, ids in raw.items()
    }


def _scope_results(raw: object) -> dict[str, ScopeFinish] | None:
    """Per-folder outcomes, so one truncated folder cannot hold back the rest."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SyncArgumentError("scopeResults must be an object")
    results: dict[str, ScopeFinish] = {}
    for scope, entry in raw.items():
        if not isinstance(entry, Mapping):
            raise SyncArgumentError(f"scopeResults[{scope}] must be an object")
        visible = entry.get("visibleTotal", entry.get("visible_total"))
        if visible is not None and (not isinstance(visible, int) or isinstance(visible, bool)):
            raise SyncArgumentError(
                f"scopeResults[{scope}].visibleTotal must be an integer or null"
            )
        results[str(scope)] = ScopeFinish(
            max_scan_reached=bool(
                entry.get("maxScanReached", entry.get("max_scan_reached", False))
            ),
            visible_total=visible,
        )
    return results


def _string_tuple(raw: object, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise SyncArgumentError(f"{field} must be a list of strings")
    return tuple(raw)


def _required_string(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SyncArgumentError(f"{name} must be a non-blank string")
    return value


__all__ = [
    "BROWSER_PLATFORMS",
    "CANCELLED_BY_USER",
    "BrowserGateway",
]
