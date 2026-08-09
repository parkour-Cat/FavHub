"""Read-only local retrieval over the durable content index."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from favhub.database import Database
from favhub.domain import SUPPORTED_PLATFORMS
from favhub.embedding import decode_float32, validate_embeddings
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.embedding_runtime import (
    EmbeddingDependencyUnavailableError,
    EmbeddingModelCacheMissingError,
    EmbeddingRuntime,
    EmbeddingRuntimeError,
)
from favhub.fts_text import fts_text
from favhub.hybrid_search import (
    FusedCandidate,
    LexicalCandidate,
    SemanticCandidate,
    candidate_pool_size,
    published_at_sort_key,
    reciprocal_rank_fusion,
)
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.retrieval_results import ItemEvidence, classify_evidence, group_candidates

MAX_CANDIDATE_POOL_SIZE = candidate_pool_size(50)


class RetrievalMode(StrEnum):
    AUTO = "auto"
    FTS = "fts"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    platforms: tuple[str, ...] | None = None
    content_types: tuple[str, ...] | None = None
    published_since: datetime | str | None = None
    published_until: datetime | str | None = None
    favorited_since: datetime | str | None = None
    favorited_until: datetime | str | None = None
    # The user's own folder names. Their reliability varies enormously and the
    # caller is the one who knows which way: a one-click default folder holds
    # whatever was saved without a thought, while putting something in a named
    # folder took a deliberate choice. Offered as a filter rather than folded
    # into ranking, so nothing is buried without the caller asking for it.
    collections: tuple[str, ...] | None = None
    limit: int = 10


@dataclass(frozen=True, slots=True)
class SupportingChunk:
    citation_id: str
    excerpt: str
    local_path: str
    line_start: int
    line_end: int
    timestamp: str | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    platform: str
    source_id: str
    title: str
    author: str | None
    published_at: str
    content_type: str
    excerpt: str
    canonical_url: str
    local_path: str
    line_start: int
    line_end: int
    timestamp: str | None = None
    citation_id: str = ""
    match_sources: tuple[str, ...] = ("fts",)
    fts_rank: int | None = None
    cosine_similarity: float | None = None
    rrf_score: float | None = None
    evidence_level: str = "body"
    evidence_warning: str | None = None
    supporting_chunks: tuple[SupportingChunk, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrievalStatus:
    indexed_items: int
    indexed_chunks: int
    pending_index_tasks: int
    failed_index_tasks: int
    index_state: str = "available"
    # Items the platform no longer serves. Reported because they are otherwise
    # invisible: every search path filters them out, so a library steadily
    # rotting away looks exactly like a healthy one that is merely smaller.
    unavailable_items: int = 0

    def as_dict(self) -> dict[str, int | str]:
        return {
            "indexed_items": self.indexed_items,
            "indexed_chunks": self.indexed_chunks,
            "pending_index_tasks": self.pending_index_tasks,
            "failed_index_tasks": self.failed_index_tasks,
            "index_state": self.index_state,
            "unavailable_items": self.unavailable_items,
        }


@dataclass(frozen=True, slots=True)
class SearchResponse:
    found: bool
    hits: tuple[SearchHit, ...] = ()
    reason: str | None = None
    index_summary: dict[str, int | str] = field(default_factory=dict)
    total_returned: int = 0
    retrieval_mode: str = "fts"
    vector_warning: str | None = None
    embedding_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def not_found_reason(self) -> str | None:
        """An explicit alias for the backwards-compatible ``reason`` field."""
        return self.reason


@dataclass(frozen=True, slots=True)
class GetItemRequest:
    platform: str
    source_id: str
    include_content: bool = True

    def __post_init__(self) -> None:
        if type(self.include_content) is not bool:
            raise ValueError("include_content must be a boolean")


@dataclass(frozen=True, slots=True)
class ItemResponse:
    platform: str
    source_id: str
    source: dict[str, Any]
    files: tuple[str, ...]
    system_content: dict[str, str] = field(default_factory=dict)
    access_status: str = "available"
    content_type: str = "text"


@dataclass(frozen=True, slots=True)
class ReindexRequest:
    force: bool = False


@dataclass(frozen=True, slots=True)
class ReindexResponse:
    enqueued: int


@dataclass(frozen=True, slots=True)
class _StatusSummary:
    status: RetrievalStatus


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _semantic_candidate_key(candidate: SemanticCandidate) -> tuple[Any, ...]:
    return (
        -candidate.similarity,
        published_at_sort_key(candidate.published_at),
        candidate.platform,
        candidate.source_id,
        candidate.ordinal,
        candidate.citation_id,
    )


def _date_value(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("date filters must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("date filters must be ISO-8601 values")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("date filters must be valid ISO-8601 values") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("date filters must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _filter_values(
    name: str, values: tuple[str, ...] | None, *, supported: frozenset[str] | None = None
) -> tuple[str, ...] | None:
    if values is None:
        return None
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{name} must be a non-empty tuple of strings")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{name} must contain non-blank strings")
    if supported is not None and any(value not in supported for value in values):
        raise ValueError(f"{name} contains an unsupported value")
    return values


def _registered_item_directory(
    store: ItemStore, platform: str, source_id: str, item_dir: str
) -> tuple[Path, str]:
    expected = store._item_directory(platform, source_id)
    registered = Path(item_dir)
    root = store.items_root.resolve()
    try:
        relative = registered.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("registered item directory is outside the item store") from exc
    if registered.resolve() != expected.resolve():
        raise ValueError("registered item directory does not match the item identity")
    return expected, relative.as_posix()


@dataclass(frozen=True, slots=True)
class CollectionSummary:
    platform: str
    name: str
    items: int

    def as_dict(self) -> dict[str, int | str]:
        return {"platform": self.platform, "name": self.name, "items": self.items}


@dataclass(frozen=True, slots=True)
class PlatformSummary:
    platform: str
    items: int
    unfiled: int

    def as_dict(self) -> dict[str, int | str]:
        return {"platform": self.platform, "items": self.items, "unfiled": self.unfiled}


@dataclass(frozen=True, slots=True)
class CollectionMap:
    collections: tuple[CollectionSummary, ...]
    platforms: tuple[PlatformSummary, ...]

    def as_dict(self) -> dict[str, list[dict[str, int | str]]]:
        return {
            "collections": [folder.as_dict() for folder in self.collections],
            "platforms": [platform.as_dict() for platform in self.platforms],
        }


def _summarize_folders(database: Database) -> tuple[CollectionSummary, ...]:
    rows = database.connection.execute(
        """SELECT folder.platform, folder.name, COUNT(*) AS items
             FROM item_collections AS folder
             JOIN items ON items.platform = folder.platform
                       AND items.source_id = folder.source_id
            WHERE items.access_status = 'available'
            GROUP BY folder.platform, folder.name
            ORDER BY items DESC, folder.platform, folder.name"""
    ).fetchall()
    return tuple(
        CollectionSummary(str(row["platform"]), str(row["name"]), int(row["items"])) for row in rows
    )


def _summarize_platform_coverage(database: Database) -> tuple[PlatformSummary, ...]:
    rows = database.connection.execute(
        """SELECT items.platform,
                  COUNT(*) AS items,
                  SUM(CASE WHEN NOT EXISTS (
                        SELECT 1 FROM item_collections AS folder
                        WHERE folder.platform = items.platform
                          AND folder.source_id = items.source_id
                      ) THEN 1 ELSE 0 END) AS unfiled
             FROM items
            WHERE items.access_status = 'available'
            GROUP BY items.platform
            ORDER BY items DESC, items.platform"""
    ).fetchall()
    return tuple(
        PlatformSummary(str(row["platform"]), int(row["items"]), int(row["unfiled"]))
        for row in rows
    )


def summarize_collections(database: Database) -> CollectionMap:
    """A map of what the library's owner actually cares about.

    This is what makes the library consultable while working rather than only
    when asked: knowing which folders exist answers "is there anything of
    theirs worth reading here" without searching for it first. A named folder
    was a deliberate choice; a platform's default folder collects one-click
    saves, so the count matters as much as the name.

    The folder list alone is a map with holes in it. Folders are a thing some
    platforms have — GitHub stars and X bookmarks are flat lists, so every item
    from them is unfiled and no folder name will ever hint at what they hold.
    Reading the folders as the whole library would make a third of it invisible
    exactly when it is most relevant, which is why the platform counts ship
    beside them and name their own blind spot.
    """
    return CollectionMap(_summarize_folders(database), _summarize_platform_coverage(database))


def summarize_index(database: Database, store: ItemStore) -> RetrievalStatus:
    """Summarize the current durable index state without reading item files.

    Failed work overlaps pending work because the durable queue records a failed
    attempt as ``status='pending'`` with a non-null error for retry.
    """
    conn = database.connection
    current_task = """EXISTS (
        SELECT 1 FROM enrichment_tasks AS task
        WHERE task.platform = items.platform
          AND task.source_id = items.source_id
          AND task.kind = 'index_content'
          AND task.input_hash = items.index_input_hash
          AND task.status = 'completed'
    )"""
    indexed_items = int(
        conn.execute(
            f"""SELECT COUNT(*) FROM items
                WHERE access_status='available'
                  AND index_input_hash IS NOT NULL
                  AND {current_task}"""
        ).fetchone()[0]
    )
    indexed_chunks = int(
        conn.execute(
            f"""SELECT COUNT(*)
                FROM content_chunks
                JOIN items
                  ON items.platform = content_chunks.platform
                 AND items.source_id = content_chunks.source_id
                WHERE items.access_status='available'
                  AND items.index_input_hash IS NOT NULL
                  AND content_chunks.input_hash = items.index_input_hash
                  AND {current_task}"""
        ).fetchone()[0]
    )
    pending = int(
        conn.execute(
            """SELECT COUNT(*) FROM enrichment_tasks
               WHERE kind='index_content' AND status IN ('pending', 'running')"""
        ).fetchone()[0]
    )
    failed = int(
        conn.execute(
            """SELECT COUNT(*) FROM enrichment_tasks
               WHERE kind='index_content' AND error IS NOT NULL"""
        ).fetchone()[0]
    )
    unavailable = int(
        conn.execute("SELECT COUNT(*) FROM items WHERE access_status <> 'available'").fetchone()[0]
    )
    index_state = "available" if database.is_fts_available() else "index_unavailable"
    return RetrievalStatus(indexed_items, indexed_chunks, pending, failed, index_state, unavailable)


class RetrievalService:
    def __init__(
        self,
        database: Database,
        store: ItemStore,
        indexer: ContentIndexer,
        embedding_profiles: EmbeddingProfileStore | None = None,
        embedding_runtime: EmbeddingRuntime | None = None,
    ) -> None:
        self.database = database
        self.store = store
        self.indexer = indexer
        self.embedding_profiles = embedding_profiles
        self.embedding_runtime = embedding_runtime

    def status(self) -> RetrievalStatus:
        return summarize_index(self.database, self.store)

    def collections(self) -> CollectionMap:
        return summarize_collections(self.database)

    def search(
        self,
        request: SearchRequest,
        *,
        mode: RetrievalMode | str = RetrievalMode.AUTO,
    ) -> SearchResponse:
        try:
            requested_mode = RetrievalMode(mode)
        except (TypeError, ValueError) as exc:
            raise ValueError("retrieval mode must be auto, fts, or hybrid") from exc
        if not isinstance(request.query, str) or not request.query.strip():
            raise ValueError("query must not be blank")
        if (
            not isinstance(request.limit, int)
            or isinstance(request.limit, bool)
            or not 1 <= request.limit <= 50
        ):
            raise ValueError("limit must be between 1 and 50")
        platforms = _filter_values("platforms", request.platforms, supported=SUPPORTED_PLATFORMS)
        content_types = _filter_values("content_types", request.content_types)
        collections = _filter_values("collections", request.collections)
        since = _date_value(request.published_since)
        until = _date_value(request.published_until)
        if since is not None and until is not None and since > until:
            raise ValueError("published_since must not be later than published_until")
        favorited_since = _date_value(request.favorited_since)
        favorited_until = _date_value(request.favorited_until)
        if (
            favorited_since is not None
            and favorited_until is not None
            and favorited_since > favorited_until
        ):
            raise ValueError("favorited_since must not be later than favorited_until")
        status = self.status()
        summary = status.as_dict()
        if status.index_state == "index_unavailable":
            raise RuntimeError("index_unavailable")
        tokens = _TOKEN_RE.findall(request.query)
        initial_pool = candidate_pool_size(request.limit)

        # Quote every token and construct the OR expression ourselves.  User text
        # is never interpolated into SQL or FTS operators.  CJK tokens expand
        # into their bigram phrase so they match the fts_text shadow column
        # with exact substring semantics.
        match_query = " OR ".join(
            '"' + fts_text(token).replace('"', '""') + '"' for token in tokens
        )
        clauses = [
            "content_chunks_fts MATCH ?",
            "items.access_status = 'available'",
            "content_chunks.input_hash = items.index_input_hash",
            """EXISTS (
                SELECT 1 FROM enrichment_tasks AS task
                WHERE task.platform = items.platform
                  AND task.source_id = items.source_id
                  AND task.kind = 'index_content'
                  AND task.input_hash = items.index_input_hash
                  AND task.status = 'completed'
            )""",
        ]
        params: list[Any] = [match_query]
        if platforms is not None:
            placeholders = ",".join("?" for _ in platforms)
            clauses.append(f"items.platform IN ({placeholders})")
            params.extend(platforms)
        if content_types is not None:
            placeholders = ",".join("?" for _ in content_types)
            clauses.append(f"items.content_type IN ({placeholders})")
            params.extend(content_types)
        if collections is not None:
            placeholders = ",".join("?" for _ in collections)
            clauses.append(
                f"""EXISTS (
                    SELECT 1 FROM item_collections AS folder
                    WHERE folder.platform = items.platform
                      AND folder.source_id = items.source_id
                      AND folder.name IN ({placeholders})
                )"""
            )
            params.extend(collections)
        if since is not None:
            clauses.append("julianday(items.published_at) >= julianday(?)")
            params.append(since)
        if until is not None:
            clauses.append("julianday(items.published_at) <= julianday(?)")
            params.append(until)
        if favorited_since is not None:
            clauses.append(
                "items.favorited_at IS NOT NULL AND julianday(items.favorited_at) >= julianday(?)"
            )
            params.append(favorited_since)
        if favorited_until is not None:
            clauses.append(
                "items.favorited_at IS NOT NULL AND julianday(items.favorited_at) <= julianday(?)"
            )
            params.append(favorited_until)
        search_sql = f"""SELECT content_chunks.platform, content_chunks.source_id,
                       content_chunks.ordinal, content_chunks.relative_path,
                       content_chunks.line_start, content_chunks.line_end,
                       content_chunks.text, content_chunks.input_hash,
                       items.published_at, items.content_type, items.access_status,
                       items.item_dir,
                       bm25(content_chunks_fts) AS rank,
                       snippet(content_chunks_fts, 0, '', '', '…', 24) AS excerpt
                FROM content_chunks_fts
                JOIN content_chunks ON content_chunks_fts.rowid = content_chunks.id
                JOIN items ON items.platform = content_chunks.platform
                          AND items.source_id = content_chunks.source_id
                WHERE {" AND ".join(clauses)}
                ORDER BY rank ASC, items.published_at DESC,
                         content_chunks.platform ASC, content_chunks.source_id ASC,
                         content_chunks.ordinal ASC
                LIMIT ? OFFSET ?"""
        lexical: list[LexicalCandidate] = []
        lexical_offset = 0
        lexical_exhausted = not tokens

        def load_lexical_candidates(target: int) -> None:
            nonlocal lexical_exhausted, lexical_offset
            while not lexical_exhausted and len(lexical) < target:
                page_size = target - len(lexical)
                lexical_rows = self.database.connection.execute(
                    search_sql, (*params, page_size, lexical_offset)
                ).fetchall()
                for row in lexical_rows:
                    lexical.append(
                        LexicalCandidate(
                            self._citation(
                                str(row["platform"]),
                                str(row["source_id"]),
                                int(row["ordinal"]),
                            ),
                            len(lexical) + 1,
                            str(row["published_at"]),
                            str(row["platform"]),
                            str(row["source_id"]),
                            int(row["ordinal"]),
                            row,
                        )
                    )
                lexical_offset += len(lexical_rows)
                if len(lexical_rows) < page_size:
                    lexical_exhausted = True

        load_lexical_candidates(initial_pool)

        semantic: list[SemanticCandidate]
        warning: str | None
        embedding_summary: dict[str, Any]
        if requested_mode is RetrievalMode.FTS:
            semantic, warning, embedding_summary = [], None, self._embedding_summary()
        else:
            semantic, warning, embedding_summary = self._semantic_candidates(
                request,
                platforms,
                content_types,
                since,
                until,
                favorited_since,
                favorited_until,
                MAX_CANDIDATE_POOL_SIZE,
                collections=collections,
            )
            if requested_mode is RetrievalMode.HYBRID and (
                warning in {"embedding_unavailable", "query_embedding_failed"}
                or embedding_summary.get("state") in {"disabled", "unavailable"}
            ):
                raise RuntimeError("hybrid retrieval is unavailable")
        if not tokens and not semantic:
            if embedding_summary.get("pending_tasks") or embedding_summary.get("failed_tasks"):
                reason = "no matching content; embedding tasks are pending or failed"
            elif warning:
                reason = f"no matching content; {warning}"
            else:
                reason = "query contains no searchable tokens"
            return SearchResponse(
                False,
                reason=reason,
                index_summary=summary,
                vector_warning=warning,
                embedding_summary=embedding_summary,
            )
        item_cache: dict[tuple[str, str], tuple[Path, dict[str, Any]] | None] = {}
        chunks_by_citation: dict[str, SupportingChunk] = {}
        evidence_by_item: dict[tuple[str, str], ItemEvidence] = {}
        valid_by_citation: dict[str, FusedCandidate] = {}
        target_pool = initial_pool
        while True:
            load_lexical_candidates(target_pool)
            fused = reciprocal_rank_fusion(lexical, semantic, limit=target_pool)
            for candidate in fused:
                row = candidate.payload
                if row is None:
                    continue
                platform = str(row["platform"])
                source_id = str(row["source_id"])
                key = (platform, source_id)
                if key not in item_cache:
                    try:
                        directory, _ = _registered_item_directory(
                            self.store,
                            platform,
                            source_id,
                            str(row["item_dir"]),
                        )
                        source = self.store.read_source(platform, source_id)
                        item_cache[key] = None if source is None else (directory, source)
                    except (OSError, UnicodeError, ValueError):
                        item_cache[key] = None
                cached = item_cache[key]
                if cached is None:
                    continue
                directory, source = cached
                chunk = self._supporting_chunk(candidate, directory)
                if chunk is None:
                    continue
                chunks_by_citation[candidate.citation_id] = chunk
                if key not in evidence_by_item:
                    evidence_by_item[key] = classify_evidence(source)
                valid_by_citation[candidate.citation_id] = candidate

            valid_candidates = [
                valid_by_citation[candidate.citation_id]
                for candidate in fused
                if candidate.citation_id in valid_by_citation
            ]
            grouped_candidates = group_candidates(
                valid_candidates, evidence_by_item, limit=request.limit
            )
            if len(grouped_candidates) >= request.limit or target_pool >= MAX_CANDIDATE_POOL_SIZE:
                break
            target_pool = min(MAX_CANDIDATE_POOL_SIZE, max(target_pool + 1, target_pool * 2))

        hits: list[SearchHit] = []
        for grouped in grouped_candidates:
            primary = grouped.primary
            row = primary.payload
            if row is None:
                continue
            cached = item_cache.get((primary.platform, primary.source_id))
            if cached is None:
                continue
            _, source = cached
            primary_chunk = chunks_by_citation.get(primary.citation_id)
            if primary_chunk is None:
                continue
            supporting_chunks = tuple(
                chunk
                for candidate in grouped.supporting
                if (chunk := chunks_by_citation.get(candidate.citation_id)) is not None
            )
            hits.append(
                SearchHit(
                    platform=primary.platform,
                    source_id=primary.source_id,
                    title=str(source.get("title", "")),
                    author=source.get("author"),
                    published_at=str(row["published_at"]),
                    content_type=str(row["content_type"] or "text"),
                    excerpt=primary_chunk.excerpt,
                    canonical_url=str(source.get("canonical_url", "")),
                    local_path=primary_chunk.local_path,
                    line_start=primary_chunk.line_start,
                    line_end=primary_chunk.line_end,
                    timestamp=primary_chunk.timestamp,
                    citation_id=primary.citation_id,
                    match_sources=primary.match_sources,
                    fts_rank=primary.fts_rank,
                    cosine_similarity=primary.cosine_similarity,
                    rrf_score=primary.rrf_score,
                    evidence_level=str(grouped.evidence.level),
                    evidence_warning=grouped.evidence.warning,
                    supporting_chunks=supporting_chunks,
                )
            )
        if hits:
            return SearchResponse(
                True,
                tuple(hits),
                index_summary=summary,
                total_returned=len(hits),
                retrieval_mode="hybrid" if semantic else "fts",
                vector_warning=warning,
                embedding_summary=embedding_summary,
            )
        pending_reason = "index contains no matching content"
        if summary["pending_index_tasks"] or summary["failed_index_tasks"]:
            pending_reason = "no matching content; index tasks are pending or failed"
        elif embedding_summary.get("pending_tasks") or embedding_summary.get("failed_tasks"):
            pending_reason = "no matching content; embedding tasks are pending or failed"
        elif warning:
            pending_reason = f"no matching content; {warning}"
        return SearchResponse(
            False,
            reason=pending_reason,
            index_summary=summary,
            retrieval_mode="hybrid" if semantic else "fts",
            vector_warning=warning,
            embedding_summary=embedding_summary,
        )

    def _embedding_ready(self) -> bool:
        return self.embedding_profiles is not None and self.embedding_profiles.active() is not None

    @staticmethod
    def _citation(platform: str, source_id: str, ordinal: int) -> str:
        return f"favhub:{platform}/{source_id}#chunk-{ordinal}"

    def _supporting_chunk(
        self, candidate: FusedCandidate, directory: Path
    ) -> SupportingChunk | None:
        row = candidate.payload
        if row is None:
            return None
        try:
            relative_path = PurePosixPath(str(row["relative_path"]))
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ValueError("indexed content path is unsafe")
            local_file = directory.joinpath(*relative_path.parts).resolve()
            local_file.relative_to(directory.resolve())
            local_path = local_file.relative_to(self.store.items_root.parent.resolve()).as_posix()
            row_keys = row.keys()
            excerpt = row["excerpt"] if "excerpt" in row_keys and row["excerpt"] else row["text"]
            timestamp = row["timestamp"] if "timestamp" in row_keys else None
            return SupportingChunk(
                citation_id=candidate.citation_id,
                excerpt=str(excerpt),
                local_path=local_path,
                line_start=int(row["line_start"]),
                line_end=int(row["line_end"]),
                timestamp=None if timestamp is None else str(timestamp),
            )
        except (KeyError, OSError, TypeError, ValueError):
            return None

    def _semantic_candidates(
        self,
        request: SearchRequest,
        platforms: tuple[str, ...] | None,
        content_types: tuple[str, ...] | None,
        since: str | None,
        until: str | None,
        favorited_since: str | None,
        favorited_until: str | None,
        pool: int,
        collections: tuple[str, ...] | None = None,
    ) -> tuple[list[SemanticCandidate], str | None, dict[str, Any]]:
        if self.embedding_profiles is None or self.embedding_runtime is None:
            return [], None, {"state": "disabled"}
        profile = self.embedding_profiles.active()
        if profile is None:
            return [], None, {"state": "disabled"}
        try:
            provider = self.embedding_runtime.load_active(local_only=True)
        except (EmbeddingRuntimeError, OSError, ValueError):
            return [], "embedding_unavailable", self._embedding_summary(unavailable=True)
        try:
            vectors = validate_embeddings(
                provider,
                (request.query,),
                provider.embed_queries((request.query,)),
            )
            import numpy as np

            query = np.asarray(vectors[0], dtype=np.float32)
            norm = float(np.linalg.norm(query))
            if norm == 0:
                raise ValueError("query embedding is zero")
            query /= norm
        except (ImportError, EmbeddingDependencyUnavailableError, EmbeddingModelCacheMissingError):
            return [], "embedding_unavailable", self._embedding_summary(unavailable=True)
        except Exception:
            return [], "query_embedding_failed", self._embedding_summary(unavailable=True)

        clauses = [
            "e.profile_id = ?",
            "i.access_status = 'available'",
            "c.input_hash = i.index_input_hash",
            "EXISTS (SELECT 1 FROM enrichment_tasks t WHERE "
            "t.platform=i.platform AND t.source_id=i.source_id "
            "AND t.kind='index_content' AND t.input_hash=i.index_input_hash "
            "AND t.status='completed')",
            "EXISTS (SELECT 1 FROM enrichment_tasks t WHERE "
            "t.platform=i.platform AND t.source_id=i.source_id "
            "AND t.kind='embed_content' AND t.status='completed')",
        ]
        params: list[Any] = [profile.id]
        if platforms is not None:
            clauses.append("i.platform IN (" + ",".join("?" for _ in platforms) + ")")
            params.extend(platforms)
        if content_types is not None:
            clauses.append("i.content_type IN (" + ",".join("?" for _ in content_types) + ")")
            params.extend(content_types)
        if collections is not None:
            # The same restriction as the lexical half. A filter honoured by
            # only one arm of a hybrid search is worse than none: results would
            # leak from outside the folder the caller asked for, and only
            # sometimes.
            clauses.append(
                "EXISTS (SELECT 1 FROM item_collections f WHERE f.platform = i.platform "
                "AND f.source_id = i.source_id AND f.name IN ("
                + ",".join("?" for _ in collections)
                + "))"
            )
            params.extend(collections)
        if since is not None:
            clauses.append("julianday(i.published_at) >= julianday(?)")
            params.append(since)
        if until is not None:
            clauses.append("julianday(i.published_at) <= julianday(?)")
            params.append(until)
        if favorited_since is not None:
            clauses.append(
                "i.favorited_at IS NOT NULL AND julianday(i.favorited_at) >= julianday(?)"
            )
            params.append(favorited_since)
        if favorited_until is not None:
            clauses.append(
                "i.favorited_at IS NOT NULL AND julianday(i.favorited_at) <= julianday(?)"
            )
            params.append(favorited_until)
        sql = f"""SELECT e.vector, c.id, c.platform, c.source_id, c.ordinal,
                          c.relative_path, c.line_start, c.line_end, c.text,
                          i.published_at, i.content_type, i.item_dir,
                          i.index_input_hash, t.input_hash AS embed_task_hash
                   FROM chunk_embeddings e JOIN content_chunks c ON c.id=e.chunk_id
                   JOIN items i ON i.platform=c.platform AND i.source_id=c.source_id
                   JOIN enrichment_tasks t ON t.platform=i.platform AND t.source_id=i.source_id
                                            AND t.kind='embed_content' AND t.status='completed'
                   WHERE {" AND ".join(clauses)}"""
        cursor = self.database.connection.execute(sql, tuple(params))
        collapsed: dict[str, SemanticCandidate] = {}
        corrupt_vectors = 0
        while True:
            batch_rows = cursor.fetchmany(256)
            if not batch_rows:
                break
            valid_rows: list[Any] = []
            vectors_for_batch: list[tuple[float, ...]] = []
            for row in batch_rows:
                if str(row["embed_task_hash"]) != embedding_task_input_hash(
                    profile.id, str(row["index_input_hash"])
                ):
                    continue
                try:
                    values = decode_float32(bytes(row["vector"]), dimensions=profile.dimensions)
                except (TypeError, ValueError, FloatingPointError):
                    corrupt_vectors += 1
                    continue
                valid_rows.append(row)
                vectors_for_batch.append(values)
            if not vectors_for_batch:
                continue
            scores = np.asarray(vectors_for_batch, dtype=np.float32).dot(query)
            for row, score in zip(valid_rows, scores, strict=True):
                platform = str(row["platform"])
                source_id = str(row["source_id"])
                ordinal = int(row["ordinal"])
                citation = self._citation(platform, source_id, ordinal)
                # Do not retain the vector BLOB in candidate payloads.  The
                # cursor is streamed in bounded batches, and the payload only
                # needs fields used later to build SearchHit.
                payload = {
                    key: row[key]
                    for key in (
                        "id",
                        "platform",
                        "source_id",
                        "ordinal",
                        "relative_path",
                        "line_start",
                        "line_end",
                        "text",
                        "published_at",
                        "content_type",
                        "item_dir",
                        "index_input_hash",
                        "embed_task_hash",
                    )
                }
                candidate = SemanticCandidate(
                    citation,
                    float(score),
                    str(row["published_at"]),
                    platform,
                    source_id,
                    ordinal,
                    payload,
                )
                previous = collapsed.get(citation)
                if previous is None or _semantic_candidate_key(candidate) < _semantic_candidate_key(
                    previous
                ):
                    collapsed[citation] = candidate
        collapsed_candidates = sorted(collapsed.values(), key=_semantic_candidate_key)
        warning = "corrupt_vectors" if corrupt_vectors else None
        summary = self._embedding_summary()
        if corrupt_vectors:
            summary["corrupt_vectors"] = max(
                corrupt_vectors, int(summary.get("corrupt_vectors", 0))
            )
            summary["state"] = "degraded"
        return collapsed_candidates[:pool], warning, summary

    def _embedding_summary(self, *, unavailable: bool = False) -> dict[str, Any]:
        if self.embedding_profiles is None:
            return {"state": "disabled"}
        try:
            summary = self.embedding_profiles.summary()
            cache_check = getattr(self.embedding_runtime, "cache_available", None)
            cache_unavailable = callable(cache_check) and not bool(cache_check())
            return {
                "state": ("unavailable" if unavailable or cache_unavailable else summary.state),
                "active_profile": summary.active_profile_metadata,
                "current_chunks": summary.current_chunks,
                "embedded_chunks": summary.embedded_chunks,
                "pending_tasks": summary.pending_tasks,
                "failed_tasks": summary.failed_tasks,
                "corrupt_vectors": summary.corrupt_vectors,
            }
        except Exception:
            return {"state": "unavailable"}

    def embedding_summary(self) -> dict[str, Any]:
        return self._embedding_summary()

    def get_item(self, request: GetItemRequest) -> ItemResponse:
        row = self.database.connection.execute(
            """SELECT item_dir, access_status, content_type FROM items
               WHERE platform=? AND source_id=?""",
            (request.platform, request.source_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"item not found: {request.platform}/{request.source_id}")
        directory, _ = _registered_item_directory(
            self.store, request.platform, request.source_id, str(row["item_dir"])
        )
        source = self.store.read_source(request.platform, request.source_id)
        if source is None:
            raise KeyError(f"item not found: {request.platform}/{request.source_id}")
        entries = self.store.iter_index_markdown(request.platform, request.source_id)
        files = ["source.json"]
        if (directory / "content.md").exists():
            files.append("content.md")
        if (directory / "notes.md").exists():
            files.append("notes.md")
        files.extend(path for path, _ in entries if path not in files)
        content = dict(entries) if request.include_content else {}
        return ItemResponse(
            request.platform,
            request.source_id,
            source,
            tuple(sorted(set(files))),
            content,
            str(row["access_status"]),
            str(row["content_type"] or "text"),
        )

    def reindex(self, request: ReindexRequest) -> ReindexResponse:
        if not self.database.is_fts_available():
            self.database.repair_fts()
        return ReindexResponse(self.indexer.reindex_missing(force=request.force))


__all__ = [
    "GetItemRequest",
    "ItemResponse",
    "ReindexRequest",
    "ReindexResponse",
    "RetrievalMode",
    "RetrievalService",
    "RetrievalStatus",
    "SearchHit",
    "SearchRequest",
    "SearchResponse",
    "SupportingChunk",
    "summarize_index",
]
