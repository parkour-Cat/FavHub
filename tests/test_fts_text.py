"""Unit tests for the CJK bigram transform behind Chinese FTS matching."""

from favhub.fts_text import fts_text


def test_cjk_runs_become_overlapping_bigrams() -> None:
    assert fts_text("芭蕾舞者") == "芭蕾 蕾舞 舞者"


def test_single_cjk_char_kept_as_is() -> None:
    assert fts_text("猫") == "猫"


def test_ascii_passes_through_unchanged() -> None:
    assert fts_text("golang practice-projects 2024") == "golang practice-projects 2024"


def test_mixed_text_interleaves_words_and_bigrams() -> None:
    assert fts_text("golang练手项目") == "golang 练手 手项 项目"


def test_punctuation_breaks_runs() -> None:
    assert fts_text("算法，图论") == "算法 ， 图论"


def test_deterministic() -> None:
    sample = "用AI做出一套专属微信表情包 - 实在太快了"
    assert fts_text(sample) == fts_text(sample)
