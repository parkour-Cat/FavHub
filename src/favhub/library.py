import json
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from favhub.capture import CAPTURE_ERROR_CODES, SOURCE_UNAVAILABLE
from favhub.database import Database
from favhub.domain import (
    ITEM_AVAILABLE,
    ITEM_UNAVAILABLE,
    CapturedItem,
    isoformat,
    validate_enrichment,
)
from favhub.enrichment_queue import EnrichmentQueue, now
from favhub.item_store import ItemStore


def _access_status(metadata: Any) -> str | None:
    """Whether the platform still serves this item, or None when unknown.

    Three answers, not two, and the third is the one that matters. A run that
    was rate limited or met a changed page did not learn that the source is
    gone — it learned nothing — so it must leave the column alone. Folding
    those into "unavailable" would bury a live item on a bad afternoon, and
    every search path filters on this column, so the item would simply cease
    to exist with nothing to show why.

    A capture that says nothing about the source is not the same as one that
    failed: fixtures and older extractors carry no `source_status`, and having
    produced an item at all is the evidence that it was there. Only a real
    capture error code withholds a verdict.

    Resurrection is real and handled: a source that answers again goes back to
    available, because platforms do restore things.
    """
    if not isinstance(metadata, dict) or "source_status" not in metadata:
        return ITEM_AVAILABLE
    status = metadata["source_status"]
    if status == SOURCE_UNAVAILABLE:
        return ITEM_UNAVAILABLE
    return ITEM_AVAILABLE if status not in CAPTURE_ERROR_CODES else None


