"""Classify the durable evidence available for a retrieved item."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from numbers import Real
from typing import Any

from favhub.domain import SAFE_ID, WINDOWS_RESERVED_NAMES
from favhub.hybrid_search import FusedCandidate, published_at_sort_key

type ItemKey = tuple[str, str]


class EvidenceLevel(StrEnum):
    """The strongest durable evidence captured for an item."""

    TITLE_ONLY = "title_only"
    BODY = "body"
    TRANSCRIPT = "transcript"
    OCR = "ocr"
    MIXED = "mixed"


@dataclass(frozen=True, slots=True)
class ItemEvidence:
    """Evidence quality and its ranking adjustment."""

    level: EvidenceLevel
    sources: tuple[str, ...]
    rank_factor: float
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class GroupedCandidate:
    """One retrieved item with its primary and supporting chunks."""

    primary: FusedCandidate
    supporting: tuple[FusedCandidate, ...]
    evidence: ItemEvidence
    adjusted_rrf_score: float


_TITLE_ONLY_WARNING = (
    "Only title and metadata are available; no body, transcript, or OCR was captured."
)


def classify_evidence(item: Mapping[str, Any]) -> ItemEvidence:
    """Return evidence quality using only durable source text and captured text assets."""
    sources: list[str] = []
    if isinstance(item.get("body"), str) and item["body"].strip():
        sources.append("body")

    assets = item.get("assets")
    if isinstance(assets, list | tuple):
        for asset in assets:
            source = _asset_source(asset)
            if source is not None and source not in sources:
                sources.append(source)

    evidence_sources = tuple(sources)
    if not evidence_sources:
        return ItemEvidence(
            level=EvidenceLevel.TITLE_ONLY,
            sources=evidence_sources,
            rank_factor=0.75,
            warning=_TITLE_ONLY_WARNING,
        )

    if len(evidence_sources) > 1:
        return ItemEvidence(EvidenceLevel.MIXED, evidence_sources, 1.0)

    source = evidence_sources[0]
    if source == "body":
        return ItemEvidence(EvidenceLevel.BODY, evidence_sources, 0.95)
    if source == "transcript":
        return ItemEvidence(EvidenceLevel.TRANSCRIPT, evidence_sources, 1.0)
    return ItemEvidence(EvidenceLevel.OCR, evidence_sources, 1.0)


def group_candidates(
    candidates: Sequence[FusedCandidate],
    evidence_by_item: Mapping[ItemKey, ItemEvidence],
    *,
    limit: int,
    max_supporting: int = 3,
) -> tuple[GroupedCandidate, ...]:
    """Collapse chunk-level results, normalizing each item's order and citations.

    Each item selects its primary and supporting chunks by fused relevance rather
    than caller order.  Duplicate citation IDs are collapsed to their best
    candidate before supporting chunks are bounded.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if (
        isinstance(max_supporting, bool)
        or not isinstance(max_supporting, int)
        or max_supporting < 0
    ):
        raise ValueError("max_supporting must be a non-negative integer")

    grouped_by_item: dict[ItemKey, list[FusedCandidate]] = {}
    for candidate in candidates:
        _require_finite_real(candidate.rrf_score, "candidate rrf_score")
        item_key = (candidate.platform, candidate.source_id)
        if item_key not in evidence_by_item:
            continue
        grouped_by_item.setdefault(item_key, []).append(candidate)

    grouped: list[GroupedCandidate] = []
    for item_key, item_candidates in grouped_by_item.items():
        evidence = evidence_by_item[item_key]
        _require_finite_real(evidence.rank_factor, "evidence rank_factor")
        normalized_candidates = _normalize_item_candidates(item_candidates)
        primary, *supporting = normalized_candidates
        grouped.append(
            GroupedCandidate(
                primary=primary,
                supporting=tuple(supporting[:max_supporting]),
                evidence=evidence,
                adjusted_rrf_score=primary.rrf_score * evidence.rank_factor,
            )
        )
    return tuple(
        sorted(
            grouped,
            key=lambda candidate: (
                -candidate.adjusted_rrf_score,
                candidate.primary.best_rank,
                candidate.primary.platform,
                candidate.primary.source_id,
                candidate.primary.ordinal,
            ),
        )[:limit]
    )


def _normalize_item_candidates(
    candidates: Sequence[FusedCandidate],
) -> tuple[FusedCandidate, ...]:
    sorted_candidates = sorted(candidates, key=_fused_candidate_key)
    unique_candidates: dict[str, FusedCandidate] = {}
    for candidate in sorted_candidates:
        unique_candidates.setdefault(candidate.citation_id, candidate)
    return tuple(unique_candidates.values())


def _fused_candidate_key(candidate: FusedCandidate) -> tuple[Any, ...]:
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


def _require_finite_real(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value):
        raise ValueError(f"{name} must be a finite real number")


def _asset_source(asset: object) -> str | None:
    if not isinstance(asset, Mapping):
        return None
    relative_path = asset.get("relative_path")
    if not isinstance(relative_path, str):
        return None
    if not _is_safe_relative_posix_path(relative_path):
        return None

    if relative_path.startswith("transcript/"):
        return "transcript"
    if relative_path.startswith("ocr/"):
        return "ocr"
    return None


def _is_safe_relative_posix_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or ":" in path:
        return False
    return all(
        SAFE_ID.fullmatch(part) is not None
        and part not in {".", ".."}
        and not part.endswith(".")
        and part.split(".", 1)[0].upper() not in WINDOWS_RESERVED_NAMES
        for part in path.split("/")
    )


__all__ = [
    "EvidenceLevel",
    "GroupedCandidate",
    "ItemEvidence",
    "ItemKey",
    "classify_evidence",
    "group_candidates",
]
