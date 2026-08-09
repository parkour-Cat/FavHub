"""Deterministic token-window segmentation independent of any tokenizer."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SemanticSegment:
    ordinal: int
    token_start: int
    token_end: int
    text: str


def segment_tokens(
    tokens: Sequence[str],
    *,
    segment_tokens: int = 480,
    overlap_tokens: int = 32,
    token_to_text: Callable[[Sequence[str]], str] | None = None,
) -> tuple[SemanticSegment, ...]:
    if (
        isinstance(segment_tokens, bool)
        or not isinstance(segment_tokens, int)
        or segment_tokens <= 0
    ):
        raise ValueError("segment_tokens must be a positive integer")
    if (
        isinstance(overlap_tokens, bool)
        or not isinstance(overlap_tokens, int)
        or overlap_tokens < 0
        or overlap_tokens >= segment_tokens
    ):
        raise ValueError("overlap_tokens must be in [0, segment_tokens)")
    if not tokens:
        return ()
    render = token_to_text or (lambda window: " ".join(window))
    segments: list[SemanticSegment] = []
    start = 0
    ordinal = 0
    token_count = len(tokens)
    while start < token_count:
        end = min(start + segment_tokens, token_count)
        window = tokens[start:end]
        segments.append(
            SemanticSegment(
                ordinal=ordinal,
                token_start=start,
                token_end=end,
                text=render(window),
            )
        )
        ordinal += 1
        if end == token_count:
            break
        start = end - overlap_tokens
    return tuple(segments)
