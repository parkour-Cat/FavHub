"""Pure candidate fusion primitives for lexical and semantic retrieval."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any


@dataclass(frozen=True, slots=True)
class LexicalCandidate:
    citation_id: str
    rank: int
    published_at: str = ""
    platform: str = ""
    source_id: str = ""
    ordinal: int = 0
    payload: Any = None


@dataclass(frozen=True, slots=True)
class SemanticCandidate:
    citation_id: str
    similarity: float
    published_at: str = ""
    platform: str = ""
    source_id: str = ""
    ordinal: int = 0
    payload: Any = None


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    citation_id: str
    match_sources: tuple[str, ...]
    rrf_score: float
    fts_rank: int | None = None
    cosine_similarity: float | None = None
    published_at: str = ""
    platform: str = ""
    source_id: str = ""
    ordinal: int = 0
    payload: Any = None
    best_rank: int = 0


def candidate_pool_size(limit: int) -> int:
    """Return the bounded candidate pool used by both retrieval paths."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    return min(500, max(50, limit * 10))


def reciprocal_rank_fusion(
    lexical: Sequence[LexicalCandidate],
    semantic: Sequence[SemanticCandidate],
    *,
    limit: int,
    k: int = 60,
) -> tuple[FusedCandidate, ...]:
    """Fuse lexical and semantic candidates using equal-weight RRF.

    Semantic segments are collapsed to their highest cosine score before they
    receive a rank.  Citation identity is the stable join key, so one citation
    can only occupy one result position.
    """
    pool = candidate_pool_size(limit)
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")

    lexical_by_id: dict[str, LexicalCandidate] = {}
    for candidate in lexical:
        if candidate.citation_id not in lexical_by_id:
            lexical_by_id[candidate.citation_id] = candidate
            continue
        lexical_previous = lexical_by_id[candidate.citation_id]
        if _lexical_key(candidate) < _lexical_key(lexical_previous):
            lexical_by_id[candidate.citation_id] = candidate
    lexical_sorted = sorted(lexical_by_id.values(), key=_lexical_key)[:pool]

    semantic_by_id: dict[str, SemanticCandidate] = {}
    for semantic_candidate in semantic:
        if not isfinite(float(semantic_candidate.similarity)):
            continue
        semantic_previous = semantic_by_id.get(semantic_candidate.citation_id)
        if semantic_previous is None or _semantic_key(semantic_candidate) < _semantic_key(
            semantic_previous
        ):
            semantic_by_id[semantic_candidate.citation_id] = semantic_candidate
    semantic_sorted = sorted(semantic_by_id.values(), key=_semantic_key)[:pool]

    fused: dict[str, FusedCandidate] = {}
    for rank, candidate in enumerate(lexical_sorted, 1):
        fused[candidate.citation_id] = FusedCandidate(
            citation_id=candidate.citation_id,
            match_sources=("fts",),
            rrf_score=1.0 / (k + rank),
            best_rank=rank,
            fts_rank=rank,
            published_at=candidate.published_at,
            platform=candidate.platform,
            source_id=candidate.source_id,
            ordinal=candidate.ordinal,
            payload=candidate.payload,
        )
    for rank, semantic_candidate in enumerate(semantic_sorted, 1):
        existing = fused.get(semantic_candidate.citation_id)
        score = 1.0 / (k + rank)
        if existing is None:
            fused[semantic_candidate.citation_id] = FusedCandidate(
                citation_id=semantic_candidate.citation_id,
                match_sources=("vector",),
                rrf_score=score,
                best_rank=rank,
                cosine_similarity=float(semantic_candidate.similarity),
                published_at=semantic_candidate.published_at,
                platform=semantic_candidate.platform,
                source_id=semantic_candidate.source_id,
                ordinal=semantic_candidate.ordinal,
                payload=semantic_candidate.payload,
            )
        else:
            sources = tuple(sorted(set(existing.match_sources + ("vector",))))
            fused[semantic_candidate.citation_id] = FusedCandidate(
                citation_id=existing.citation_id,
                match_sources=sources,
                rrf_score=existing.rrf_score + score,
                best_rank=min(existing.best_rank, rank),
                fts_rank=existing.fts_rank,
                cosine_similarity=float(semantic_candidate.similarity),
                published_at=existing.published_at or semantic_candidate.published_at,
                platform=existing.platform or semantic_candidate.platform,
                source_id=existing.source_id or semantic_candidate.source_id,
                ordinal=existing.ordinal,
                payload=(
                    existing.payload if existing.payload is not None else semantic_candidate.payload
                ),
            )

    ordered = sorted(fused.values(), key=_fused_key)
    return tuple(ordered[:limit])


def _lexical_key(candidate: LexicalCandidate) -> tuple[Any, ...]:
    return (
        int(candidate.rank),
        published_at_sort_key(candidate.published_at),
        candidate.platform,
        candidate.source_id,
        int(candidate.ordinal),
        candidate.citation_id,
    )


def _semantic_key(candidate: SemanticCandidate) -> tuple[Any, ...]:
    return (
        -float(candidate.similarity),
        published_at_sort_key(candidate.published_at),
        candidate.platform,
        candidate.source_id,
        int(candidate.ordinal),
        candidate.citation_id,
    )


def _fused_key(candidate: FusedCandidate) -> tuple[Any, ...]:
    return (
        -candidate.rrf_score,
        candidate.best_rank or 10**9,
        candidate.fts_rank or 10**9,
        -(
            candidate.cosine_similarity
            if candidate.cosine_similarity is not None
            else float("-inf")
        ),
        published_at_sort_key(candidate.published_at),
        candidate.platform,
        candidate.source_id,
        candidate.ordinal,
        candidate.citation_id,
    )


def published_at_sort_key(value: str) -> tuple[int, float, str]:
    """Return a stable key for newest-first ordering of ISO timestamps.

    Stored timestamps are ISO-8601, but fractional seconds are optional.  A
    parsed instant avoids treating ``...00Z`` as newer than ``...00.1Z``.
    Invalid values remain deterministic and sort after valid timestamps.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return (0, -parsed.timestamp(), "")
    except (TypeError, ValueError, OverflowError):
        inverted = "".join(chr(0x10FFFF - ord(char)) for char in value)
        return (1, 0.0, inverted)


__all__ = [
    "FusedCandidate",
    "LexicalCandidate",
    "SemanticCandidate",
    "candidate_pool_size",
    "published_at_sort_key",
    "reciprocal_rank_fusion",
]
