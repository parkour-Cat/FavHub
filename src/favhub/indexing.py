from contextlib import suppress
from dataclasses import dataclass

from favhub.chunking import ContentChunk, chunk_markdown
from favhub.database import Database
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.enrichment_queue import EnrichmentQueue, EnrichmentTask, now
from favhub.fts_text import fts_text
from favhub.item_store import ItemStore


@dataclass(frozen=True, slots=True)
class IndexedTask:
    task: EnrichmentTask
    chunk_count: int


class ContentIndexer:
    """Consume durable ``index_content`` tasks into derived content chunks."""

    def __init__(
        self,
        database: Database,
        store: ItemStore,
        queue: EnrichmentQueue,
        profile_store: EmbeddingProfileStore | None = None,
    ) -> None:
        self.database = database
        self.store = store
        self.queue = queue
        self.profile_store = profile_store or EmbeddingProfileStore(database)

    def index_next(self) -> IndexedTask | None:
        task = self.queue.claim_next(kind="index_content")
        if task is None:
            return None
        return self.index_task(task)

    def index_task(self, task: EnrichmentTask) -> IndexedTask:
        if task.kind != "index_content":
            raise ValueError(f"unsupported task kind: {task.kind}")
        try:
            row = self.database.connection.execute(
                "SELECT content_hash, item_dir FROM items WHERE platform = ? AND source_id = ?",
                (task.platform, task.source_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown item: {task.platform}/{task.source_id}")
            expected_dir = self.store._item_directory(task.platform, task.source_id)
            if str(row["item_dir"]) != str(expected_dir):
                raise ValueError("registered item directory is outside the item store")

            entries = self.store.iter_index_markdown(task.platform, task.source_id)
            chunks: list[ContentChunk] = []
            for relative_path, text in entries:
                chunks.extend(chunk_markdown(relative_path, text))
            fingerprint = self.store.fingerprint_index_markdown(entries)
            if fingerprint != task.input_hash:
                current = self.database.connection.execute(
                    """SELECT index_input_hash FROM items
                       WHERE platform = ? AND source_id = ?""",
                    (task.platform, task.source_id),
                ).fetchone()
                if current is not None and current["index_input_hash"] != task.input_hash:
                    self.queue.complete(task.id)
                    return IndexedTask(task=task, chunk_count=0)
                raise ValueError("index input changed before task processing")

            timestamp = now()
            with self.database.transaction():
                self.database.connection.execute(
                    "DELETE FROM content_chunks WHERE platform = ? AND source_id = ?",
                    (task.platform, task.source_id),
                )
                self.database.connection.executemany(
                    """
                    INSERT INTO content_chunks(
                        platform, source_id, ordinal, relative_path,
                        line_start, line_end, heading, text, fts_text,
                        input_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            task.platform,
                            task.source_id,
                            ordinal,
                            chunk.relative_path,
                            chunk.line_start,
                            chunk.line_end,
                            chunk.heading,
                            chunk.text,
                            fts_text(chunk.text),
                            fingerprint,
                            timestamp,
                        )
                        for ordinal, chunk in enumerate(chunks)
                    ],
                )
                cursor = self.database.connection.execute(
                    """UPDATE items SET index_input_hash = ?
                       WHERE platform = ? AND source_id = ?
                         AND index_input_hash = ?""",
                    (fingerprint, task.platform, task.source_id, task.input_hash),
                )
                if cursor.rowcount != 1:
                    raise ValueError("index input changed during task processing")
                profile = self.profile_store.active()
                if profile is not None:
                    embed_task_id = self.queue.enqueue(
                        task.platform,
                        task.source_id,
                        "embed_content",
                        embedding_task_input_hash(profile.id, fingerprint),
                    )
                    self.database.connection.execute(
                        """UPDATE enrichment_tasks
                           SET status = 'pending', error = NULL, updated_at = ?
                           WHERE id = ? AND status = 'completed'""",
                        (now(), embed_task_id),
                    )
                self.queue.complete(task.id)
            return IndexedTask(task=task, chunk_count=len(chunks))
        except Exception as exc:
            # Preserve the original indexing error; transition failures are
            # generally only possible when a caller supplied an invalid task.
            with suppress(Exception):
                self.queue.fail(task.id, str(exc))
            raise

    def reindex_missing(self, force: bool = False) -> int:
        rows = self.database.connection.execute(
            "SELECT platform, source_id FROM items ORDER BY platform, source_id"
        ).fetchall()
        enqueued = 0
        for row in rows:
            platform = str(row["platform"])
            source_id = str(row["source_id"])
            entries = self.store.iter_index_markdown(platform, source_id)
            fingerprint = self.store.fingerprint_index_markdown(entries)
            has_chunks = any(chunk_markdown(path, text) for path, text in entries)
            with self.database.transaction():
                self.database.connection.execute(
                    """UPDATE items SET index_input_hash = ?
                       WHERE platform = ? AND source_id = ?""",
                    (fingerprint, platform, source_id),
                )
                indexed = self.database.connection.execute(
                    """
                    SELECT COUNT(*) AS count, MAX(input_hash) AS input_hash
                    FROM content_chunks WHERE platform = ? AND source_id = ?
                    """,
                    (platform, source_id),
                ).fetchone()
                count = int(indexed["count"] or 0)
                stale = indexed["input_hash"] not in (None, fingerprint)
                task_row = self.database.connection.execute(
                    """SELECT id, status FROM enrichment_tasks
                       WHERE platform=? AND source_id=? AND kind='index_content'
                         AND input_hash=?""",
                    (platform, source_id, fingerprint),
                ).fetchone()
                chunks_current = count > 0 and not stale
                task_status = None if task_row is None else str(task_row["status"])
                up_to_date = chunks_current if has_chunks else task_status == "completed"
                already_queued = task_status in {"pending", "running"}
                if not force and (already_queued or up_to_date):
                    continue
                if force:
                    self.database.connection.execute(
                        "DELETE FROM content_chunks WHERE platform = ? AND source_id = ?",
                        (platform, source_id),
                    )
                if already_queued:
                    continue
                task_id = self.queue.enqueue(platform, source_id, "index_content", fingerprint)
                if task_status == "completed":
                    cursor = self.database.connection.execute(
                        """
                        UPDATE enrichment_tasks
                        SET status = 'pending', error = NULL, updated_at = ?
                        WHERE id = ? AND status = 'completed'
                        """,
                        (now(), task_id),
                    )
                    if cursor.rowcount == 0:
                        continue
                enqueued += 1
        return enqueued


__all__ = ["ContentIndexer", "IndexedTask"]
