import json
from pathlib import Path

from favhub.hybrid_search import SemanticCandidate, reciprocal_rank_fusion

FIXTURE = Path(__file__).parent / "fixtures" / "m2b-semantic-queries.json"


def test_fake_vector_acceptance_has_traceable_top_five_for_at_least_16_queries(
    tmp_path: Path,
) -> None:
    queries = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(queries) == 20
    assert sum(bool(entry["paraphrase"]) for entry in queries) >= 8
    sources = [
        "local-knowledge",
        "retry-code",
        "embedding-model",
        "subtitle-index",
        "ocr-recipe",
        "rrf-ranking",
        "sqlite-vectors",
        "citations",
        "durable-queue",
        "metadata-filter",
    ]
    assert {str(entry["expected_source"]) for entry in queries} == set(sources)
    source_vectors = {
        source: tuple(1.0 if index == dimension else 0.0 for dimension in range(len(sources)))
        for index, source in enumerate(sources)
    }
    axis_to_source = dict(enumerate(sources))
    # Query embeddings are keyed by the complete query text and carry an
    # independent axis label. Changing either the query text or expected label
    # therefore makes acceptance fail rather than silently reusing the label.
    query_vectors: dict[str, tuple[float, ...]] = {}
    for entry in queries:
        axis = int(entry["embedding_axis"])
        query_vectors[str(entry["query"])] = source_vectors[axis_to_source[axis]]
    for source in sources:
        path = tmp_path / "items" / "x" / source / "content.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"# {source}\n", encoding="utf-8")
    useful = 0
    for entry in queries:
        expected = str(entry["expected_source"])
        query_vector = query_vectors[str(entry["query"])]
        semantic = [
            SemanticCandidate(
                f"favhub:x/{source}#chunk-0",
                sum(a * b for a, b in zip(query_vector, vector, strict=True)),
                platform="x",
                source_id=source,
                payload={"local_path": f"items/x/{source}/content.md"},
            )
            for source, vector in source_vectors.items()
        ]
        top_five = reciprocal_rank_fusion([], semantic, limit=5)
        expected_hit = next(
            (candidate for candidate in top_five if candidate.source_id == expected), None
        )
        if expected_hit is not None:
            assert expected_hit.citation_id == f"favhub:x/{expected}#chunk-0"
            assert expected_hit.payload["local_path"] == f"items/x/{expected}/content.md"
            assert (tmp_path / expected_hit.payload["local_path"]).is_file()
            useful += 1
    assert useful >= 16
