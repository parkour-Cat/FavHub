"""Contract tests for the answer-quality evaluation question set."""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "favhub-answer-quality-questions.json"
RUBRIC = Path(__file__).parents[1] / "docs" / "favhub-answer-quality-evaluation.md"
EXPECTED_CATEGORIES = {"find", "summarize", "recommend", "compare", "plan"}


def load_questions() -> list[dict[str, object]]:
    """Load the checked-in question set used for answer-quality review."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_answer_quality_fixture_has_required_synthesis_coverage() -> None:
    questions = load_questions()

    assert len(questions) == 20
    assert {question["category"] for question in questions} == EXPECTED_CATEGORIES
    assert all(question["requires_synthesis"] is True for question in questions)

    ids = [question["id"] for question in questions]
    prompts = [question["question"] for question in questions]
    assert len(set(ids)) == len(ids)
    assert all(isinstance(item_id, str) and item_id.strip() for item_id in ids)
    assert len(set(prompts)) == len(prompts)
    assert all(isinstance(prompt, str) and prompt.strip() for prompt in prompts)

    category_counts = {
        category: sum(question["category"] == category for question in questions)
        for category in EXPECTED_CATEGORIES
    }
    assert category_counts == dict.fromkeys(EXPECTED_CATEGORIES, 4)


def test_rubric_defines_reproducible_scoring_boundaries() -> None:
    rubric = RUBRIC.read_text(encoding="utf-8")

    required_markers = (
        "每题覆盖率 = 已覆盖的重要主张数 / 全部重要主张数",
        "全部 20 题的微平均",
        "find / summarize",
        "recommend / compare / plan",
        "标题级条目不能作为详细步骤的证明",
        "明确标为 Agent 推断、推荐或假设",
        "全部重要主张数",
        "已覆盖的重要主张数",
        "每题覆盖率",
        "六个维度分别跨 20 道题计算算术平均",
        "某维度平均分 = 20 道题该维度得分之和 / 20",
        "六个维度各自都必须不低于 3.0",
        "不能用每题六项分数的平均值替代",
        "不能用全部 120 个分数的总平均替代",
    )

    for marker in required_markers:
        assert marker in rubric, marker
