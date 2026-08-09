from __future__ import annotations

import pytest

from favhub.semantic_chunking import segment_tokens


def test_empty_tokens_produce_no_segments() -> None:
    assert segment_tokens(()) == ()


@pytest.mark.parametrize(
    ("segment_size", "overlap"),
    [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11)],
)
def test_segment_tokens_rejects_invalid_window_bounds(segment_size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="segment|overlap"):
        segment_tokens(("a",), segment_tokens=segment_size, overlap_tokens=overlap)


def test_segments_are_deterministic_ordered_and_overlap_with_reconstruction_seam() -> None:
    tokens = tuple(f"t{i}" for i in range(1000))
    render_calls: list[tuple[str, ...]] = []

    def render(window: tuple[str, ...] | list[str]) -> str:
        render_calls.append(tuple(window))
        return "|".join(window)

    first = segment_tokens(tokens, segment_tokens=480, overlap_tokens=32, token_to_text=render)
    second = segment_tokens(tokens, segment_tokens=480, overlap_tokens=32, token_to_text=render)

    assert first == second
    assert [segment.ordinal for segment in first] == list(range(len(first)))
    assert first[0].token_start == 0
    assert first[0].token_end == 480
    assert first[1].token_start == 448
    assert first[-1].token_end == 1000
    assert first[0].text == "|".join(tokens[:480])
    assert render_calls[0] == tokens[:480]
