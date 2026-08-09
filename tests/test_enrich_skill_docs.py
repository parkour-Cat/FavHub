"""Documentation contract tests for the enrichment Skill."""

from pathlib import Path

SKILL_DIR = Path(__file__).parents[1] / "skills" / "favhub-enrich"
SKILL = SKILL_DIR / "SKILL.md"
MCP_CONTRACT = SKILL_DIR / "references" / "mcp-contract.md"

ENRICH_TOOLS = ("favhub.enrich_next", "favhub.enrich_submit", "favhub.enrich_skip")

REQUIRED_SKILL_MARKERS = (
    "预检",
    # The generate loop and its result rules.
    "拉取",
    "中文摘要",
    "关键术语",
    "3-8",
    "小写",
    "video",
    "image",
    "mixed",
    "text",
    # Flow control.
    "stale",
    "truncated",
    "generation_failed",
    "content_unsupported",
    "进度",
    # Disclosure duty and user control (v0.1 design §7).
    "由 Agent 模型生成",
    "随时停止",
    # Honesty marker.
    "fixture",
    # Batch mode: delegate bulk generation to cheap-model subagents.
    "批量",
    "便宜模型",
    "子代理",
    "抽查",
    "如实记录",
    "纯娱乐",
    "跳过判据严格",
    "误跳过",
)

# This Skill never touches a browser; credential words must not appear at all.
FORBIDDEN_WORDS = ("cookie", "token", "bearer", "csrf", "sessdata")


def test_skill_package_files_exist() -> None:
    assert SKILL.is_file()
    assert MCP_CONTRACT.is_file()


def test_skill_names_every_enrich_tool() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for tool in ENRICH_TOOLS:
        assert tool in text, tool


def test_skill_documents_required_workflow_markers() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in REQUIRED_SKILL_MARKERS:
        assert marker in text, marker


def test_skill_offers_the_cheap_platform_before_the_whole_queue() -> None:
    """Cost is per item and platforms differ by an order of magnitude in length.

    An estimate is a guess; one cheap platform run is a measurement. The Skill
    has to offer that, and has to say why skip is not the way to get it.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("platform", "最便宜", "决不", "enrich_skip"):
        assert marker in text, marker


def test_contract_says_an_empty_scoped_claim_is_not_an_empty_queue() -> None:
    text = MCP_CONTRACT.read_text(encoding="utf-8")
    for marker in ("platform", "不表示队列为空", "不计 attempts"):
        assert marker in text, marker


def test_skill_separates_a_retry_from_a_verdict_about_the_content() -> None:
    """Using the retryable code to mean "skip this one" loops the whole run."""
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("retryable", "declined", "卡死", "input_hash"):
        assert marker in text, marker


def test_contract_says_which_skip_code_comes_back() -> None:
    text = MCP_CONTRACT.read_text(encoding="utf-8")
    for marker in ("retryable", "declined", "最老的 pending"):
        assert marker in text, marker


def test_skill_requires_a_summary_to_be_shorter_and_a_cheap_model_to_write_it() -> None:
    """Two rules the measured data forced, not preferences.

    17% of this library's summaries were no shorter than their source, and the
    generation is a read-a-page-write-a-paragraph job that does not need the
    orchestrating model's price.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("显著短于原文", "50%", "拒绝", "标签"):
        assert marker in text, marker
    for marker in ("便宜档", "如实记录"):
        assert marker in text, marker


def test_the_cheap_model_rule_is_written_for_both_runtimes() -> None:
    """favhub setup installs this Skill for Claude Code and Codex alike.

    A rule phrased as "use haiku" is a Claude vocabulary item that the other
    runtime cannot act on, and model names age out faster than the rule does.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("Claude Code", "Codex", "档位", "haiku"):
        assert marker in text, marker


def test_skill_forbids_transliterating_chinese_tags() -> None:
    """46% of one cheap-model batch came back as misspelled pinyin.

    Tags are the whole reason to enrich a short item, and nobody searches for
    "xianyuuyu".
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("音译", "拼音", "闲鱼", "cursor", "拒绝"):
        assert marker in text, marker


def test_skill_says_what_to_do_when_a_submission_is_rejected() -> None:
    """Reaching for skip after a rejection disguises quality as content."""
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("被拒绝", "重新提交", "enrich_skip"):
        assert marker in text, marker


def test_skill_bounds_the_retries_and_says_to_read_the_rejection() -> None:
    """ "Fix it and resubmit" without a bound is an instruction to loop.

    One run met the empty-body rule, varied fields fifteen times, and reported
    the tool as broken rather than the rule as unmet.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("哪条规则", "两次", "停下", "15 次"):
        assert marker in text, marker


def test_skill_says_to_use_the_readable_half_rather_than_refuse() -> None:
    """A refusal is one-way, so "part of this is unusable" must not become one."""
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("部分内容不可用", "只要还有一份能读", "全部内容"):
        assert marker in text, marker


def test_skill_also_forbids_summarising_an_item_that_is_only_a_link() -> None:
    """The counterweight. Told to use what content exists, a model invented some.

    Without this the previous rule reads as "always produce a summary", and a
    body of one t.co link came back as "X post about AI, ads and apps".
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("去掉 URL", "什么都没说", "编造"):
        assert marker in text, marker


def test_skill_contains_no_credential_words() -> None:
    for path in (SKILL, MCP_CONTRACT):
        folded = path.read_text(encoding="utf-8").casefold()
        for word in FORBIDDEN_WORDS:
            assert word not in folded, (path.name, word)


def test_mcp_contract_reference_documents_tools_and_outcomes() -> None:
    text = MCP_CONTRACT.read_text(encoding="utf-8")
    for tool in ENRICH_TOOLS:
        assert tool in text, tool
    for marker in (
        "taskId",
        "applied",
        "stale",
        "contentType",
        "invalid_argument",
        "not_found",
    ):
        assert marker in text, marker
