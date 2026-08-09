"""Documentation contract tests for the GitHub collection Skill."""

import re
from pathlib import Path

SKILL = Path(__file__).parents[1] / "skills" / "favhub-github-sync" / "SKILL.md"

REQUIRED_MARKERS = (
    # One call. The orchestration the Skill used to describe now lives in the
    # process that owns the data root.
    "favhub.github_sync",
    "favhub.status",
    "user",
    "maxScanItems",
    "incremental",
    # How to read what comes back.
    "added",
    "duplicates",
    "readmes_missing",
    "authenticated",
    # The four failures, each of which deserves a different sentence.
    "source_unavailable",
    "login_required",
    "rate_limited",
    "page_changed",
    # Standing limits.
    "公开",
    "冒烟",
    "fixture",
)

TOKEN_ENV = "FAVHUB_GITHUB_TOKEN"

# What the Agent must be told not to do with the credential. The old test
# instead required a prohibition word near every mention of one, which fails on
# a sentence as harmless as "`authenticated` says whether one was used" and
# passes anything that says "决不" somewhere on the line. Name the actual
# forbidden acts instead.
FORBIDDEN_ACTS = ("索取", "贴进对话", "写进任何文件")

# Shapes a real GitHub credential takes. A Skill that ever grew an example
# value would teach the pattern it exists to forbid.
TOKEN_SHAPES = re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}")


def test_skill_exists_and_documents_the_single_call() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in REQUIRED_MARKERS:
        assert marker in text, marker


def test_skill_no_longer_describes_agent_side_collection() -> None:
    """The Agent used to paginate and submit batches itself; it must not now.

    Leaving the old choreography in place would have the Agent hand-rolling a
    path that exists precisely so an optional credential never has to travel
    through its context.
    """
    text = SKILL.read_text(encoding="utf-8")
    for stale in (
        "favhub.sync_start",
        "favhub.sync_submit_batch",
        "favhub.sync_finish",
        "batchId",
        "x-ratelimit-remaining",
    ):
        assert stale not in text, stale


def test_skill_names_the_credential_variable_and_forbids_holding_it() -> None:
    """Only the variable name is the Agent's business; the value never is."""
    text = SKILL.read_text(encoding="utf-8")
    assert TOKEN_ENV in text
    for act in FORBIDDEN_ACTS:
        assert act in text, act
    # Whether one was used is reportable; the value is not.
    assert "authenticated" in text


def test_skill_keeps_the_agent_off_githubs_hosts() -> None:
    """The Agent calling these directly is how the credential would get near it."""
    text = SKILL.read_text(encoding="utf-8")
    for host in ("api.github.com", "raw.githubusercontent.com"):
        assert host in text, host
    assert "决不**自己去请求" in text


def test_skill_contains_nothing_shaped_like_a_real_credential() -> None:
    assert TOKEN_SHAPES.search(SKILL.read_text(encoding="utf-8")) is None


def test_skill_warns_that_the_cli_is_locked_out_while_the_server_runs() -> None:
    """The first real run failed exactly this way.

    A CLI-only instruction is usable precisely when the user is not using
    FavHub, which is not when they ask for a sync.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("github-sync", "数据根锁"):
        assert marker in text, marker
