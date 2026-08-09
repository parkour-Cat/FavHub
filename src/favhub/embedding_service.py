"""Explicit embedding initialization and durable build orchestration."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace

from favhub.database import Database
from favhub.embedding import EmbeddingProfile
from favhub.embedding_indexing import EmbeddingIndexer
from favhub.embedding_profiles import EmbeddingProfileStore, EmbeddingSummary
from favhub.embedding_runtime import (
    EmbeddingDependencyUnavailableError,
    EmbeddingModelCacheMissingError,
    EmbeddingRuntime,
    EmbeddingRuntimeError,
)
from favhub.enrichment_queue import EnrichmentQueue, EnrichmentTask, now
from favhub.indexing import ContentIndexer

DEFAULT_MODEL_LICENSE = "MIT"
EMBEDDING_TASK_BATCH_SIZE = 8


@dataclass(frozen=True, slots=True)
class EmbeddingBuildError:
    task_id: str | None
    kind: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class EmbeddingBuildProgress:
    """One heartbeat from a build, for a caller that wants to show its work.

    A full rebuild of a real library runs for hours on CPU. Reporting only at
    the end makes that indistinguishable from a hang, which is a bad thing to
    hand someone who is waiting and cannot tell whether to keep waiting.
    """

    phase: str
    done: int
    remaining: int
    vectors: int
    elapsed_seconds: float

    @property
    def rate(self) -> float:
        """Items per second so far, or zero before the first one lands."""
        return self.done / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        """Seconds left at the observed rate; None until one is measurable."""
        rate = self.rate
        return self.remaining / rate if rate > 0 else None


ProgressCallback = Callable[[EmbeddingBuildProgress], None]


@dataclass(frozen=True, slots=True)
class EmbeddingBuildReport:
    run_id: str
    profile_id: str
    attempted: int
    processed: int
    skipped: int
    failed: int
    remaining: int
    elapsed_seconds: float
    errors: tuple[EmbeddingBuildError, ...] = ()


class EmbeddingService:
    """Coordinate profile initialization and bounded, restart-safe builds."""

    def __init__(
        self,
        database: Database,
        runtime: EmbeddingRuntime,
        profiles: EmbeddingProfileStore,
        queue: EnrichmentQueue,
        content_indexer: ContentIndexer,
        embedding_indexer: EmbeddingIndexer,
    ) -> None:
        self.database = database
        self.runtime = runtime
        self.profiles = profiles
        self.queue = queue
        self.content_indexer = content_indexer
        self.embedding_indexer = embedding_indexer

    def initialize(self) -> EmbeddingProfile:
        profile = self.runtime.initialize()
        self.profiles.activate(profile)
        # Initialization must make already indexed items visible to the
        # embedding build queue.  The composition root supplies a local-only
        # provider loader, so reconciliation cannot trigger another download.
        self.embedding_indexer.reindex_missing(force=False)
        return profile

    def build(
        self,
        *,
        max_items: int | None,
        force: bool,
        progress: ProgressCallback | None = None,
    ) -> EmbeddingBuildReport:
        if max_items is not None and (
            isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0
        ):
            raise ValueError("max_items must be a positive integer")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        profile = self.profiles.active()
        if profile is None:
            raise EmbeddingRuntimeError("embedding profile is not initialized; run embeddings init")

        run_id = str(uuid.uuid4())
        started_at = now()
        started = time.monotonic()
        attempted_ids: set[str] = set()
        processed = 0
        skipped = 0
        errors: list[EmbeddingBuildError] = []
        indexed_this_build = False
        initial_report = EmbeddingBuildReport(
            run_id=run_id,
            profile_id=profile.id,
            attempted=0,
            processed=0,
            skipped=0,
            failed=0,
            remaining=self._remaining(),
            elapsed_seconds=0.0,
        )
        self._start_run(initial_report, max_items=max_items, started_at=started_at)
        vectors = 0

        def report_progress(phase: str) -> None:
            if progress is None:
                return
            progress(
                EmbeddingBuildProgress(
                    phase=phase,
                    done=len(attempted_ids),
                    remaining=self._remaining(),
                    vectors=vectors,
                    elapsed_seconds=time.monotonic() - started,
                )
            )

        phase = "index"
        try:
            while self._has_capacity(max_items, attempted_ids):
                task = self.queue.claim_next(kind="index_content", excluded_ids=attempted_ids)
                if task is None:
                    break
                attempted_ids.add(task.id)
                indexed_this_build = True
                try:
                    indexed = self.content_indexer.index_task(task)
                except Exception as exc:
                    errors.append(self._task_error(task, exc))
                else:
                    if indexed.chunk_count == 0:
                        skipped += 1
                    else:
                        processed += 1
                report_progress("index")

            if force or indexed_this_build:
                phase = "reconcile"
                report_progress("reconcile")
                self.embedding_indexer.reindex_missing(force=force)

            phase = "embed"
            while self._has_capacity(max_items, attempted_ids):
                batch: list[EnrichmentTask] = []
                while len(batch) < EMBEDDING_TASK_BATCH_SIZE and self._has_capacity(
                    max_items, attempted_ids
                ):
                    task = self.queue.claim_next(kind="embed_content", excluded_ids=attempted_ids)
                    if task is None:
                        break
                    attempted_ids.add(task.id)
                    batch.append(task)
                if not batch:
                    break
                index_tasks = getattr(self.embedding_indexer, "index_tasks", None)
                if callable(index_tasks):
                    try:
                        embedded_tasks = index_tasks(tuple(batch))
                    except Exception as exc:
                        errors.extend(self._task_error(task, exc) for task in batch)
                        continue
                    for embedded in embedded_tasks:
                        vectors += embedded.vector_count
                        if embedded.skipped:
                            skipped += 1
                        else:
                            processed += 1
                    report_progress("embed")
                    continue
                for task in batch:
                    try:
                        embedded = self.embedding_indexer.index_task(task)
                    except Exception as exc:
                        errors.append(self._task_error(task, exc))
                    else:
                        vectors += embedded.vector_count
                        if embedded.skipped:
                            skipped += 1
                        else:
                            processed += 1
                report_progress("embed")
        except Exception as exc:
            errors.append(self._run_error(exc, phase=phase))
            failed_report = self._report(
                run_id=run_id,
                profile_id=profile.id,
                attempted_ids=attempted_ids,
                processed=processed,
                skipped=skipped,
                errors=errors,
                started=started,
            )
            self._finish_run(failed_report, status="failed", finished_at=now())
            raise

        report = self._report(
            run_id=run_id,
            profile_id=profile.id,
            attempted_ids=attempted_ids,
            processed=processed,
            skipped=skipped,
            errors=errors,
            started=started,
        )
        self._finish_run(report, status="completed", finished_at=now())
        return report

    def status(self) -> EmbeddingSummary:
        summary = self.profiles.summary()
        cache_check = getattr(self.runtime, "cache_available", None)
        if summary.active_profile is not None and callable(cache_check) and not cache_check():
            return replace(summary, state="unavailable")
        return summary

    def recover_interrupted_builds(self) -> int:
        error = EmbeddingBuildError(
            task_id=None,
            kind="build",
            code="build_interrupted",
            message="embedding build was interrupted before completion",
        )
        with self.database.transaction():
            cursor = self.database.connection.execute(
                """UPDATE embedding_build_runs
                   SET status='failed', error_json=?, finished_at=?
                   WHERE status='running'""",
                (json.dumps([asdict(error)], ensure_ascii=False, sort_keys=True), now()),
            )
        return cursor.rowcount

    @staticmethod
    def _has_capacity(max_items: int | None, attempted_ids: set[str]) -> bool:
        return max_items is None or len(attempted_ids) < max_items

    @staticmethod
    def _task_error(task: EnrichmentTask, error: Exception) -> EmbeddingBuildError:
        message = str(error).strip() or type(error).__name__
        return EmbeddingBuildError(
            task_id=task.id,
            kind=task.kind,
            code=f"{task.kind}_failed",
            message=message,
        )

    @staticmethod
    def _run_error(error: Exception, *, phase: str) -> EmbeddingBuildError:
        if isinstance(error, EmbeddingDependencyUnavailableError):
            code = "embedding_dependency_unavailable"
        elif isinstance(error, EmbeddingModelCacheMissingError):
            code = "embedding_cache_unavailable"
        elif phase == "reconcile":
            code = "reconcile_failed"
        else:
            code = "build_failed"
        return EmbeddingBuildError(
            task_id=None,
            kind="build",
            code=code,
            message=str(error).strip() or type(error).__name__,
        )

    def _report(
        self,
        *,
        run_id: str,
        profile_id: str,
        attempted_ids: set[str],
        processed: int,
        skipped: int,
        errors: list[EmbeddingBuildError],
        started: float,
    ) -> EmbeddingBuildReport:
        return EmbeddingBuildReport(
            run_id=run_id,
            profile_id=profile_id,
            attempted=len(attempted_ids),
            processed=processed,
            skipped=skipped,
            failed=len(errors),
            remaining=self._remaining(),
            elapsed_seconds=time.monotonic() - started,
            errors=tuple(errors),
        )

    def _remaining(self) -> int:
        row = self.database.connection.execute(
            """SELECT COUNT(*) FROM enrichment_tasks
               WHERE kind IN ('index_content', 'embed_content')
                 AND status IN ('pending', 'running')"""
        ).fetchone()
        return int(row[0])

    def _start_run(
        self,
        report: EmbeddingBuildReport,
        *,
        max_items: int | None,
        started_at: str,
    ) -> None:
        payload = asdict(report)
        payload.pop("errors")
        with self.database.transaction():
            self.database.connection.execute(
                """INSERT INTO embedding_build_runs(
                       id, profile_id, status, max_items, counts_json,
                       error_json, started_at, finished_at
                   ) VALUES (?, ?, 'running', ?, ?, NULL, ?, NULL)""",
                (
                    report.run_id,
                    report.profile_id,
                    max_items,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    started_at,
                ),
            )

    def _finish_run(
        self,
        report: EmbeddingBuildReport,
        *,
        status: str,
        finished_at: str,
    ) -> None:
        payload = asdict(report)
        errors = payload.pop("errors")
        with self.database.transaction():
            cursor = self.database.connection.execute(
                """UPDATE embedding_build_runs
                   SET status=?, counts_json=?, error_json=?, finished_at=?
                   WHERE id=? AND status='running'""",
                (
                    status,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(errors, ensure_ascii=False, sort_keys=True) if errors else None,
                    finished_at,
                    report.run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("embedding build run is not running")


__all__ = [
    "DEFAULT_MODEL_LICENSE",
    "EmbeddingBuildError",
    "EmbeddingBuildReport",
    "EmbeddingService",
]
