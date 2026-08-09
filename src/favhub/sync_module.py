import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from favhub.database import Database
from favhub.domain import (
    SCOPED_PLATFORMS,
    SUPPORTED_PLATFORMS,
    CapturedItem,
    CaptureStatus,
    SyncMode,
    isoformat,
)
from favhub.library import BatchReceipt, LibraryModule
from favhub.retrieval import summarize_index


@dataclass(frozen=True, slots=True)
class StartSyncRequest:
    platforms: tuple[str, ...]
    mode: SyncMode
    published_since: datetime | None
    published_until: datetime | None
    max_scan_items: int | None
    scope_ids: tuple[str, ...] | None = None
    scope_names: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.platforms:
            raise ValueError("platforms must not be empty")
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("platforms must not contain duplicates")
        unsupported = [
            platform for platform in self.platforms if platform not in SUPPORTED_PLATFORMS
        ]
        if unsupported:
            raise ValueError(f"unsupported platform: {unsupported[0]}")
        if not isinstance(self.mode, SyncMode):
            raise ValueError("mode must be a SyncMode")
        for field_name, value in (
            ("published_since", self.published_since),
            ("published_until", self.published_until),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field_name} must be timezone-aware")
        if (
            self.published_since is not None
            and self.published_until is not None
            and self.published_since > self.published_until
        ):
            raise ValueError("published_since must not be after published_until")
        if self.max_scan_items is not None and (
            isinstance(self.max_scan_items, bool)
            or not isinstance(self.max_scan_items, int)
            or self.max_scan_items < 1
        ):
            raise ValueError("max_scan_items must be an integer of at least 1")
        if self.scope_ids is not None:
            if not isinstance(self.scope_ids, tuple) or not self.scope_ids:
                raise ValueError("scope_ids must be a non-empty tuple when provided")
            if any(
                not isinstance(scope_id, str) or not scope_id.strip() for scope_id in self.scope_ids
            ):
                raise ValueError("scope_ids must contain non-blank strings")
            if len(set(self.scope_ids)) != len(self.scope_ids):
                raise ValueError("scope_ids must not contain duplicates")
            if len(self.platforms) != 1:
                raise ValueError("scope_ids require exactly one platform")
        if self.scope_names is not None:
            if not isinstance(self.scope_names, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                for key, value in self.scope_names.items()
            ):
                raise ValueError("scope_names must map scope ids to non-blank names")
            if self.scope_ids is None or not set(self.scope_names).issubset(self.scope_ids):
                raise ValueError("scope_names keys must be a subset of scope_ids")


@dataclass(frozen=True, slots=True)
class StartSyncResult:
    job_id: str
    frontiers: dict[str, tuple[str, ...]]
    scoped_frontiers: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScopeFinish:
    """Per-folder scan outcome reported at finish time."""

    max_scan_reached: bool
    visible_total: int | None

    def __post_init__(self) -> None:
        if type(self.max_scan_reached) is not bool:
            raise ValueError("scope max_scan_reached must be a boolean")
        if self.visible_total is not None and (
            type(self.visible_total) is not int or self.visible_total < 0
        ):
            raise ValueError("scope visible_total must be a non-negative integer or null")


@dataclass(frozen=True, slots=True)
class SubmitBatchReceipt:
    receipt_id: str
    added: int
    refreshed: int
    duplicates: int
    out_of_range: int


