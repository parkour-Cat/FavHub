from favhub.hybrid_search import (
    LexicalCandidate,
    SemanticCandidate,
    candidate_pool_size,
    reciprocal_rank_fusion,
)


def test_candidate_pool_is_bounded() -> None:
    assert candidate_pool_size(1) == 50
    assert candidate_pool_size(10) == 100
    assert candidate_pool_size(100) == 500


def test_semantic_segments_collapse_to_max_and_citation_deduplicates() -> None:
    result = reciprocal_rank_fusion(
        [LexicalCandidate("c1", 1)],
        [
            SemanticCandidate("c1", 0.2),
            SemanticCandidate("c1", 0.9),
            SemanticCandidate("c2", 0.8),
        ],
        limit=10,
    )
    assert [candidate.citation_id for candidate in result] == ["c1", "c2"]
    assert result[0].cosine_similarity == 0.9
    assert result[0].match_sources == ("fts", "vector")


def test_rrf_equal_weights_and_stable_ties() -> None:
    result = reciprocal_rank_fusion(
        [LexicalCandidate("b", 1, platform="x", source_id="2"), LexicalCandidate("a", 2)],
        [SemanticCandidate("a", 0.9), SemanticCandidate("b", 0.8)],
        limit=10,
    )
    assert result[0].rrf_score == 1 / 61 + 1 / 62
    assert result[1].rrf_score == 1 / 61 + 1 / 62
    assert [candidate.citation_id for candidate in result] == ["b", "a"]


def test_published_at_sort_handles_optional_fractional_seconds() -> None:
    result = reciprocal_rank_fusion(
        [],
        [
            SemanticCandidate("older", 0.9, published_at="2026-01-01T00:00:00Z"),
            SemanticCandidate("newer", 0.9, published_at="2026-01-01T00:00:00.1Z"),
        ],
        limit=10,
    )

    assert [candidate.citation_id for candidate in result] == ["newer", "older"]
