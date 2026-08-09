import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from favhub.database import Database


def now() -> str:
    """Return the current UTC timestamp in the repository's ISO-8601 form."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EnrichmentTask:
    id: str
    platform: str
    source_id: str
    kind: str
    input_hash: str
    attempts: int


class EnrichmentQueue:
    def __init__(self, database: Database) -> None:
        self.database = database

    def enqueue(
        self,
        platform: str,
        source_id: str,
        kind: str,
        input_hash: str,
    ) -> str:
        task_id = str(uuid.uuid4())
        timestamp = now()
        try:
            self.database.connection.execute(
                """
                INSERT INTO enrichment_tasks (
                    id, platform, source_id, kind, input_hash,
                    status, attempts, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                """,
                (
                    task_id,
                    platform,
                    source_id,
                    kind,
                    input_hash,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self.database.connection.execute(
                """
                SELECT id
                FROM enrichment_tasks
                WHERE platform = ? AND source_id = ?
                  AND kind = ? AND input_hash = ?
                """,
                (platform, source_id, kind, input_hash),
            ).fetchone()
            if existing is None:
                raise
            return str(existing["id"])
        return task_id

    def claim_next(
        self,
        kind: str | None = None,
        *,
        platform: str | None = None,
        excluded_ids: Collection[str] = (),
    ) -> EnrichmentTask | None:
        with self.database.transaction():
            kind_clause = " AND kind = ?" if kind is not None else ""
            # Work whose cost is paid per item is worth being able to spend a
            # slice of. Without this the only way to enrich the cheap platform
            # first is to claim everything and skip what you did not want,
            # which records a refusal against items nobody refused.
            platform_clause = " AND platform = ?" if platform is not None else ""
            exclusions = tuple(dict.fromkeys(excluded_ids))
            exclusion_clause = ""
            if exclusions:
                exclusion_clause = " AND id NOT IN (" + ", ".join("?" for _ in exclusions) + ")"
            params: tuple[str, ...] = (
                ((kind,) if kind is not None else ())
                + ((platform,) if platform is not None else ())
                + exclusions
            )
            row = self.database.connection.execute(
                f"""
                SELECT id, platform, source_id, kind, input_hash, attempts
                FROM enrichment_tasks
                WHERE status = 'pending'{kind_clause}{platform_clause}{exclusion_clause}
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None

            timestamp = now()
            self.database.connection.execute(
                """
                UPDATE enrichment_tasks
                SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (timestamp, row["id"]),
            )
            return EnrichmentTask(
                id=str(row["id"]),
                platform=str(row["platform"]),
                source_id=str(row["source_id"]),
                kind=str(row["kind"]),
                input_hash=str(row["input_hash"]),
                attempts=int(row["attempts"]) + 1,
            )

    def complete(self, task_id: str) -> None:
        cursor = self.database.connection.execute(
            """
            UPDATE enrichment_tasks
            SET status = 'completed', error = NULL, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (now(), task_id),
        )
        if cursor.rowcount != 1:
            self._raise_invalid_transition(task_id)

    def fail(self, task_id: str, error: str) -> None:
        cursor = self.database.connection.execute(
            """
            UPDATE enrichment_tasks
            SET status = 'pending', error = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (error, now(), task_id),
        )
        if cursor.rowcount != 1:
            self._raise_invalid_transition(task_id)

    def decline(self, task_id: str, error: str) -> None:
        """Retire a task whose content cannot be enriched, keeping the reason.

        ``fail`` returns a task to ``pending`` because most failures are the
        attempt's fault and deserve another one. A verdict about the content
        itself is not: it will hold until the content changes, and returning it
        to the queue means the next claim hands back the same task forever —
        one unsummarizable item stalls the whole run. Tasks are keyed by
        ``input_hash``, so changed content enqueues a fresh task on its own.
        """
        cursor = self.database.connection.execute(
            """
            UPDATE enrichment_tasks
            SET status = 'declined', error = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (error, now(), task_id),
        )
        if cursor.rowcount != 1:
            self._raise_invalid_transition(task_id)

    def reset_running(self) -> int:
        cursor = self.database.connection.execute(
            """
            UPDATE enrichment_tasks
            SET status = 'pending', updated_at = ?
            WHERE status = 'running'
            """,
            (now(),),
        )
        return cursor.rowcount

    def _raise_invalid_transition(self, task_id: str) -> None:
        row = self.database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        raise ValueError(
            f"enrichment task {task_id} must be running, current status is {row['status']}"
        )
