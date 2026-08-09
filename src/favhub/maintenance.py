from dataclasses import dataclass
from typing import cast

from favhub.database import Database
from favhub.domain import ITEM_AVAILABLE, ITEM_MISSING
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    registered_items: int
    reset_tasks: int


class StartupMaintenance:
    def __init__(
        self,
        database: Database,
        store: ItemStore,
        queue: EnrichmentQueue,
    ) -> None:
        self.database = database
        self.store = store
        self.queue = queue

    def run(self) -> MaintenanceReport:
        # ItemStore validates every snapshot before returning, so no database
        # writes happen when any published source.json is corrupt.
        snapshots = self.store.iter_sources()
        published_keys = {
            (cast(str, snapshot["platform"]), cast(str, snapshot["source_id"]))
            for snapshot in snapshots
        }
        for snapshot in snapshots:
            self.store.ensure_content_from_source(
                cast(str, snapshot["platform"]), cast(str, snapshot["source_id"])
            )
        registered_items = 0

        for snapshot in snapshots:
            platform = cast(str, snapshot["platform"])
            source_id = cast(str, snapshot["source_id"])
            content_hash = cast(str, snapshot["content_hash"])
            published_at = cast(str, snapshot["published_at"])
            observed_at = cast(str, snapshot["observed_at"])
            item_dir = str(self.store.items_root / platform / source_id)
            index_input_hash = self.store.index_fingerprint(platform, source_id)

            with self.database.transaction():
                row = self.database.connection.execute(
                    """
                    SELECT content_hash, item_dir, published_at, access_status
                    FROM items
                    WHERE platform = ? AND source_id = ?
                    """,
                    (platform, source_id),
                ).fetchone()
                if row is None:
                    self.database.connection.execute(
                        """
                        INSERT INTO items (
                            platform, source_id, content_hash, item_dir,
                            published_at, first_seen_at, index_input_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            platform,
                            source_id,
                            content_hash,
                            item_dir,
                            published_at,
                            observed_at,
                            index_input_hash,
                        ),
                    )
                    registered_items += 1
                elif (
                    str(row["content_hash"]) != content_hash
                    or str(row["item_dir"]) != item_dir
                    or str(row["published_at"]) != published_at
                    or str(row["access_status"]) == ITEM_MISSING
                ):
                    self.database.connection.execute(
                        f"""
                        UPDATE items
                        SET content_hash = ?, item_dir = ?, published_at = ?,
                            -- Only the state this pass owns. Finding the local
                            -- snapshot proves the snapshot is here; it says
                            -- nothing about whether the platform still serves
                            -- the source, so a tombstone stays a tombstone.
                            access_status = CASE
                                WHEN access_status = '{ITEM_MISSING}' THEN '{ITEM_AVAILABLE}'
                                ELSE access_status
                            END
                        WHERE platform = ? AND source_id = ?
                        """,
                        (
                            content_hash,
                            item_dir,
                            published_at,
                            platform,
                            source_id,
                        ),
                    )

                self.database.connection.execute(
                    """UPDATE items SET index_input_hash = ?
                       WHERE platform = ? AND source_id = ?""",
                    (index_input_hash, platform, source_id),
                )

                # Enqueue is deliberately inside this caller-owned transaction.
                # The queue's uniqueness key makes this safe on repeated runs.
                self.queue.enqueue(
                    platform,
                    source_id,
                    "index_content",
                    index_input_hash,
                )

        database_keys = {
            (str(row["platform"]), str(row["source_id"]))
            for row in self.database.connection.execute(
                "SELECT platform, source_id FROM items"
            ).fetchall()
        }
        missing_keys = database_keys.difference(published_keys)
        for platform, source_id in missing_keys:
            with self.database.transaction():
                self.database.connection.execute(
                    f"""
                    UPDATE items
                    SET access_status = '{ITEM_MISSING}'
                    WHERE platform = ? AND source_id = ?
                    """,
                    (platform, source_id),
                )

        # This method is intentionally outside the per-item transactions and
        # uses the queue's direct autocommit update for restart recovery.
        reset_tasks = self.queue.reset_running()
        return MaintenanceReport(
            registered_items=registered_items,
            reset_tasks=reset_tasks,
        )


__all__ = ["MaintenanceReport", "StartupMaintenance"]
