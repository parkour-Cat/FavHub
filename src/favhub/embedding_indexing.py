"""Durable consumer for item-level ``embed_content`` tasks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from favhub.database import Database
from favhub.embedding import EmbeddingProfile, encode_float32, validate_embeddings
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.enrichment_queue import EnrichmentQueue, EnrichmentTask, now
from favhub.semantic_chunking import SemanticSegment
from favhub.semantic_chunking import segment_tokens as split_segments


class _Provider(Protocol):
    name: str
    version: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def tokenize(self, text: str) -> Sequence[int]: ...

    def decode_tokens(self, tokens: Sequence[int]) -> str: ...


ProviderLoader = Callable[[], _Provider]
Segmenter = Callable[..., Sequence[SemanticSegment]]


@dataclass(frozen=True, slots=True)
class EmbeddedTask:
    task: EnrichmentTask
    vector_count: int
    segment_count: int
    skipped: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedEmbeddingTask:
    task: EnrichmentTask
    profile: EmbeddingProfile
    index_hash: str
    payload: tuple[tuple[int, SemanticSegment], ...]


@dataclass(frozen=True, slots=True)
class _EmbeddingTaskState:
    task: EnrichmentTask
    profile: EmbeddingProfile
    index_hash: str
    chunks: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _ReindexPlan:
    platform: str
    source_id: str
    index_hash: str
    task_hash: str
    chunk_ids: frozenset[int]
    expected_segments: frozenset[tuple[int, int, int, int]]


class EmbeddingIndexer:
    """Generate and commit vectors while preserving durable task semantics."""

    def __init__(
        self,
        database: Database,
        queue: EnrichmentQueue,
        profiles: EmbeddingProfileStore,
        provider_loader: ProviderLoader | None = None,
        *,
        provider: _Provider | None = None,
        segmenter: Segmenter | None = None,
    ) -> None:
        self.database = database
        self.queue = queue
        self.profiles = profiles
        if provider_loader is not None and provider is not None:
            raise ValueError("configure provider_loader or provider, not both")
        self.provider_loader = provider_loader
        self.provider = provider
        self.segmenter = segmenter

    def index_next(self, *, excluded_ids: Sequence[str] = ()) -> EmbeddedTask | None:
        task = self.queue.claim_next(kind="embed_content", excluded_ids=excluded_ids)
        if task is None:
            return None
        return self.index_task(task)

    def index_task(self, task: EnrichmentTask) -> EmbeddedTask:
        return self.index_tasks((task,))[0]

    def index_tasks(self, tasks: Sequence[EnrichmentTask]) -> tuple[EmbeddedTask, ...]:
        if not tasks:
            return ()
        prepared: list[_PreparedEmbeddingTask] = []
        skipped: list[EmbeddedTask] = []
        try:
            provider = self._get_provider()
            states: list[_EmbeddingTaskState] = []
            for task in tasks:
                if task.kind != "embed_content":
                    raise ValueError(f"unsupported task kind: {task.kind}")
                profile = self.profiles.active()
                state = self._current_item_state(task, profile)
                if state is None:
                    self.queue.complete(task.id)
                    skipped.append(EmbeddedTask(task, 0, 0, skipped=True))
                    continue
                profile, index_hash, chunks = state
                self._validate_provider(profile, provider)
                states.append(
                    _EmbeddingTaskState(
                        task=task,
                        profile=profile,
                        index_hash=index_hash,
                        chunks=tuple(chunks),
                    )
                )
            prepared.extend(self._prepare_payloads(provider, states))

            texts = tuple(
                segment.text for prepared_task in prepared for _, segment in prepared_task.payload
            )
            vectors = validate_embeddings(
                provider,
                texts,
                provider.embed_documents(texts) if texts else (),
            )
            offset = 0
            results: list[EmbeddedTask] = []
            for prepared_task in prepared:
                vector_count = len(prepared_task.payload)
                task_vectors = vectors[offset : offset + vector_count]
                offset += vector_count
                blobs = tuple(
                    encode_float32(vector, dimensions=prepared_task.profile.dimensions)
                    for vector in task_vectors
                )
                self._commit_vectors(prepared_task, blobs)
                results.append(
                    EmbeddedTask(
                        prepared_task.task,
                        vector_count=vector_count,
                        segment_count=vector_count,
                    )
                )
            return tuple(results + skipped)
        except Exception as exc:
            for task in tasks:
                with suppress(Exception):
                    self.queue.fail(task.id, str(exc))
            raise

    def _prepare_payloads(
        self,
        provider: _Provider,
        states: Sequence[_EmbeddingTaskState],
    ) -> tuple[_PreparedEmbeddingTask, ...]:
        tokenize_many = getattr(provider, "tokenize_many", None)
        decode_many = getattr(provider, "decode_many", None)
        if self.segmenter is not None or not callable(tokenize_many) or not callable(decode_many):
            return tuple(
                _PreparedEmbeddingTask(
                    state.task,
                    state.profile,
                    state.index_hash,
                    tuple(
                        (int(chunk["id"]), segment)
                        for chunk in state.chunks
                        for segment in self._segments_for_text(
                            provider, state.profile, str(chunk["text"])
                        )
                    ),
                )
                for state in states
            )

        chunk_records = [
            (state_index, int(chunk["id"]), str(chunk["text"]))
            for state_index, state in enumerate(states)
            for chunk in state.chunks
        ]
        token_groups = tokenize_many(tuple(text for _, _, text in chunk_records))
        if len(token_groups) != len(chunk_records):
            raise ValueError("batch tokenizer must return one token group per content chunk")

        payloads: list[list[tuple[int, SemanticSegment]]] = [[] for _ in states]
        windows: list[tuple[int, int, int, int, int, tuple[int, ...]]] = []
        for (state_index, chunk_id, _), token_ids in zip(chunk_records, token_groups, strict=True):
            profile = states[state_index].profile
            for ordinal, start, end, window in self._segment_windows(profile, token_ids):
                windows.append((state_index, chunk_id, ordinal, start, end, window))
        decoded = decode_many(tuple(window for *_, window in windows))
        if len(decoded) != len(windows):
            raise ValueError("batch decoder must return one text value per token window")
        for (state_index, chunk_id, ordinal, start, end, _), text in zip(
            windows, decoded, strict=True
        ):
            payloads[state_index].append((chunk_id, SemanticSegment(ordinal, start, end, text)))
        return tuple(
            _PreparedEmbeddingTask(state.task, state.profile, state.index_hash, tuple(payload))
            for state, payload in zip(states, payloads, strict=True)
        )

    @staticmethod
    def _segment_windows(
        profile: EmbeddingProfile, token_ids: Sequence[int]
    ) -> tuple[tuple[int, int, int, tuple[int, ...]], ...]:
        windows: list[tuple[int, int, int, tuple[int, ...]]] = []
        start = 0
        ordinal = 0
        while start < len(token_ids):
            end = min(start + profile.segment_tokens, len(token_ids))
            windows.append((ordinal, start, end, tuple(token_ids[start:end])))
            ordinal += 1
            if end == len(token_ids):
                break
            start = end - profile.overlap_tokens
        return tuple(windows)

    def _commit_vectors(
        self,
        prepared: _PreparedEmbeddingTask,
        blobs: Sequence[bytes],
    ) -> None:
        task = prepared.task
        profile = prepared.profile
        index_hash = prepared.index_hash
        payload = prepared.payload
        if len(blobs) != len(payload):
            raise ValueError("embedding vector count must equal segment count")
        with self.database.transaction():
            current = self.profiles.active()
            if current is None or current.id != profile.id:
                raise ValueError("active embedding profile changed during inference")
            item = self.database.connection.execute(
                """SELECT index_input_hash FROM items
                   WHERE platform = ? AND source_id = ?""",
                (task.platform, task.source_id),
            ).fetchone()
            if item is None or str(item["index_input_hash"]) != index_hash:
                raise ValueError("item index input changed during inference")
            if embedding_task_input_hash(profile.id, index_hash) != task.input_hash:
                raise ValueError("embedding task input changed during inference")
            index_task = self.database.connection.execute(
                """SELECT status FROM enrichment_tasks
                   WHERE platform = ? AND source_id = ? AND kind = 'index_content'
                     AND input_hash = ?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (task.platform, task.source_id, index_hash),
            ).fetchone()
            if index_task is None or str(index_task["status"]) != "completed":
                raise ValueError("index task changed during embedding inference")
            self.database.connection.execute(
                """DELETE FROM chunk_embeddings
                   WHERE profile_id = ?
                     AND chunk_id IN (
                       SELECT id FROM content_chunks
                       WHERE platform = ? AND source_id = ?
                     )""",
                (profile.id, task.platform, task.source_id),
            )
            timestamp = now()
            self.database.connection.executemany(
                """INSERT INTO chunk_embeddings(
                       chunk_id, profile_id, segment_ordinal,
                       token_start, token_end, vector, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        chunk_id,
                        profile.id,
                        segment.ordinal,
                        segment.token_start,
                        segment.token_end,
                        blob,
                        timestamp,
                    )
                    for (chunk_id, segment), blob in zip(payload, blobs, strict=True)
                ],
            )
            self.queue.complete(task.id)

    def reindex_missing(self, force: bool = False) -> int:
        profile = self.profiles.active()
        if profile is None:
            return 0
        rows = self.database.connection.execute(
            """SELECT platform, source_id, index_input_hash
               FROM items WHERE access_status = 'available'
                 AND index_input_hash IS NOT NULL
               ORDER BY platform, source_id"""
        ).fetchall()
        if not rows and not force:
            return 0
        provider = self._get_provider()
        self._validate_provider(profile, provider)
        plans: list[_ReindexPlan] = []
        for row in rows:
            platform = str(row["platform"])
            source_id = str(row["source_id"])
            index_hash = str(row["index_input_hash"])
            task_hash = embedding_task_input_hash(profile.id, index_hash)
            index_task = self.database.connection.execute(
                """SELECT status FROM enrichment_tasks
                   WHERE platform=? AND source_id=? AND kind='index_content'
                     AND input_hash=?
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (platform, source_id, index_hash),
            ).fetchone()
            if index_task is None or str(index_task["status"]) != "completed":
                continue
            chunks = self.database.connection.execute(
                """SELECT id, text FROM content_chunks
                   WHERE platform=? AND source_id=? AND input_hash=?
                   ORDER BY ordinal, id""",
                (platform, source_id, index_hash),
            ).fetchall()
            expected_segments = {
                (int(chunk["id"]), segment.ordinal, segment.token_start, segment.token_end)
                for chunk in chunks
                for segment in self._segments_for_text(provider, profile, str(chunk["text"]))
            }
            plans.append(
                _ReindexPlan(
                    platform=platform,
                    source_id=source_id,
                    index_hash=index_hash,
                    task_hash=task_hash,
                    chunk_ids=frozenset(int(chunk["id"]) for chunk in chunks),
                    expected_segments=frozenset(expected_segments),
                )
            )

        if force:
            return self._force_reindex(profile, plans)

        enqueued = 0
        for plan in plans:
            with self.database.transaction():
                current_profile = self.profiles.active()
                if current_profile is None or current_profile.id != profile.id:
                    continue
                current_item = self.database.connection.execute(
                    """SELECT index_input_hash FROM items
                       WHERE platform=? AND source_id=? AND access_status='available'""",
                    (plan.platform, plan.source_id),
                ).fetchone()
                if current_item is None or str(current_item["index_input_hash"]) != plan.index_hash:
                    continue
                current_chunk_ids = {
                    int(current_chunk["id"])
                    for current_chunk in self.database.connection.execute(
                        """SELECT id FROM content_chunks
                           WHERE platform=? AND source_id=? AND input_hash=?""",
                        (plan.platform, plan.source_id, plan.index_hash),
                    ).fetchall()
                }
                if current_chunk_ids != plan.chunk_ids:
                    continue
                index_task = self.database.connection.execute(
                    """SELECT status FROM enrichment_tasks
                       WHERE platform=? AND source_id=? AND kind='index_content'
                         AND input_hash=?
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (plan.platform, plan.source_id, plan.index_hash),
                ).fetchone()
                if index_task is None or str(index_task["status"]) != "completed":
                    continue
                task_row = self.database.connection.execute(
                    """SELECT id, status FROM enrichment_tasks
                       WHERE platform=? AND source_id=? AND kind='embed_content'
                         AND input_hash=?""",
                    (plan.platform, plan.source_id, plan.task_hash),
                ).fetchone()
                status = None if task_row is None else str(task_row["status"])
                actual_segments = {
                    (
                        int(segment["chunk_id"]),
                        int(segment["segment_ordinal"]),
                        int(segment["token_start"]),
                        int(segment["token_end"]),
                    )
                    for segment in self.database.connection.execute(
                        """SELECT e.chunk_id, e.segment_ordinal,
                                  e.token_start, e.token_end
                           FROM chunk_embeddings e
                           JOIN content_chunks c ON c.id=e.chunk_id
                           WHERE e.profile_id=? AND c.platform=? AND c.source_id=?
                             AND c.input_hash=?""",
                        (profile.id, plan.platform, plan.source_id, plan.index_hash),
                    ).fetchall()
                }
                needs = status != "completed" or actual_segments != plan.expected_segments
                if not needs:
                    continue
                if status == "running":
                    continue
                task_id = self.queue.enqueue(
                    plan.platform,
                    plan.source_id,
                    "embed_content",
                    plan.task_hash,
                )
                if status == "completed":
                    self.database.connection.execute(
                        """UPDATE enrichment_tasks SET status='pending', error=NULL,
                                  updated_at=? WHERE id=? AND status='completed'""",
                        (now(), task_id),
                    )
                enqueued += 1
        return enqueued

    def _force_reindex(self, profile: EmbeddingProfile, plans: Sequence[_ReindexPlan]) -> int:
        """Atomically invalidate one active profile and requeue eligible items."""
        enqueued = 0
        with self.database.transaction():
            current_profile = self.profiles.active()
            if current_profile is None or current_profile.id != profile.id:
                raise ValueError("active embedding profile changed during force reconciliation")
            self.database.connection.execute(
                "DELETE FROM chunk_embeddings WHERE profile_id=?", (profile.id,)
            )
            for plan in plans:
                current_item = self.database.connection.execute(
                    """SELECT index_input_hash FROM items
                       WHERE platform=? AND source_id=? AND access_status='available'""",
                    (plan.platform, plan.source_id),
                ).fetchone()
                if current_item is None or str(current_item["index_input_hash"]) != plan.index_hash:
                    raise ValueError("item changed during force reconciliation")
                current_chunk_ids = frozenset(
                    int(row["id"])
                    for row in self.database.connection.execute(
                        """SELECT id FROM content_chunks
                           WHERE platform=? AND source_id=? AND input_hash=?""",
                        (plan.platform, plan.source_id, plan.index_hash),
                    ).fetchall()
                )
                if current_chunk_ids != plan.chunk_ids:
                    raise ValueError("content chunks changed during force reconciliation")
                index_task = self.database.connection.execute(
                    """SELECT status FROM enrichment_tasks
                       WHERE platform=? AND source_id=? AND kind='index_content'
                         AND input_hash=?
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (plan.platform, plan.source_id, plan.index_hash),
                ).fetchone()
                if index_task is None or str(index_task["status"]) != "completed":
                    raise ValueError("index task changed during force reconciliation")
                task_row = self.database.connection.execute(
                    """SELECT id, status FROM enrichment_tasks
                       WHERE platform=? AND source_id=? AND kind='embed_content'
                         AND input_hash=?""",
                    (plan.platform, plan.source_id, plan.task_hash),
                ).fetchone()
                status = None if task_row is None else str(task_row["status"])
                if status == "running":
                    continue
                task_id = self.queue.enqueue(
                    plan.platform,
                    plan.source_id,
                    "embed_content",
                    plan.task_hash,
                )
                if status == "completed":
                    cursor = self.database.connection.execute(
                        """UPDATE enrichment_tasks SET status='pending', error=NULL,
                                  updated_at=? WHERE id=? AND status='completed'""",
                        (now(), task_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("embedding task changed during force reconciliation")
                enqueued += 1
        return enqueued

    def _get_provider(self) -> _Provider:
        if self.provider_loader is not None:
            return self.provider_loader()
        if self.provider is None:
            raise RuntimeError("embedding provider is not configured")
        return self.provider

    def _segments_for_text(
        self, provider: _Provider, profile: EmbeddingProfile, text: str
    ) -> Sequence[SemanticSegment]:
        if self.segmenter is not None:
            return self.segmenter(
                text,
                segment_tokens=profile.segment_tokens,
                overlap_tokens=profile.overlap_tokens,
            )
        token_ids = tuple(int(token) for token in provider.tokenize(text))
        string_tokens = tuple(str(token) for token in token_ids)
        return split_segments(
            string_tokens,
            segment_tokens=profile.segment_tokens,
            overlap_tokens=profile.overlap_tokens,
            token_to_text=lambda window: provider.decode_tokens(
                tuple(int(token) for token in window)
            ),
        )

    @staticmethod
    def _validate_provider(profile: EmbeddingProfile, provider: _Provider) -> None:
        if (
            provider.name != profile.provider
            or provider.version != profile.provider_version
            or provider.dimensions != profile.dimensions
        ):
            raise ValueError("embedding provider does not match the active profile")

    def _current_item_state(
        self, task: EnrichmentTask, profile: EmbeddingProfile | None
    ) -> tuple[EmbeddingProfile, str, list[Any]] | None:
        if profile is None:
            return None
        row = self.database.connection.execute(
            """SELECT index_input_hash FROM items
               WHERE platform = ? AND source_id = ? AND access_status = 'available'""",
            (task.platform, task.source_id),
        ).fetchone()
        if row is None or row["index_input_hash"] is None:
            return None
        index_hash = str(row["index_input_hash"])
        if embedding_task_input_hash(profile.id, index_hash) != task.input_hash:
            return None
        chunks = self.database.connection.execute(
            """SELECT id, text FROM content_chunks
               WHERE platform = ? AND source_id = ? AND input_hash = ?
               ORDER BY ordinal, id""",
            (task.platform, task.source_id, index_hash),
        ).fetchall()
        index_task = self.database.connection.execute(
            """SELECT status FROM enrichment_tasks
               WHERE platform = ? AND source_id = ? AND kind = 'index_content'
                 AND input_hash = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (task.platform, task.source_id, index_hash),
        ).fetchone()
        if index_task is None or str(index_task["status"]) != "completed":
            return None
        return profile, index_hash, chunks


__all__ = ["EmbeddedTask", "EmbeddingIndexer"]
