"""Documentation contract tests for the Zhihu collection Skill.

The Skill is prose, but what it tells an Agent to do is part of the contract.
Collection moved into the browser extension, so the dangerous failure now is a
Skill that still reads like the old one: an Agent following it would paste a
console script and submit batches the extension is already submitting. These
tests pin the new shape and forbid the old one by name.
"""

from pathlib import Path

SKILL_DIR = Path(__file__).parents[1] / "skills" / "favhub-zhihu-sync"
SKILL = SKILL_DIR / "SKILL.md"
MCP_CONTRACT = SKILL_DIR / "references" / "mcp-contract.md"
BROWSER_PROBE = SKILL_DIR / "references" / "browser-probe.md"

BROWSER_TOOLS = (
    "favhub.browser_start",
    "favhub.browser_resume",
    "favhub.browser_status",
    "favhub.browser_cancel",
)

REQUIRED_SKILL_MARKERS = (
    "favhub.status",
    # Preflight, and the two commands that repair a broken install.
    "预检",
    "登录态",
    "扩展",
    "favhub setup",
    "favhub doctor",
    # The state an Agent reports while the browser has not arrived yet.
    "awaiting_browser",
    # Same-origin API, never a constructed third-party request.
    "/api/v4/",
    "同源",
    # The only end signal. A page below the limit is not the end.
    "is_end",
    "短页不是终点",
    "totals",
    # Per-folder frontier: one truncated collection must not hold back the rest.
    "frontier",
    "frontierScopes",
    "scopeResults",
    "maxScanReached",
    # Resume happens on the same job.
    "job_id",
    # Cross-folder deduplication keeps the earliest real favourite time.
    "最早值",
    # Pause causes.
    "login_required",
    "rate_limited",
    "page_changed",
    "browser_unavailable",
    # Smoke-first and fixture honesty.
    "maxScanItems",
    "冒烟",
    "fixture",
    # Mode is required by the tool and has no default, so the Skill has to
    # decide rather than ask. It must also not equate a first run with a full
    # one: with no frontier yet, incremental already scans to the end.
    "默认 `incremental`",
    "第一次同步",
)

# Instructions from the pre-extension design. An Agent that still followed
# these would duplicate — or corrupt — what the extension is already doing.
FORBIDDEN_SKILL_MARKERS = (
    "favhub.sync_submit_batch",
    "favhub.sync_finish",
    "batchId",
    "采集脚本",
    "控制台脚本",
    # The account is identified through the API by the extension; a Skill has
    # no reason to name the field at all any more.
    "url_token",
)

PROHIBITION_WORDS = ("禁", "不得", "不要", "决不", "不支持", "不涉及", "never", "不导出")
CREDENTIAL_WORDS = ("token", "z_c0", "凭证", "credential", "cookie")


def test_skill_package_files_exist() -> None:
    assert SKILL.is_file()
    assert MCP_CONTRACT.is_file()
    assert BROWSER_PROBE.is_file()


def test_skill_names_every_browser_tool() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for tool in BROWSER_TOOLS:
        assert tool in text, tool


def test_skill_documents_required_workflow_markers() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in REQUIRED_SKILL_MARKERS:
        assert marker in text, marker


def test_skill_no_longer_asks_the_agent_to_collect_by_hand() -> None:
    """The extension collects; the Skill orchestrates.

    Leaving these in would not merely be stale prose: an Agent submitting its
    own batches alongside the extension is a second writer on one job.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in FORBIDDEN_SKILL_MARKERS:
        assert marker not in text, marker


def test_skill_forbids_a_dom_fallback() -> None:
    """A run that cannot reach the API must stop, not scrape the page."""
    text = SKILL.read_text(encoding="utf-8")
    assert "DOM" in text
    for line in text.splitlines():
        if "DOM" in line:
            assert any(word in line for word in PROHIBITION_WORDS), line


def test_skill_prohibits_arbitrary_urls_and_debug_ports() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "任意 URL" in text
    for line in text.splitlines():
        if "任意 URL" in line or "调试端口" in line:
            assert any(word in line for word in PROHIBITION_WORDS), line


def test_credential_words_only_inside_prohibitions() -> None:
    for path in (SKILL, MCP_CONTRACT, BROWSER_PROBE):
        for line in path.read_text(encoding="utf-8").splitlines():
            folded = line.casefold()
            if any(word in folded for word in CREDENTIAL_WORDS):
                assert any(word.casefold() in folded for word in PROHIBITION_WORDS), (
                    path.name,
                    line,
                )


def test_mcp_contract_reference_documents_tools_and_errors() -> None:
    text = MCP_CONTRACT.read_text(encoding="utf-8")
    for tool in BROWSER_TOOLS:
        assert tool in text, tool
    for marker in ("scopeResults", "frontierScopes", "invalid_argument", "not_found"):
        assert marker in text, marker


def test_browser_probe_reference_explains_who_collects() -> None:
    """The probe is now a description of the extension, not instructions."""
    text = BROWSER_PROBE.read_text(encoding="utf-8")
    for marker in ("同源", "扩展", "脱敏", "is_end"):
        assert marker in text, marker
