import json
from dataclasses import replace
from pathlib import Path

import pytest

from favhub.hybrid_search import FusedCandidate
from favhub.retrieval_results import (
    EvidenceLevel,
    ItemEvidence,
    classify_evidence,
    group_candidates,
)


def _fused(
    source_id: str,
    ordinal: int,
    score: float,
    best_rank: int,
    *,
    platform: str = "x",
) -> FusedCandidate:
    return FusedCandidate(
        citation_id=f"favhub:{platform}/{source_id}#chunk-{ordinal}",
        match_sources=("vector",),
        rrf_score=score,
        best_rank=best_rank,
        platform=platform,
        source_id=source_id,
        ordinal=ordinal,
        payload={"source_id": source_id, "ordinal": ordinal},
    )


def test_group_candidates_collapses_chunks_and_keeps_bounded_supporting() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)
    candidates = tuple(
        _fused("same", ordinal, 0.030 - ordinal / 1000, ordinal + 1) for ordinal in range(5)
    ) + (_fused("other", 0, 0.020, 6),)

    grouped = group_candidates(
        candidates,
        {("x", "same"): evidence, ("x", "other"): evidence},
        limit=10,
    )

    assert [candidate.primary.source_id for candidate in grouped] == ["same", "other"]
    assert grouped[0].primary.ordinal == 0
    assert [candidate.ordinal for candidate in grouped[0].supporting] == [1, 2, 3]
    assert grouped[1].primary.ordinal == 0


def test_group_candidates_adjusts_title_only_evidence_without_dropping_it() -> None:
    title = ItemEvidence(EvidenceLevel.TITLE_ONLY, (), 0.75, "Title only")
    body = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)

    grouped = group_candidates(
        (_fused("title", 0, 0.030, 1), _fused("body", 0, 0.025, 2)),
        {("x", "title"): title, ("x", "body"): body},
        limit=10,
    )

    assert [candidate.primary.source_id for candidate in grouped] == ["body", "title"]
    assert grouped[1].evidence.level is EvidenceLevel.TITLE_ONLY


def test_group_candidates_uses_stable_item_key_tiebreaks() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 1.0)
    candidates = (
        _fused("alpha", 2, 0.025, 1, platform="z"),
        _fused("zeta", 1, 0.025, 1, platform="x"),
        _fused("alpha", 3, 0.025, 1, platform="x"),
        _fused("alpha", 4, 0.024, 2, platform="x"),
    )

    grouped = group_candidates(
        candidates,
        {
            ("z", "alpha"): evidence,
            ("x", "zeta"): evidence,
            ("x", "alpha"): evidence,
        },
        limit=10,
    )

    assert [
        (candidate.primary.platform, candidate.primary.source_id, candidate.primary.ordinal)
        for candidate in grouped
    ] == [("x", "alpha", 3), ("x", "zeta", 1), ("z", "alpha", 2)]
    assert [candidate.ordinal for candidate in grouped[0].supporting] == [4]
    # Ordinal orders chunks within an item; it does not manufacture another item-level key.


def test_group_candidates_skips_candidates_without_item_evidence() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)

    grouped = group_candidates(
        (_fused("missing", 0, 0.030, 1), _fused("known", 0, 0.020, 2)),
        {("x", "known"): evidence},
        limit=10,
    )

    assert [candidate.primary.source_id for candidate in grouped] == ["known"]


def test_group_candidates_allows_no_supporting_chunks() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)

    grouped = group_candidates(
        (_fused("same", 0, 0.030, 1), _fused("same", 1, 0.029, 2)),
        {("x", "same"): evidence},
        limit=10,
        max_supporting=0,
    )

    assert grouped[0].supporting == ()


def test_group_candidates_normalizes_chunks_by_fused_relevance() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)

    grouped = group_candidates(
        (_fused("same", 0, 0.010, 9), _fused("same", 1, 0.030, 1)),
        {("x", "same"): evidence},
        limit=10,
    )

    assert grouped[0].primary.ordinal == 1
    assert [candidate.ordinal for candidate in grouped[0].supporting] == [0]


def test_group_candidates_keeps_only_best_candidate_for_each_citation() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)
    duplicate_citation = "favhub:x/same#chunk-shared"

    grouped = group_candidates(
        (
            replace(_fused("same", 0, 0.010, 9), citation_id=duplicate_citation),
            replace(_fused("same", 1, 0.030, 1), citation_id=duplicate_citation),
            _fused("same", 2, 0.020, 2),
        ),
        {("x", "same"): evidence},
        limit=10,
    )

    assert grouped[0].primary.ordinal == 1
    assert [candidate.ordinal for candidate in grouped[0].supporting] == [2]
    assert {
        grouped[0].primary.citation_id,
        *(candidate.citation_id for candidate in grouped[0].supporting),
    } == {duplicate_citation, "favhub:x/same#chunk-2"}


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), True])
def test_group_candidates_rejects_non_finite_candidate_rrf_score(score: object) -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)

    with pytest.raises(ValueError, match="^candidate rrf_score must be a finite real number$"):
        group_candidates(
            (replace(_fused("same", 0, 0.030, 1), rrf_score=score),),  # type: ignore[arg-type]
            {("x", "same"): evidence},
            limit=10,
        )


@pytest.mark.parametrize("rank_factor", [float("nan"), float("inf"), float("-inf"), True])
def test_group_candidates_rejects_non_finite_used_evidence_rank_factor(
    rank_factor: object,
) -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), rank_factor)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="^evidence rank_factor must be a finite real number$"):
        group_candidates(
            (_fused("same", 0, 0.030, 1),),
            {("x", "same"): evidence},
            limit=10,
        )


