"""Durable browser capture sessions.

A session records what the browser is currently doing for one platform run;
the sync tables stay the record of what actually landed. Keeping the two apart
means an interrupted browser never advances a frontier on its own.

This module owns SQL and transition checks only. It never calls a platform
parser and never sees a raw response body: pause/fail reasons are a stable code
plus a short redacted message.
"""

import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from favhub.capture import BROWSER_ERROR_CODES, BROWSER_UNAVAILABLE
from favhub.database import Database

MAX_ERROR_MESSAGE = 200
LEASE_EXPIRED_MESSAGE = "capture lease expired"

# Bumped whenever the extension <-> FavHub message contract changes shape.
# A session records the version it was created with so a mismatched extension
# is refused loudly instead of half-working.
BROWSER_PROTOCOL_VERSION = 1


class BrowserCaptureStatus(StrEnum):
    AWAITING_BROWSER = "awaiting_browser"
    CAPTURING = "capturing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        BrowserCaptureStatus.COMPLETED,
        BrowserCaptureStatus.FAILED,
        BrowserCaptureStatus.CANCELLED,
    }
)

OPEN_STATUSES = frozenset(
    {
        BrowserCaptureStatus.AWAITING_BROWSER,
        BrowserCaptureStatus.CAPTURING,
        BrowserCaptureStatus.PAUSED,
    }
)


class BrowserCaptureError(RuntimeError):
    """An unknown session, or a transition the state machine forbids."""