class SyncModule:
    def __init__(self, database: Database, library: LibraryModule) -> None:
        self.database = database
        self.library = library

    def start_sync(self, request: StartSyncRequest) -> StartSyncResult:
        # Re-run validation for callers that bypass normal dataclass construction.
        request.__post_init__()
        job_id = str(uuid.uuid4())
        timestamp = _now()
        options = {
            "platforms": list(request.platforms),
            "published_since": _timestamp_or_none(request.published_since),
            "published_until": _timestamp_or_none(request.published_until),
            "max_scan_items": request.max_scan_items,
        }
        frontiers: dict[str, tuple[str, ...]] = {}
        scoped_frontiers: dict[str, tuple[str, ...]] = {}
        with self.database.transaction():
            self.database.connection.execute(
                """
                INSERT INTO sync_jobs (
                    id, mode, status, options_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.mode.value,
                    CaptureStatus.RUNNING.value,
                    json.dumps(options, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )
            for platform in request.platforms:
                self.database.connection.execute(
                    """
                    INSERT INTO sync_platform_runs (
                        job_id, platform, status, counts_json, error_json
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        job_id,
                        platform,
                        CaptureStatus.RUNNING.value,
                        json.dumps(_zero_counts(), sort_keys=True),
                    ),
                )
                if request.mode is SyncMode.INCREMENTAL:
                    row = self.database.connection.execute(
                        "SELECT source_ids_json FROM sync_frontiers WHERE platform = ?",
                        (platform,),
                    ).fetchone()
                    frontiers[platform] = _frontier_from_row(row)
                else:
                    frontiers[platform] = ()
            if request.scope_ids is not None:
                scope_platform = request.platforms[0]
                scope_names = request.scope_names or {}
                for scope_id in request.scope_ids:
                    self.database.connection.execute(
                        """
                        INSERT INTO sync_scope_runs (
                            job_id, platform, scope_id, scope_name, status,
                            counts_json, error_json
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            job_id,
                            scope_platform,
                            scope_id,
                            scope_names.get(scope_id, scope_id),
                            CaptureStatus.RUNNING.value,
                            json.dumps(_zero_scope_counts(), sort_keys=True),
                        ),
                    )
                    if request.mode is SyncMode.INCREMENTAL:
                        scope_row = self.database.connection.execute(
                            """
                            SELECT source_ids_json FROM sync_frontier_scopes
                            WHERE platform = ? AND scope_id = ?
                            """,
                            (scope_platform, scope_id),
                        ).fetchone()
                        scoped_frontiers[scope_id] = _frontier_from_row(scope_row)
                    else:
                        scoped_frontiers[scope_id] = ()
        return StartSyncResult(
            job_id=job_id, frontiers=frontiers, scoped_frontiers=scoped_frontiers
        )

    def submit_batch(
        self,
        job_id: str,
        platform: str,
        idempotency_key: str,
        items: list[CapturedItem],
        scope_scans: Mapping[str, tuple[str, ...]] | None = None,
    ) -> SubmitBatchReceipt:
        with self.database.transaction():
            job, run = self._job_and_run(job_id, platform)
            for item in items:
                if item.platform != platform:
                    raise ValueError(
                        "captured item platform does not match batch platform: "
                        f"{item.platform!r} != {platform!r}"
                    )
            if scope_scans:
                self._validate_scopes_belong(job_id, platform, scope_scans)
            fingerprint = _batch_fingerprint(items, scope_scans)
            persisted = self.database.connection.execute(
                """
                SELECT receipt_json
                FROM sync_batches
                WHERE job_id = ? AND platform = ? AND idempotency_key = ?
                """,
                (job_id, platform, idempotency_key),
            ).fetchone()
            persisted_payload = _json_object(str(persisted["receipt_json"])) if persisted else None
            metadata = persisted_payload.get("_sync") if persisted_payload else None
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("persisted sync metadata must be a JSON object")
            if isinstance(metadata, dict):
                persisted_fingerprint = metadata.get("request_fingerprint")
                if persisted is not None and (
                    not isinstance(persisted_fingerprint, str) or not persisted_fingerprint
                ):
                    raise ValueError("persisted sync receipt is missing its payload fingerprint")
                if persisted_fingerprint is not None and persisted_fingerprint != fingerprint:
                    raise ValueError("idempotency key reused with a different batch payload")
                if metadata.get("counts_applied") is True:
                    if persisted_payload is None:
                        raise RuntimeError("missing persisted receipt payload")
                    return _submit_receipt(persisted_payload)
            elif persisted is not None:
                raise ValueError("persisted sync receipt is missing its sync metadata")

            recovery = persisted_payload is not None
            run_status = str(run["status"])
            if run_status in _TERMINAL_STATUSES:
                if not recovery:
                    raise ValueError(
                        f"sync platform run {platform!r} is terminal (status: {run_status})"
                    )
            elif run_status not in {
                CaptureStatus.RUNNING.value,
                CaptureStatus.PAUSED.value,
            }:
                raise ValueError(
                    f"sync platform run {platform!r} is not accepting batches "
                    f"(status: {run_status})"
                )

            options = _json_object(str(job["options_json"]))
            since = _parse_timestamp_or_none(options.get("published_since"))
            until = _parse_timestamp_or_none(options.get("published_until"))
            current_run = self.database.connection.execute(
                """
                SELECT counts_json
                FROM sync_platform_runs
                WHERE job_id = ? AND platform = ?
                """,
                (job_id, platform),
            ).fetchone()
            if current_run is None:
                raise KeyError(f"unknown platform for sync job: {platform}")
            counts = _counts_from_json(str(current_run["counts_json"]))
            max_scan_items = options.get("max_scan_items")
            if max_scan_items is not None and (
                type(max_scan_items) is not int or max_scan_items < 1
            ):
                raise ValueError("invalid max_scan_items option")
            batch_items = items
            truncated = False
            if max_scan_items is not None:
                remaining = max_scan_items - counts["scanned"]
                if remaining <= 0:
                    batch_items = []
                    truncated = bool(items)
                elif len(items) > remaining:
                    batch_items = items[:remaining]
                    truncated = True

            accepted: list[CapturedItem] = []
            out_of_range = 0
            for item in batch_items:
                if (since is not None and item.published_at < since) or (
                    until is not None and item.published_at > until
                ):
                    out_of_range += 1
                else:
                    accepted.append(item)

            if recovery:
                if persisted_payload is None:
                    raise RuntimeError("missing persisted receipt for recovery")
                library_receipt: BatchReceipt | SubmitBatchReceipt = _submit_receipt(
                    persisted_payload
                )
                if isinstance(metadata, dict) and "scanned" in metadata:
                    persisted_scanned = metadata["scanned"]
                    if type(persisted_scanned) is not int or persisted_scanned < 0:
                        raise ValueError("persisted sync metadata has invalid scanned")
                    scanned = persisted_scanned
                    persisted_out_of_range = persisted_payload.get("out_of_range")
                    if type(persisted_out_of_range) is int and persisted_out_of_range >= 0:
                        out_of_range = persisted_out_of_range
                    persisted_truncated = metadata.get("truncated", False)
                    if type(persisted_truncated) is not bool:
                        raise ValueError("persisted sync metadata has invalid truncated")
                    truncated = persisted_truncated
                else:
                    scanned = len(batch_items)
            else:
                library_receipt = self.library.ingest_batch(
                    job_id,
                    platform,
                    idempotency_key,
                    accepted,
                    str(job["mode"]) == SyncMode.FULL.value,
                )
                scanned = len(batch_items)

            receipt = SubmitBatchReceipt(
                receipt_id=library_receipt.receipt_id,
                added=library_receipt.added,
                refreshed=library_receipt.refreshed,
                duplicates=library_receipt.duplicates,
                out_of_range=out_of_range,
            )
            counts["scanned"] += scanned
            counts["added"] += receipt.added
            counts["refreshed"] += receipt.refreshed
            counts["duplicates"] += receipt.duplicates
            counts["out_of_range"] += receipt.out_of_range
            self.database.connection.execute(
                """
                UPDATE sync_platform_runs
                SET counts_json = ?,
                    max_scan_reached = CASE WHEN ? THEN 1 ELSE max_scan_reached END
                WHERE job_id = ? AND platform = ?
                """,
                (
                    json.dumps(counts, sort_keys=True),
                    int(truncated),
                    job_id,
                    platform,
                ),
            )
            if scope_scans:
                self._apply_scope_scans(job_id, platform, scope_scans)
            receipt_payload = asdict(receipt)
            receipt_payload["_sync"] = {
                "counts_applied": True,
                "out_of_range": out_of_range,
                "request_fingerprint": fingerprint,
                "scanned": scanned,
                "truncated": truncated,
            }
            self.database.connection.execute(
                """
                UPDATE sync_batches
                SET receipt_json = ?
                WHERE job_id = ? AND platform = ? AND idempotency_key = ?
                """,
                (
                    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True),
                    job_id,
                    platform,
                    idempotency_key,
                ),
            )
            self.database.connection.execute(
                "UPDATE sync_jobs SET updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )
            return receipt

    def pause_sync(self, job_id: str, platform: str, code: str, message: str) -> None:
        with self.database.transaction():
            _, run = self._job_and_run(job_id, platform)
            if str(run["status"]) in _TERMINAL_STATUSES:
                raise ValueError(f"cannot pause terminal platform run: {platform}")
            timestamp = _now()
            self.database.connection.execute(
                """
                UPDATE sync_platform_runs
                SET status = ?, error_json = ?
                WHERE job_id = ? AND platform = ?
                """,
                (
                    CaptureStatus.PAUSED.value,
                    json.dumps({"code": code, "message": message}, sort_keys=True),
                    job_id,
                    platform,
                ),
            )
            self._update_job_status(job_id, timestamp)

    def resume_sync(self, job_id: str, platform: str) -> None:
        """Lift a pause so a browser session can continue the same run.

        Counts, scopes, and frontiers are untouched: resuming says the browser
        is back, not that anything already scanned should be rescanned.
        """
        with self.database.transaction():
            _, run = self._job_and_run(job_id, platform)
            status = str(run["status"])
            if status == CaptureStatus.RUNNING.value:
                return
            if status != CaptureStatus.PAUSED.value:
                raise ValueError(f"cannot resume platform run in status: {status}")
            timestamp = _now()
            self.database.connection.execute(
                """
                UPDATE sync_platform_runs
                SET status = ?, error_json = NULL
                WHERE job_id = ? AND platform = ?
                """,
                (CaptureStatus.RUNNING.value, job_id, platform),
            )
            self._update_job_status(job_id, timestamp)

    def register_scopes(
        self,
        job_id: str,
        platform: str,
        scopes: Mapping[str, str],
    ) -> dict[str, tuple[str, ...]]:
        """Attach folders the browser discovered to a run started without them.

        Returns each scope's incremental frontier, so the caller learns where to
        stop in the same call that declares the folders.
        """
        if platform not in SCOPED_PLATFORMS:
            raise ValueError(f"platform does not support scopes: {platform}")
        if not scopes:
            raise ValueError("scopes must not be empty")
        for scope_id, scope_name in scopes.items():
            if not isinstance(scope_id, str) or not scope_id.strip():
                raise ValueError("scope ids must be non-blank strings")
            if not isinstance(scope_name, str) or not scope_name.strip():
                raise ValueError("scope names must be non-blank strings")
        with self.database.transaction():
            job, run = self._job_and_run(job_id, platform)
            known = {
                str(row["scope_id"]): str(row["scope_name"])
                for row in self.database.connection.execute(
                    "SELECT scope_id, scope_name FROM sync_scope_runs "
                    "WHERE job_id = ? AND platform = ?",
                    (job_id, platform),
                ).fetchall()
            }
            dropped = set(known) - set(scopes)
            if dropped:
                raise ValueError(f"cannot drop a registered scope: {sorted(dropped)[0]}")
            for scope_id, scope_name in known.items():
                if scopes[scope_id] != scope_name:
                    raise ValueError(f"cannot rename a registered scope: {scope_id}")
            added = sorted(set(scopes) - set(known))
            if added and _counts_from_json(str(run["counts_json"]))["scanned"] > 0:
                raise ValueError("cannot register new scopes after scanning started")
            mode = SyncMode(str(job["mode"]))
            for scope_id in added:
                self.database.connection.execute(
                    """
                    INSERT INTO sync_scope_runs (
                        job_id, platform, scope_id, scope_name, status,
                        counts_json, error_json
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        job_id,
                        platform,
                        scope_id,
                        scopes[scope_id],
                        CaptureStatus.RUNNING.value,
                        json.dumps(_zero_scope_counts(), sort_keys=True),
                    ),
                )
            frontiers: dict[str, tuple[str, ...]] = {}
            for scope_id in sorted(scopes):
                if mode is not SyncMode.INCREMENTAL:
                    frontiers[scope_id] = ()
                    continue
                row = self.database.connection.execute(
                    """
                    SELECT source_ids_json FROM sync_frontier_scopes
                    WHERE platform = ? AND scope_id = ?
                    """,
                    (platform, scope_id),
                ).fetchone()
                frontiers[scope_id] = _frontier_from_row(row)
        return frontiers

    def fail_sync(self, job_id: str, platform: str, code: str, message: str) -> None:
        if (
            not isinstance(code, str)
            or not code.strip()
            or not isinstance(message, str)
            or not message.strip()
        ):
            raise ValueError("failure code and message must not be blank")
        error = {"code": code, "message": message}
        with self.database.transaction():
            _, run = self._job_and_run(job_id, platform)
            current_status = str(run["status"])
            if current_status == CaptureStatus.FAILED.value:
                persisted_error = run["error_json"]
                if persisted_error is not None and _json_object(str(persisted_error)) == error:
                    return
                raise ValueError(f"cannot change failed platform run: {platform}")
            if current_status in _TERMINAL_STATUSES:
                raise ValueError(f"cannot fail terminal platform run: {platform}")
            timestamp = _now()
            self.database.connection.execute(
                """
                UPDATE sync_platform_runs
                SET status = ?, error_json = ?
                WHERE job_id = ? AND platform = ?
                """,
                (
                    CaptureStatus.FAILED.value,
                    json.dumps(error, sort_keys=True),
                    job_id,
                    platform,
                ),
            )
            self._update_job_status(job_id, timestamp)

    def finish_scan(
        self,
        job_id: str,
        platform: str,
        *,
        observed_end: bool,
        max_scan_reached: bool,
        visible_total: int | None,
        frontier_ids: tuple[str, ...],
        frontier_scopes: Mapping[str, tuple[str, ...]] | None = None,
        scope_results: Mapping[str, ScopeFinish] | None = None,
    ) -> None:
        if type(observed_end) is not bool or type(max_scan_reached) is not bool:
            raise ValueError("observed_end and max_scan_reached must be booleans")
        if visible_total is not None and (type(visible_total) is not int or visible_total < 0):
            raise ValueError("visible_total must be a non-negative integer or null")
        if not all(isinstance(source_id, str) for source_id in frontier_ids):
            raise ValueError("frontier_ids must contain only strings")
        if frontier_scopes is not None:
            for scope_frontier in frontier_scopes.values():
                if not all(isinstance(source_id, str) for source_id in scope_frontier):
                    raise ValueError("frontier_scopes values must contain only strings")
        if scope_results is not None:
            for scope_id, result in scope_results.items():
                if not isinstance(result, ScopeFinish):
                    raise ValueError("scope_results values must be ScopeFinish")
                # A folder that hit its scan cap did not reach its observable
                # end, so its frontier must not advance in the same finish.
                if (
                    result.max_scan_reached
                    and frontier_scopes is not None
                    and scope_id in frontier_scopes
                ):
                    raise ValueError(
                        "scope cannot both advance its frontier and report "
                        f"max_scan_reached: {scope_id}"
                    )
        with self.database.transaction():
            job, run = self._job_and_run(job_id, platform)
            current_status = str(run["status"])
            if current_status in _TERMINAL_STATUSES:
                return
            effective_max_scan_reached = max_scan_reached or bool(run["max_scan_reached"])
            effective_observed_end = observed_end and not effective_max_scan_reached
            run_status = (
                CaptureStatus.COMPLETED.value
                if effective_observed_end
                else CaptureStatus.PARTIAL.value
            )
            timestamp = _now()
            self.database.connection.execute(
                """
                UPDATE sync_platform_runs
                SET status = ?, error_json = NULL, observed_end = ?,
                    max_scan_reached = ?, visible_total = ?
                WHERE job_id = ? AND platform = ?
                """,
                (
                    run_status,
                    int(effective_observed_end),
                    int(effective_max_scan_reached),
                    visible_total,
                    job_id,
                    platform,
                ),
            )
            if effective_observed_end:
                previous_frontier = self.database.connection.execute(
                    "SELECT source_ids_json FROM sync_frontiers WHERE platform = ?",
                    (platform,),
                ).fetchone()
                old_frontier = (
                    _frontier_from_row(previous_frontier)
                    if str(job["mode"]) == SyncMode.INCREMENTAL.value
                    else ()
                )
                persisted_frontier = _bounded_frontier(frontier_ids, old_frontier)
                self.database.connection.execute(
                    """
                    INSERT INTO sync_frontiers(platform, source_ids_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(platform) DO UPDATE SET
                        source_ids_json = excluded.source_ids_json,
                        updated_at = excluded.updated_at
                    """,
                    (platform, json.dumps(list(persisted_frontier)), timestamp),
                )
            if scope_results is not None:
                self._apply_scope_results(job_id, platform, scope_results)
            if frontier_scopes is not None:
                self._finish_scopes(job, platform, frontier_scopes, timestamp)
            self._update_job_status(job_id, timestamp)

    def get_status(self, job_id: str) -> dict[str, Any]:
        job = self.database.connection.execute(
            """
            SELECT id, mode, status, options_json, created_at, updated_at,
                   capture_finished_at
            FROM sync_jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if job is None:
            raise KeyError(f"unknown sync job: {job_id}")
        platform_rows = self.database.connection.execute(
            """
            SELECT platform, status, counts_json, error_json, observed_end,
                   max_scan_reached, visible_total
            FROM sync_platform_runs
            WHERE job_id = ? ORDER BY platform
            """,
            (job_id,),
        ).fetchall()
        platforms: list[dict[str, Any]] = []
        for row in platform_rows:
            error = row["error_json"]
            platform_name = str(row["platform"])
            platforms.append(
                {
                    "platform": platform_name,
                    "status": str(row["status"]),
                    "counts": _counts_from_json(str(row["counts_json"])),
                    "error": _json_object(str(error)) if error is not None else None,
                    "observed_end": bool(row["observed_end"]),
                    "max_scan_reached": bool(row["max_scan_reached"]),
                    "visible_total": row["visible_total"],
                    "scopes": self._scope_status(job_id, platform_name),
                }
            )
        pending = self.database.connection.execute(
            "SELECT COUNT(*) AS count FROM enrichment_tasks WHERE status != 'completed'"
        ).fetchone()
        index_summary = summarize_index(self.database, self.library.store).as_dict()
        return {
            "job_id": str(job["id"]),
            "capture_status": str(job["status"]),
            "mode": str(job["mode"]),
            "options": _json_object(str(job["options_json"])),
            "created_at": str(job["created_at"]),
            "updated_at": str(job["updated_at"]),
            "capture_finished_at": job["capture_finished_at"],
            "platforms": platforms,
            # This value is global because enrichment_tasks have no job_id.
            "enrichment_pending": int(pending["count"]),
            "index_summary": index_summary,
        }

    def _job_and_run(self, job_id: str, platform: str) -> tuple[Any, Any]:
        job = self.database.connection.execute(
            "SELECT id, mode, status, options_json FROM sync_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise KeyError(f"unknown sync job: {job_id}")
        run = self.database.connection.execute(
            """
            SELECT status, counts_json, error_json, max_scan_reached
            FROM sync_platform_runs
            WHERE job_id = ? AND platform = ?
            """,
            (job_id, platform),
        ).fetchone()
        if run is None:
            raise KeyError(f"unknown platform for sync job: {platform}")
        return job, run

    def _validate_scopes_belong(
        self, job_id: str, platform: str, scope_scans: Mapping[str, tuple[str, ...]]
    ) -> None:
        known = {
            str(row["scope_id"])
            for row in self.database.connection.execute(
                "SELECT scope_id FROM sync_scope_runs WHERE job_id = ? AND platform = ?",
                (job_id, platform),
            ).fetchall()
        }
        for scope_id, source_ids in scope_scans.items():
            if scope_id not in known:
                raise KeyError(f"unknown scope for sync job: {scope_id}")
            if not all(isinstance(source_id, str) for source_id in source_ids):
                raise ValueError("scope_scans values must contain only strings")

    def _apply_scope_scans(
        self, job_id: str, platform: str, scope_scans: Mapping[str, tuple[str, ...]]
    ) -> None:
        for scope_id, source_ids in scope_scans.items():
            row = self.database.connection.execute(
                """
                SELECT counts_json FROM sync_scope_runs
                WHERE job_id = ? AND platform = ? AND scope_id = ?
                """,
                (job_id, platform, scope_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown scope for sync job: {scope_id}")
            counts = _scope_counts_from_json(str(row["counts_json"]))
            counts["scanned"] += len(source_ids)
            self.database.connection.execute(
                """
                UPDATE sync_scope_runs SET counts_json = ?
                WHERE job_id = ? AND platform = ? AND scope_id = ?
                """,
                (json.dumps(counts, sort_keys=True), job_id, platform, scope_id),
            )

    def _apply_scope_results(
        self, job_id: str, platform: str, scope_results: Mapping[str, ScopeFinish]
    ) -> None:
        known = {
            str(row["scope_id"])
            for row in self.database.connection.execute(
                "SELECT scope_id FROM sync_scope_runs WHERE job_id = ? AND platform = ?",
                (job_id, platform),
            ).fetchall()
        }
        for scope_id, result in scope_results.items():
            if scope_id not in known:
                raise KeyError(f"unknown scope for sync job: {scope_id}")
            self.database.connection.execute(
                """
                UPDATE sync_scope_runs
                SET max_scan_reached = ?, visible_total = ?
                WHERE job_id = ? AND platform = ? AND scope_id = ?
                """,
                (
                    int(result.max_scan_reached),
                    result.visible_total,
                    job_id,
                    platform,
                    scope_id,
                ),
            )

    def _finish_scopes(
        self,
        job: Any,
        platform: str,
        frontier_scopes: Mapping[str, tuple[str, ...]],
        timestamp: str,
    ) -> None:
        job_id = str(job["id"])
        incremental = str(job["mode"]) == SyncMode.INCREMENTAL.value
        existing = {
            str(row["scope_id"]): str(row["status"])
            for row in self.database.connection.execute(
                "SELECT scope_id, status FROM sync_scope_runs WHERE job_id = ? AND platform = ?",
                (job_id, platform),
            ).fetchall()
        }
        for scope_id in frontier_scopes:
            if scope_id not in existing:
                raise KeyError(f"unknown scope for sync job: {scope_id}")
        for scope_id, status in existing.items():
            if status in _TERMINAL_STATUSES:
                continue
            if scope_id in frontier_scopes:
                old_frontier: tuple[str, ...] = ()
                if incremental:
                    previous = self.database.connection.execute(
                        """
                        SELECT source_ids_json FROM sync_frontier_scopes
                        WHERE platform = ? AND scope_id = ?
                        """,
                        (platform, scope_id),
                    ).fetchone()
                    old_frontier = _frontier_from_row(previous)
                bounded = _bounded_frontier(tuple(frontier_scopes[scope_id]), old_frontier)
                self.database.connection.execute(
                    """
                    UPDATE sync_scope_runs
                    SET status = ?, observed_end = 1, error_json = NULL
                    WHERE job_id = ? AND platform = ? AND scope_id = ?
                    """,
                    (CaptureStatus.COMPLETED.value, job_id, platform, scope_id),
                )
                self.database.connection.execute(
                    """
                    INSERT INTO sync_frontier_scopes(
                        platform, scope_id, source_ids_json, updated_at
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(platform, scope_id) DO UPDATE SET
                        source_ids_json = excluded.source_ids_json,
                        updated_at = excluded.updated_at
                    """,
                    (platform, scope_id, json.dumps(list(bounded)), timestamp),
                )
            else:
                self.database.connection.execute(
                    """
                    UPDATE sync_scope_runs SET status = ?
                    WHERE job_id = ? AND platform = ? AND scope_id = ?
                    """,
                    (CaptureStatus.PARTIAL.value, job_id, platform, scope_id),
                )

    def _scope_status(self, job_id: str, platform: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            """
            SELECT scope_id, scope_name, status, counts_json, error_json,
                   observed_end, max_scan_reached, visible_total
            FROM sync_scope_runs
            WHERE job_id = ? AND platform = ?
            ORDER BY scope_id
            """,
            (job_id, platform),
        ).fetchall()
        scopes: list[dict[str, Any]] = []
        for row in rows:
            error = row["error_json"]
            scopes.append(
                {
                    "scope_id": str(row["scope_id"]),
                    "scope_name": str(row["scope_name"]),
                    "status": str(row["status"]),
                    "counts": _scope_counts_from_json(str(row["counts_json"])),
                    "error": _json_object(str(error)) if error is not None else None,
                    "observed_end": bool(row["observed_end"]),
                    "max_scan_reached": bool(row["max_scan_reached"]),
                    "visible_total": row["visible_total"],
                }
            )
        return scopes

    def _update_job_status(self, job_id: str, timestamp: str) -> None:
        statuses = [
            str(result["status"])
            for result in self.database.connection.execute(
                "SELECT status FROM sync_platform_runs WHERE job_id = ?",
                (job_id,),
            ).fetchall()
        ]
        all_terminal = bool(statuses) and all(status in _TERMINAL_STATUSES for status in statuses)
        if CaptureStatus.FAILED.value in statuses:
            job_status = CaptureStatus.FAILED.value
        elif all_terminal:
            job_status = (
                CaptureStatus.PARTIAL.value
                if CaptureStatus.PARTIAL.value in statuses
                else CaptureStatus.COMPLETED.value
            )
        elif CaptureStatus.PAUSED.value in statuses:
            job_status = CaptureStatus.PAUSED.value
        else:
            job_status = CaptureStatus.RUNNING.value
        if all_terminal:
            self.database.connection.execute(
                """
                UPDATE sync_jobs
                SET status = ?, updated_at = ?,
                    capture_finished_at = COALESCE(capture_finished_at, ?)
                WHERE id = ?
                """,
                (job_status, timestamp, timestamp, job_id),
            )
        else:
            self.database.connection.execute(
                "UPDATE sync_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (job_status, timestamp, job_id),
            )


_TERMINAL_STATUSES = frozenset(
    {
        CaptureStatus.COMPLETED.value,
        CaptureStatus.PARTIAL.value,
        CaptureStatus.FAILED.value,
    }
)
_FRONTIER_LIMIT = 20


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _batch_fingerprint(
    items: list[CapturedItem],
    scope_scans: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    items_payload = [
        {
            "author": item.author,
            "body": item.body,
            "canonical_url": item.canonical_url,
            "collections": list(item.collections),
            "extractor_version": item.extractor_version,
            "observed_at": isoformat(item.observed_at),
            "platform": item.platform,
            "published_at": isoformat(item.published_at),
            "source_id": item.source_id,
            "title": item.title,
        }
        for item in items
    ]
    payload: Any
    if scope_scans is None:
        # Legacy platform-level batches keep a byte-identical fingerprint.
        payload = items_payload
    else:
        payload = {
            "items": items_payload,
            "scope_scans": {
                scope_id: sorted(source_ids) for scope_id, source_ids in sorted(scope_scans.items())
            },
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp_or_none(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp option must be a string or null")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp option must be timezone-aware")
    return parsed


def _zero_counts() -> dict[str, int]:
    return {
        "scanned": 0,
        "added": 0,
        "refreshed": 0,
        "duplicates": 0,
        "out_of_range": 0,
    }


def _zero_scope_counts() -> dict[str, int]:
    return {"scanned": 0}


def _scope_counts_from_json(value: str) -> dict[str, int]:
    payload = _json_object(value)
    counts = _zero_scope_counts()
    for key in counts:
        raw = payload.get(key, 0)
        if type(raw) is not int or raw < 0:
            raise ValueError(f"invalid scope count for {key}")
        counts[key] = raw
    return counts


def _counts_from_json(value: str) -> dict[str, int]:
    payload = _json_object(value)
    counts = _zero_counts()
    for key in counts:
        raw = payload.get(key, 0)
        if type(raw) is not int or raw < 0:
            raise ValueError(f"invalid count for {key}")
        counts[key] = raw
    return counts


def _json_object(value: str) -> dict[str, Any]:
    payload = json.loads(value)
    if not isinstance(payload, dict):
        raise ValueError("expected JSON object")
    return payload


def _submit_receipt(payload: dict[str, Any]) -> SubmitBatchReceipt:
    return SubmitBatchReceipt(
        receipt_id=str(payload["receipt_id"]),
        added=int(payload["added"]),
        refreshed=int(payload["refreshed"]),
        duplicates=int(payload["duplicates"]),
        out_of_range=int(payload.get("out_of_range", 0)),
    )


def _frontier_from_row(row: Any) -> tuple[str, ...]:
    if row is None:
        return ()
    payload = json.loads(str(row["source_ids_json"]))
    if not isinstance(payload, list) or not all(isinstance(value, str) for value in payload):
        raise ValueError("frontier source_ids_json must be a list of strings")
    return _bounded_frontier(tuple(payload))


def _bounded_frontier(*groups: tuple[str, ...]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for source_id in group:
            if source_id not in seen:
                seen.add(source_id)
                result.append(source_id)
                if len(result) == _FRONTIER_LIMIT:
                    return tuple(result)
    return tuple(result)