def test_group_candidates_applies_limit_to_unique_items() -> None:
    evidence = ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95)

    grouped = group_candidates(
        (
            _fused("first", 0, 0.030, 1),
            _fused("first", 1, 0.029, 2),
            _fused("second", 0, 0.020, 3),
        ),
        {("x", "first"): evidence, ("x", "second"): evidence},
        limit=1,
    )

    assert [candidate.primary.source_id for candidate in grouped] == ["first"]
    assert [candidate.ordinal for candidate in grouped[0].supporting] == [1]


@pytest.mark.parametrize("limit", [True, False, 0, -1, 1.5, "1"])
def test_group_candidates_rejects_invalid_limit(limit: object) -> None:
    with pytest.raises(ValueError, match="^limit must be a positive integer$"):
        group_candidates((), {}, limit=limit)  # type: ignore[arg-type]


@pytest.mark.parametrize("max_supporting", [True, False, -1, 1.5, "1"])
def test_group_candidates_rejects_invalid_max_supporting(max_supporting: object) -> None:
    with pytest.raises(ValueError, match="^max_supporting must be a non-negative integer$"):
        group_candidates((), {}, limit=1, max_supporting=max_supporting)  # type: ignore[arg-type]


def test_group_candidates_replays_retrieval_quality_fixture() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "favhub-retrieval-quality-replay.json"
    replay_cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    evidence_by_level = {
        "title_only": ItemEvidence(EvidenceLevel.TITLE_ONLY, (), 0.75),
        "body": ItemEvidence(EvidenceLevel.BODY, ("body",), 0.95),
        "transcript": ItemEvidence(EvidenceLevel.TRANSCRIPT, ("transcript",), 1.0),
        "ocr": ItemEvidence(EvidenceLevel.OCR, ("ocr",), 1.0),
        "mixed": ItemEvidence(EvidenceLevel.MIXED, ("body", "transcript"), 1.0),
    }

    for replay_case in replay_cases:
        candidates = tuple(
            _fused(
                candidate["source_id"],
                candidate.get("ordinal", 0),
                candidate["score"],
                index + 1,
            )
            for index, candidate in enumerate(replay_case["candidates"])
        )
        evidence_by_item = {
            ("x", candidate["source_id"]): evidence_by_level[candidate["evidence"]]
            for candidate in replay_case["candidates"]
        }

        grouped = group_candidates(candidates, evidence_by_item, limit=10)

        assert [candidate.primary.source_id for candidate in grouped] == replay_case[
            "expected_order"
        ], replay_case["id"]


def test_classify_evidence_does_not_treat_generated_summary_as_body() -> None:
    evidence = classify_evidence(
        {
            "title": "Seedance workflow",
            "body": "",
            "enrichment": {"summary": "Summary inferred from the title."},
        }
    )

    assert evidence.level is EvidenceLevel.TITLE_ONLY
    assert evidence.rank_factor == 0.75
    assert evidence.warning == (
        "Only title and metadata are available; no body, transcript, or OCR was captured."
    )


def test_classify_evidence_uses_durable_body_and_text_assets() -> None:
    body = classify_evidence({"title": "Post", "body": "Useful source text"})
    transcript = classify_evidence(
        {
            "title": "Video",
            "body": "",
            "assets": [{"relative_path": "transcript/0001.md"}],
        }
    )
    ocr = classify_evidence(
        {
            "title": "Image post",
            "body": "",
            "assets": [{"relative_path": "ocr/0001.md"}],
        }
    )
    mixed = classify_evidence(
        {
            "title": "Subtitled video",
            "body": "Description",
            "assets": [{"relative_path": "transcript/0001.md"}],
        }
    )

    assert body.level is EvidenceLevel.BODY
    assert body.rank_factor == 0.95
    assert transcript.level is EvidenceLevel.TRANSCRIPT
    assert ocr.level is EvidenceLevel.OCR
    assert mixed.level is EvidenceLevel.MIXED
    assert transcript.rank_factor == ocr.rank_factor == mixed.rank_factor == 1.0


@pytest.mark.parametrize(
    "relative_path",
    [
        "assets/transcript/foo.md",
        "assets/ocr/foo.md",
        "transcript\\foo.md",
        "OCR/foo.md",
    ],
)
def test_classify_evidence_requires_exact_posix_asset_root(relative_path: str) -> None:
    evidence = classify_evidence(
        {"title": "Untyped asset", "body": "", "assets": [{"relative_path": relative_path}]}
    )

    assert evidence.level is EvidenceLevel.TITLE_ONLY


@pytest.mark.parametrize(
    "relative_path",
    [
        "transcript/../ocr/0001.md",
        "transcript//x.md",
        "ocr/../assets/x.json",
        "transcript/CON.md",
        "transcript/name.",
        "transcript/a b.md",
    ],
)
def test_classify_evidence_rejects_unsafe_text_asset_paths(relative_path: str) -> None:
    evidence = classify_evidence(
        {"title": "Unsafe asset", "body": "", "assets": [{"relative_path": relative_path}]}
    )

    assert evidence.level is EvidenceLevel.TITLE_ONLY


def test_classify_evidence_reports_ordered_unique_sources() -> None:
    transcript_and_ocr = classify_evidence(
        {
            "title": "Video with OCR",
            "body": "",
            "assets": [
                {"relative_path": "transcript/0001.md"},
                {"relative_path": "transcript/0002.md"},
                {"relative_path": "ocr/0001.md"},
            ],
        }
    )
    body_and_transcript = classify_evidence(
        {
            "title": "Described video",
            "body": "Description",
            "assets": [{"relative_path": "transcript/0001.md"}],
        }
    )

    assert transcript_and_ocr.level is EvidenceLevel.MIXED
    assert transcript_and_ocr.sources == ("transcript", "ocr")
    assert body_and_transcript.sources == ("body", "transcript")