def _favorited_at(metadata: Any) -> str | None:
    """Extract a valid tz-aware favorited_at string from platform metadata."""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("favorited_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return value


@dataclass(frozen=True, slots=True)
class BatchReceipt:
    receipt_id: str
    added: int
    refreshed: int
    duplicates: int
    out_of_range: int = 0


class LibraryModule:
    def __init__(
        self,
        database: Database,
        store: ItemStore,
        queue: EnrichmentQueue,
    ) -> None:
        self.database = database
        self.store = store
        self.queue = queue

    def ingest_batch(
        self,
        job_id: str,
        platform: str,
        idempotency_key: str,
        items: list[CapturedItem],
        refresh_existing: bool,
    ) -> BatchReceipt:
        for item in items:
            if item.platform != platform:
                raise ValueError(
                    "captured item platform does not match batch platform: "
                    f"{item.platform!r} != {platform!r}"
                )

        transaction = (
            nullcontext()
            if self.database.connection.in_transaction
            else self.database.transaction()
        )
        with transaction:
            existing_batch = self.database.connection.execute(
                """
                SELECT receipt_json
                FROM sync_batches
                WHERE job_id = ? AND platform = ? AND idempotency_key = ?
                """,
                (job_id, platform, idempotency_key),
            ).fetchone()
            if existing_batch is not None:
                return self._receipt_from_json(str(existing_batch["receipt_json"]))

            job = self.database.connection.execute(
                "SELECT 1 FROM sync_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job is None:
                raise ValueError(f"unknown sync job: {job_id}")

            added = 0
            refreshed = 0
            duplicates = 0
            for item in items:
                row = self.database.connection.execute(
                    """
                    SELECT content_hash, item_dir, published_at, access_status,
                           index_input_hash
                    FROM items
                    WHERE platform = ? AND source_id = ?
                    """,
                    (item.platform, item.source_id),
                ).fetchone()

                if row is None:
                    self._insert_new_item(item, refresh_existing)
                    added += 1
                    continue

                if not refresh_existing:
                    duplicates += 1
                    continue

                content_changed = str(row["content_hash"]) != item.content_hash
                snapshot_current = self.store.published_snapshot_matches(item)
                content_repaired = False
                if snapshot_current:
                    content_repaired = self.store.ensure_content_from_source(
                        item.platform, item.source_id
                    )
                expected_directory = str(self.store.items_root / item.platform / item.source_id)
                database_current = (
                    not content_changed
                    and str(row["item_dir"]) == expected_directory
                    and str(row["published_at"]) == isoformat(item.published_at)
                    and str(row["access_status"]) == ITEM_AVAILABLE
                )
                if snapshot_current and not content_repaired and database_current:
                    self.database.connection.execute(
                        """
                        UPDATE items
                        SET last_full_synced_at = ?
                        WHERE platform = ? AND source_id = ?
                        """,
                        (self._now(), item.platform, item.source_id),
                    )
                    duplicates += 1
                    continue

                self._refresh_item(
                    item,
                    publish_snapshot=not snapshot_current,
                    previous_index_hash=(
                        None if row["index_input_hash"] is None else str(row["index_input_hash"])
                    ),
                )
                refreshed += 1

            receipt = BatchReceipt(
                receipt_id=str(uuid.uuid4()),
                added=added,
                refreshed=refreshed,
                duplicates=duplicates,
            )
            self.database.connection.execute(
                """
                INSERT INTO sync_batches (
                    receipt_id, job_id, platform, idempotency_key,
                    receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    job_id,
                    platform,
                    idempotency_key,
                    json.dumps(asdict(receipt), ensure_ascii=False, sort_keys=True),
                    self._now(),
                ),
            )
            return receipt

    def _insert_new_item(self, item: CapturedItem, full_sync: bool) -> None:
        stored = self.store.write(item)
        index_input_hash = self.store.index_fingerprint(item.platform, item.source_id)
        first_seen_at = isoformat(item.observed_at)
        last_full_synced_at = self._now() if full_sync else None
        self.database.connection.execute(
            """
            INSERT INTO items (
                platform, source_id, content_hash, item_dir, published_at,
                first_seen_at, last_full_synced_at, index_input_hash,
                favorited_at, access_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.platform,
                item.source_id,
                item.content_hash,
                str(stored.directory),
                isoformat(item.published_at),
                first_seen_at,
                last_full_synced_at,
                index_input_hash,
                _favorited_at(item.platform_metadata),
                # An item first seen as a tombstone is stored as one. It was
                # collected after the platform had already dropped it, so there
                # is no local copy to fall back on and no reason to let it into
                # search results. A withheld verdict on a first sighting can
                # only be optimistic; there is no earlier decision to keep.
                _access_status(item.platform_metadata) or ITEM_AVAILABLE,
            ),
        )
        self._record_collections(item)
        self.queue.enqueue(
            item.platform,
            item.source_id,
            "index_content",
            index_input_hash,
        )
        self.queue.enqueue(item.platform, item.source_id, "summarize", item.content_hash)

    def _refresh_item(
        self,
        item: CapturedItem,
        *,
        publish_snapshot: bool,
        previous_index_hash: str | None,
    ) -> None:
        if publish_snapshot:
            directory = self.store.write(item).directory
        else:
            directory = self.store.items_root / item.platform / item.source_id
        index_input_hash = self.store.index_fingerprint(item.platform, item.source_id)
        self.database.connection.execute(
            """
            UPDATE items
            SET content_hash = ?, item_dir = ?, published_at = ?,
                last_full_synced_at = ?,
                -- Only a run that actually learned something moves this. A
                -- transient failure passes NULL and the column keeps whatever
                -- the last informed run decided.
                access_status = COALESCE(?, access_status),
                index_input_hash = ?,
                favorited_at = CASE
                    WHEN ? IS NULL THEN favorited_at
                    WHEN favorited_at IS NULL
                         OR julianday(?) < julianday(favorited_at) THEN ?
                    ELSE favorited_at
                END
            WHERE platform = ? AND source_id = ?
            """,
            (
                item.content_hash,
                str(directory),
                isoformat(item.published_at),
                self._now(),
                _access_status(item.platform_metadata),
                index_input_hash,
                _favorited_at(item.platform_metadata),
                _favorited_at(item.platform_metadata),
                _favorited_at(item.platform_metadata),
                item.platform,
                item.source_id,
            ),
        )
        self._record_collections(item)
        if index_input_hash != previous_index_hash:
            self.queue.enqueue(
                item.platform,
                item.source_id,
                "index_content",
                index_input_hash,
            )
        self.queue.enqueue(item.platform, item.source_id, "summarize", item.content_hash)

    def apply_enrichment(self, task_id: str, fields: dict[str, Any]) -> str:
        """Apply Agent-generated enrichment for a claimed summarize task.

        Returns ``"applied"``, or ``"stale"`` when the item's content changed
        after the task was claimed — the superseded task is completed without
        writing anything (a task for the new hash already exists).
        """
        transaction = (
            nullcontext()
            if self.database.connection.in_transaction
            else self.database.transaction()
        )
        with transaction:
            task = self.database.connection.execute(
                """
                SELECT platform, source_id, kind, input_hash, status
                FROM enrichment_tasks WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(f"unknown enrichment task: {task_id}")
            if str(task["kind"]) != "summarize":
                raise ValueError(f"enrichment task {task_id} is not a summarize task")
            if str(task["status"]) != "running":
                raise ValueError(
                    f"enrichment task {task_id} must be running, current status is {task['status']}"
                )
            platform = str(task["platform"])
            source_id = str(task["source_id"])
            row = self.database.connection.execute(
                """
                SELECT content_hash, index_input_hash FROM items
                WHERE platform = ? AND source_id = ?
                """,
                (platform, source_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown item: {platform}/{source_id}")
            if str(row["content_hash"]) != str(task["input_hash"]):
                self.queue.complete(task_id)
                return "stale"
            block = validate_enrichment(
                {
                    **fields,
                    "generated_at": self._now(),
                    "input_hash": str(task["input_hash"]),
                }
            )
            self.store.apply_enrichment(platform, source_id, block)
            index_input_hash = self.store.index_fingerprint(platform, source_id)
            previous = None if row["index_input_hash"] is None else str(row["index_input_hash"])
            self.database.connection.execute(
                """
                UPDATE items SET content_type = ?, index_input_hash = ?
                WHERE platform = ? AND source_id = ?
                """,
                (block["content_type"], index_input_hash, platform, source_id),
            )
            if index_input_hash != previous:
                self.queue.enqueue(platform, source_id, "index_content", index_input_hash)
            self.queue.complete(task_id)
            return "applied"

    def redo_declined_enrichment(self) -> dict[str, int]:
        """Return declined summarize tasks to the queue.

        Declining is a verdict about content, and an agent can reach it wrongly
        — refusing a video because its subtitle looked unrelated, when its
        description said plenty. That mistake is invisible afterwards: the task
        is out of the queue and backfill only covers items that never had one.

        Nothing here judges which declines were wrong. It reopens all of them so
        the next pass can decide again, which is cheap when the count is small
        and the alternative is leaving items with no route back at all.
        """
        with self.database.transaction():
            cursor = self.database.connection.execute(
                """UPDATE enrichment_tasks
                      SET status = 'pending', error = NULL, updated_at = ?
                    WHERE kind = 'summarize' AND status = 'declined'""",
                (now(),),
            )
        return {"requeued": cursor.rowcount}

    def redo_enrichment(self, model: str) -> dict[str, int]:
        """Requeue every item whose stored enrichment came from one model.

        A batch can be bad in a way no per-item check catches until someone
        reads it — a cheap model that transliterated Chinese tags rather than
        translating them, say. Without this there is no way back: the summarize
        task is completed and backfill only covers items that never had one.

        Keyed on the recorded model, which is the reason that field has to be
        honest. Existing enrichment stays in place until something better
        replaces it, so a run that is interrupted leaves the library no worse.
        """
        requeued = 0
        examined = 0
        with self.database.transaction():
            for row in self.database.connection.execute(
                "SELECT platform, source_id, content_hash FROM items "
                "WHERE access_status = ? ORDER BY platform, source_id",
                (ITEM_AVAILABLE,),
            ).fetchall():
                platform = str(row["platform"])
                source_id = str(row["source_id"])
                snapshot = self.store.read_source(platform, source_id)
                block = (snapshot or {}).get("enrichment")
                if not isinstance(block, dict) or block.get("model") != model:
                    continue
                examined += 1
                task_id = self.queue.enqueue(
                    platform, source_id, "summarize", str(row["content_hash"])
                )
                cursor = self.database.connection.execute(
                    """UPDATE enrichment_tasks
                          SET status = 'pending', error = NULL, updated_at = ?
                        WHERE id = ? AND status IN ('completed', 'declined')""",
                    (now(), task_id),
                )
                requeued += 1 if cursor.rowcount else 0
        return {"matched": examined, "requeued": requeued}

    def backfill_summarize(self) -> dict[str, int]:
        """Enqueue summarize tasks for items lacking coverage of their hash.

        An item is current when a summarize task exists for its content_hash
        or its snapshot already carries an enrichment block for that hash
        (e.g. a root restored from items/ files alone).
        """
        missing = self.database.connection.execute(
            """
            SELECT i.platform, i.source_id, i.content_hash
            FROM items AS i
            LEFT JOIN enrichment_tasks AS t
              ON t.platform = i.platform AND t.source_id = i.source_id
             AND t.kind = 'summarize' AND t.input_hash = i.content_hash
            WHERE t.id IS NULL
            ORDER BY i.platform, i.source_id
            """
        ).fetchall()
        total = self.database.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        enqueued = 0
        with self.database.transaction():
            for row in missing:
                snapshot = self.store.read_source(str(row["platform"]), str(row["source_id"]))
                block = (snapshot or {}).get("enrichment")
                if isinstance(block, dict) and block.get("input_hash") == str(row["content_hash"]):
                    continue
                self.queue.enqueue(
                    str(row["platform"]),
                    str(row["source_id"]),
                    "summarize",
                    str(row["content_hash"]),
                )
                enqueued += 1
        return {"enqueued": enqueued, "already_current": int(total) - enqueued}

    @staticmethod
    def _receipt_from_json(receipt_json: str) -> BatchReceipt:
        payload = json.loads(receipt_json)
        if not isinstance(payload, dict):
            raise ValueError("persisted batch receipt must be a JSON object")
        return BatchReceipt(
            receipt_id=str(payload["receipt_id"]),
            added=int(payload["added"]),
            refreshed=int(payload["refreshed"]),
            duplicates=int(payload["duplicates"]),
            out_of_range=int(payload.get("out_of_range", 0)),
        )

    def backfill_favorited_at(self) -> dict[str, int]:
        """Lift platform_metadata.favorited_at from snapshots into the column."""
        rows = self.database.connection.execute(
            "SELECT platform, source_id, favorited_at FROM items ORDER BY platform, source_id"
        ).fetchall()
        updated = 0
        with self.database.transaction():
            for row in rows:
                snapshot = self.store.read_source(str(row["platform"]), str(row["source_id"]))
                value = _favorited_at((snapshot or {}).get("platform_metadata"))
                cursor = None
                if value is not None:
                    # First-favorited semantics: never move the column later.
                    cursor = self.database.connection.execute(
                        """UPDATE items SET favorited_at = ?
                           WHERE platform = ? AND source_id = ?
                             AND (favorited_at IS NULL
                                  OR julianday(?) < julianday(favorited_at))""",
                        (value, str(row["platform"]), str(row["source_id"]), value),
                    )
                if cursor is not None and cursor.rowcount:
                    updated += 1
        return {"updated": updated, "unchanged": len(rows) - updated}

    def _record_collections(self, item: CapturedItem) -> None:
        """Merge this item's folder memberships, never replacing them.

        Merged rather than replaced because an incremental run sees only the
        top of each folder — it stops at the frontier — so its view of which
        folders hold an item is a partial one. Replacing from a partial
        observation would delete memberships the run never looked at.

        The cost is that a membership the user has since removed lingers. That
        is the same undecided question as un-favouriting an item, which the
        library also keeps: this is an archive of what was saved, and pruning
        it is a policy nobody has chosen yet.
        """
        self.database.connection.executemany(
            """INSERT INTO item_collections (platform, source_id, name)
               VALUES (?, ?, ?)
               ON CONFLICT (platform, source_id, name) DO NOTHING""",
            [
                (item.platform, item.source_id, name)
                for name in sorted({name.strip() for name in item.collections if name.strip()})
            ],
        )

    def backfill_collections(self) -> dict[str, int]:
        """Lift folder memberships out of existing snapshots into the table."""
        rows = self.database.connection.execute(
            "SELECT platform, source_id FROM items ORDER BY platform, source_id"
        ).fetchall()
        items = 0
        memberships = 0
        with self.database.transaction():
            for row in rows:
                platform = str(row["platform"])
                source_id = str(row["source_id"])
                snapshot = self.store.read_source(platform, source_id) or {}
                names = snapshot.get("collections")
                if not isinstance(names, list):
                    continue
                clean = sorted(
                    {name.strip() for name in names if isinstance(name, str) and name.strip()}
                )
                if not clean:
                    continue
                cursor = self.database.connection.executemany(
                    """INSERT INTO item_collections (platform, source_id, name)
                       VALUES (?, ?, ?)
                       ON CONFLICT (platform, source_id, name) DO NOTHING""",
                    [(platform, source_id, name) for name in clean],
                )
                items += 1
                memberships += cursor.rowcount if cursor.rowcount > 0 else 0
        return {"items": items, "memberships": memberships}

    def backfill_access_status(self) -> dict[str, int]:
        """Lift platform_metadata.source_status from snapshots into the column.

        Needed once, because the column was written as a constant long before
        anything read the status the mappers had been recording all along. Every
        tombstone collected before then is sitting in the index as a healthy
        item with an empty body.
        """
        rows = self.database.connection.execute(
            "SELECT platform, source_id, access_status FROM items ORDER BY platform, source_id"
        ).fetchall()
        counts = {ITEM_AVAILABLE: 0, ITEM_UNAVAILABLE: 0, "unchanged": 0}
        with self.database.transaction():
            for row in rows:
                platform = str(row["platform"])
                source_id = str(row["source_id"])
                snapshot = self.store.read_source(platform, source_id)
                status = _access_status((snapshot or {}).get("platform_metadata"))
                if status is None or status == str(row["access_status"]):
                    counts["unchanged"] += 1
                    continue
                self.database.connection.execute(
                    "UPDATE items SET access_status = ? WHERE platform = ? AND source_id = ?",
                    (status, platform, source_id),
                )
                counts[status] += 1
        return counts

    @staticmethod
    def _now() -> str:
        return isoformat(datetime.now(UTC))