@dataclass(frozen=True, slots=True)
class BrowserCaptureSession:
    id: str
    job_id: str
    platform: str
    status: BrowserCaptureStatus
    protocol_version: int
    extension_version: str | None
    lease_expires_at: str | None
    error: tuple[str, str] | None
    created_at: str
    updated_at: str
    finished_at: str | None

    def as_dict(self) -> dict[str, object]:
        """Render the session for MCP results (snake_case, no raw bodies)."""
        return {
            "session_id": self.id,
            "job_id": self.job_id,
            "platform": self.platform,
            "status": self.status.value,
            "protocol_version": self.protocol_version,
            "extension_version": self.extension_version,
            "lease_expires_at": self.lease_expires_at,
            "error": (
                None if self.error is None else {"code": self.error[0], "message": self.error[1]}
            ),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


def _format(moment: datetime) -> str:
    """Render a UTC timestamp in the repository's ISO-8601 ``Z`` form."""
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _row_to_session(row: sqlite3.Row) -> BrowserCaptureSession:
    raw_error = row["error_json"]
    error: tuple[str, str] | None = None
    if raw_error is not None:
        payload = json.loads(str(raw_error))
        error = (str(payload["code"]), str(payload["message"]))
    return BrowserCaptureSession(
        id=str(row["id"]),
        job_id=str(row["job_id"]),
        platform=str(row["platform"]),
        status=BrowserCaptureStatus(str(row["status"])),
        protocol_version=int(row["protocol_version"]),
        extension_version=None
        if row["extension_version"] is None
        else str(row["extension_version"]),
        lease_expires_at=None if row["lease_expires_at"] is None else str(row["lease_expires_at"]),
        error=error,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        finished_at=None if row["finished_at"] is None else str(row["finished_at"]),
    )


def _encode_error(code: str, message: str) -> str:
    if code not in BROWSER_ERROR_CODES:
        raise ValueError(f"unknown browser capture error code: {code}")
    return json.dumps(
        {"code": code, "message": message[:MAX_ERROR_MESSAGE]},
        ensure_ascii=False,
        sort_keys=True,
    )


class BrowserCaptureStore:
    def __init__(
        self,
        database: Database,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self.database = database
        self.clock = clock

    # -- reads ---------------------------------------------------------------

    def get(self, session_id: str) -> BrowserCaptureSession:
        row = self.database.connection.execute(
            "SELECT * FROM browser_capture_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise BrowserCaptureError(f"unknown browser capture session: {session_id}")
        return _row_to_session(row)

    def find_open(self, platform: str) -> BrowserCaptureSession | None:
        """Return the one non-terminal session for a platform, if any.

        The partial unique index guarantees at most one, so callers can treat
        this as the session the extension is allowed to claim.
        """
        row = self.database.connection.execute(
            """
            SELECT * FROM browser_capture_sessions
            WHERE platform = ?
              AND status IN ('awaiting_browser', 'capturing', 'paused')
            """,
            (platform,),
        ).fetchone()
        return None if row is None else _row_to_session(row)

    def find_for_job(self, job_id: str, platform: str) -> BrowserCaptureSession | None:
        """Return this job's session for a platform, terminal or not."""
        row = self.database.connection.execute(
            "SELECT * FROM browser_capture_sessions WHERE job_id = ? AND platform = ?",
            (job_id, platform),
        ).fetchone()
        return None if row is None else _row_to_session(row)

    def for_job(self, job_id: str) -> tuple[BrowserCaptureSession, ...]:
        rows = self.database.connection.execute(
            "SELECT * FROM browser_capture_sessions WHERE job_id = ? ORDER BY platform",
            (job_id,),
        ).fetchall()
        return tuple(_row_to_session(row) for row in rows)

    # -- transitions ---------------------------------------------------------

    def create(
        self,
        job_id: str,
        platform: str,
        protocol_version: int,
    ) -> BrowserCaptureSession:
        session_id = str(uuid.uuid4())
        timestamp = _format(self.clock())
        try:
            self.database.connection.execute(
                """
                INSERT INTO browser_capture_sessions (
                    id, job_id, platform, status, protocol_version,
                    extension_version, lease_expires_at, error_json,
                    created_at, updated_at, finished_at
                ) VALUES (?, ?, ?, 'awaiting_browser', ?, NULL, NULL, NULL, ?, ?, NULL)
                """,
                (session_id, job_id, platform, protocol_version, timestamp, timestamp),
            )
        except sqlite3.IntegrityError as error:
            raise BrowserCaptureError(
                f"a browser capture session is already open for platform {platform}"
            ) from error
        return self.get(session_id)

    def claim(
        self,
        session_id: str,
        extension_version: str,
        lease_seconds: int,
    ) -> BrowserCaptureSession:
        """Hand the session to the extension and start a fresh lease.

        Resuming a paused session clears its previous error so a stale pause
        reason never survives into a run that is making progress again.
        """
        with self.database.transaction():
            session = self.get(session_id)
            if session.status not in {
                BrowserCaptureStatus.AWAITING_BROWSER,
                BrowserCaptureStatus.PAUSED,
            }:
                raise BrowserCaptureError(
                    f"cannot claim a session in status {session.status.value}"
                )
            moment = self.clock()
            self.database.connection.execute(
                """
                UPDATE browser_capture_sessions
                SET status = 'capturing',
                    extension_version = ?,
                    lease_expires_at = ?,
                    error_json = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    extension_version,
                    _format(moment + timedelta(seconds=lease_seconds)),
                    _format(moment),
                    session_id,
                ),
            )
        return self.get(session_id)

    def renew(self, session_id: str, lease_seconds: int) -> BrowserCaptureSession:
        """Push a capturing session's lease out, without re-claiming it.

        Claiming is deliberately refused once a session is capturing, so it
        cannot double as the renewal: a heartbeat that tried it got the refusal,
        swallowed it, and left the lease exactly where the claim had put it.
        Every run therefore died at claim + one lease, however healthy — three
        minutes, measured on live X and Zhihu runs that were still collecting.
        """
        with self.database.transaction():
            session = self.get(session_id)
            if session.status is not BrowserCaptureStatus.CAPTURING:
                raise BrowserCaptureError(
                    f"cannot renew a session in status {session.status.value}"
                )
            moment = self.clock()
            self.database.connection.execute(
                """
                UPDATE browser_capture_sessions
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _format(moment + timedelta(seconds=lease_seconds)),
                    _format(moment),
                    session_id,
                ),
            )
        return self.get(session_id)

    def pause(self, session_id: str, code: str, message: str) -> BrowserCaptureSession:
        encoded = _encode_error(code, message)
        with self.database.transaction():
            session = self.get(session_id)
            if session.status in TERMINAL_STATUSES:
                raise BrowserCaptureError(
                    f"cannot pause a session in status {session.status.value}"
                )
            self.database.connection.execute(
                """
                UPDATE browser_capture_sessions
                SET status = 'paused',
                    lease_expires_at = NULL,
                    error_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (encoded, _format(self.clock()), session_id),
            )
        return self.get(session_id)

    def complete(self, session_id: str) -> BrowserCaptureSession:
        return self._finish(
            session_id,
            BrowserCaptureStatus.COMPLETED,
            allowed_from={BrowserCaptureStatus.CAPTURING},
            error=None,
        )

    def fail(self, session_id: str, code: str, message: str) -> BrowserCaptureSession:
        return self._finish(
            session_id,
            BrowserCaptureStatus.FAILED,
            allowed_from=OPEN_STATUSES,
            error=_encode_error(code, message),
        )

    def cancel(self, session_id: str) -> BrowserCaptureSession:
        return self._finish(
            session_id,
            BrowserCaptureStatus.CANCELLED,
            allowed_from=OPEN_STATUSES,
            error=None,
        )

    def recover_expired(self) -> tuple[str, ...]:
        """Pause capturing sessions whose lease ran out.

        Chrome closing, the page going away, or the pipe dropping all look the
        same from here: without this, a session would sit in ``capturing``
        forever and block the next run for that platform.
        """
        encoded = _encode_error(BROWSER_UNAVAILABLE, LEASE_EXPIRED_MESSAGE)
        with self.database.transaction():
            timestamp = _format(self.clock())
            rows = self.database.connection.execute(
                """
                SELECT id FROM browser_capture_sessions
                WHERE status = 'capturing'
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY id
                """,
                (timestamp,),
            ).fetchall()
            recovered = tuple(str(row["id"]) for row in rows)
            if recovered:
                self.database.connection.executemany(
                    """
                    UPDATE browser_capture_sessions
                    SET status = 'paused',
                        lease_expires_at = NULL,
                        error_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    [(encoded, timestamp, session_id) for session_id in recovered],
                )
        return recovered

    # -- internals -----------------------------------------------------------

    def _finish(
        self,
        session_id: str,
        target: BrowserCaptureStatus,
        *,
        allowed_from: frozenset[BrowserCaptureStatus] | set[BrowserCaptureStatus],
        error: str | None,
    ) -> BrowserCaptureSession:
        with self.database.transaction():
            session = self.get(session_id)
            if session.status is target:
                # Replaying the same terminal transition keeps the original
                # finished_at so a retried acknowledgement cannot move it.
                return session
            if session.status not in allowed_from:
                raise BrowserCaptureError(
                    f"cannot move a session from {session.status.value} to {target.value}"
                )
            timestamp = _format(self.clock())
            self.database.connection.execute(
                """
                UPDATE browser_capture_sessions
                SET status = ?,
                    lease_expires_at = NULL,
                    error_json = COALESCE(?, error_json),
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (target.value, error, timestamp, timestamp, session_id),
            )
        return self.get(session_id)


__all__ = [
    "LEASE_EXPIRED_MESSAGE",
    "MAX_ERROR_MESSAGE",
    "OPEN_STATUSES",
    "TERMINAL_STATUSES",
    "BrowserCaptureError",
    "BrowserCaptureSession",
    "BrowserCaptureStatus",
    "BrowserCaptureStore",
]
