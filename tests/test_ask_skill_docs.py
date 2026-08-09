"""Documentation contract tests for the retrieval-usage Skill."""

from pathlib import Path

SKILL_DIR = Path(__file__).parents[1] / "skills" / "favhub-ask"
SKILL = SKILL_DIR / "SKILL.md"
MCP_CONTRACT = SKILL_DIR / "references" / "mcp-contract.md"

RETRIEVAL_TOOLS = (
    "favhub.search",
    "favhub.get_item",
    "favhub.collections",
    "favhub.status",
)

REQUIRED_SKILL_MARKERS = (
    # v0.1 design §6: FavHub is a personal reference, never the only truth.
    "个人参考资料",
    "唯一事实来源",
    "易变",
    # Answers must label their provenance.
    "来自个人收藏",
    "来自当前外部资料",
    # Honest misses: no forced stitching.
    "未找到",
    "强行拼接",
    # Citation discipline.
    "citation_id",
    "favhub:",
    "原链接",
    "视频时间戳",
    "local_path",
    # Query craft.
    "改写",
    "中英",
    "platforms",
    "contentTypes",
    "publishedSince",
    "标签",
    "主题查询",
    "主要载体",
    "不同收藏",
    "supporting_chunks",
    "evidence_level",
    "证据矩阵",
    "查找型",
    "总结型",
    "推荐型",
    "对比型",
    "方案型",
    "读取全文",
    "不是搜索结果列表",
    "Agent 综合推断",
    "待进一步核验的线索",
)

FORBIDDEN_WORDS = ("cookie", "token", "bearer", "csrf", "sessdata")


def test_skill_package_files_exist() -> None:
    assert SKILL.is_file()
    assert MCP_CONTRACT.is_file()


def test_skill_names_every_retrieval_tool() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for tool in RETRIEVAL_TOOLS:
        assert tool in text, tool


def test_skill_documents_required_markers() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in REQUIRED_SKILL_MARKERS:
        assert marker in text, marker


def test_skill_decides_whether_to_search_from_the_map_not_a_hunch() -> None:
    """The trigger rule has to be checkable, or it degrades back into guessing.

    The old rule was "when the question obviously falls within their saved
    topics", which is unfalsifiable and made the Skill fire on vibes. These
    markers pin the three things that replaced it.
    """
    text = SKILL.read_text(encoding="utf-8")
    for marker in (
        # The map is what decides, and it is read once per session.
        "favhub.collections",
        "一次",
        # A folder name that describes an impulse is not a topic.
        "默认收藏夹",
        "稍后再看",
        # The map's own blind spot: platforms with no folders at all.
        "unfiled",
        '`platforms: ["github"]`',
        # General questions stay out of the library.
        "只增加延迟和噪声",
    ):
        assert marker in text, marker


def test_skill_separates_an_empty_library_from_a_broken_index() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("index_state", "不要说成"):
        assert marker in text, marker


def test_skill_distinguishes_topic_from_primary_carrier() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in (
        "AI 视频相关内容",
        "不传 `contentTypes`",
        "只看收藏的视频",
        '`contentTypes: ["video"]`',
    ):
        assert marker in text, marker


def test_skill_closes_provenance_loop_for_material_claims() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in (
        "来自收藏的实质性主张",
        "具名外部来源和外部 URL",
        "外部 URL",
        "外部来源",
        "不伪造",
        "不冒充",
    ):
        assert marker in text, marker


def test_skill_requires_full_read_for_each_selected_item() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for marker in ("前 3–5 个不同收藏", "逐一", "每个", "includeContent: true"):
        assert marker in text, marker


def test_skill_contains_no_credential_words() -> None:
    for path in (SKILL, MCP_CONTRACT):
        folded = path.read_text(encoding="utf-8").casefold()
        for word in FORBIDDEN_WORDS:
            assert word not in folded, (path.name, word)


def test_mcp_contract_reference_documents_search_surface() -> None:
    text = MCP_CONTRACT.read_text(encoding="utf-8")
    for tool in RETRIEVAL_TOOLS:
        assert tool in text, tool
    for marker in ("query", "limit", "includeContent", "retrieval_mode", "hits"):
        assert marker in text, marker
