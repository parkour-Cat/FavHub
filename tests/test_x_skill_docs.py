"""Documentation contract tests for the X bookmarks collection Skill.

The Skill is prose, but what it tells an Agent to do is part of the contract.
Collection moved into the browser extension, so the dangerous failure now is a
Skill that still reads like the old one: an Agent following it would install
hooks, parse responses, and submit batches that the extension is already
submitting. These tests pin the new shape and forbid the old one by name.
"""

from pathlib import Path

SKILL_DIR = Path(__file__).parents[1] / "skills" / "favhub-x-sync"
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
    # Preflight, and the two commands that repair a broken install.
    "预检",
    "登录态",
    "扩展",
    "favhub setup",
    "favhub doctor",
    # The state an Agent reports while the browser has not arrived yet.
    "awaiting_browser",
    # Platform-level frontier; bookmarks have no folders.
    "frontier",
    # Publication-time filtering never stops collection early.
    "发布时间",
    "不能提前停止",
    # Resume happens on the same job.
    "job_id",
    # Pause causes.
    "login_required",
    "captcha_required",
    "rate_limited",
    "page_changed",
    "browser_unavailable",
    # Smoke-first and fixture honesty.
    "maxScanItems",
    "冒烟",
    "fixture",
)

# Instructions from the pre-extension design. An Agent that still followed
# these would duplicate — or corrupt — what the extension is already doing.
FORBIDDEN_SKILL_MARKERS = (
    "favhub.sync_submit_batch",
    "favhub.sync_finish",
    "batchId",
    "拦截钩子",
    "控制台脚本",
)

REQUIRED_EXCLUSIONS = ("回复区", "讨论串", "外链正文", "二进制", "ASR", "视频下载")

PROHIBITION_WORDS = ("禁", "不得", "不要", "决不", "never", "不导出", "不读取", "不包含")

CREDENTIAL_WORDS = ("cookie", "token", "bearer", "csrf", "ct0", "auth_token", "sessdata")


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
    """A run that cannot intercept must stop, not start scraping the page.

    Falling back to the DOM would produce a partial, unlabelled library that
    looks like a successful sync.
    """
    text = SKILL.read_text(encoding="utf-8")
    assert "DOM" in text
    for line in text.splitlines():
        if "DOM" in line:
            assert any(word in line for word in PROHIBITION_WORDS), line


def test_skill_declares_explicit_exclusions() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in REQUIRED_EXCLUSIONS:
        assert marker in text, marker


def test_skill_mentions_credentials_only_inside_prohibitions() -> None:
    for path in (SKILL, MCP_CONTRACT, BROWSER_PROBE):
        for line in path.read_text(encoding="utf-8").splitlines():
            folded = line.casefold()
            if any(word in folded for word in CREDENTIAL_WORDS):
                assert any(word.casefold() in folded for word in PROHIBITION_WORDS), (
                    path.name,
                    line,
                )


def test_skill_prohibits_arbitrary_urls_and_constructed_requests() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert "任意 URL" in text
    for line in text.splitlines():
        if "任意 URL" in line or "调试端口" in line:
            assert any(word in line for word in PROHIBITION_WORDS), line


def test_mcp_contract_reference_documents_x_semantics() -> None:
    text = MCP_CONTRACT.read_text(encoding="utf-8")
    for tool in BROWSER_TOOLS:
        assert tool in text, tool
    # A platform-level frontier and no scopes is what makes X different here.
    # `frontierIds` deliberately does not appear: the extension submits it, and
    # a Skill that named it would imply the Agent has a hand in finishing.
    for marker in ("平台级", "awaiting_browser", "invalid_argument", "not_found"):
        assert marker in text, marker
    assert "frontierIds" not in text


def test_browser_probe_reference_explains_who_collects() -> None:
    """The probe is now a description of the extension, not instructions."""
    text = BROWSER_PROBE.read_text(encoding="utf-8")
    for marker in ("被动拦截", "扩展", "响应正文", "脱敏"):
        assert marker in text, marker
