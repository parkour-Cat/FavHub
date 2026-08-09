import json
from datetime import UTC, datetime
from pathlib import Path

from favhub.zhihu_mapping import deduplicate, map_captured_item
from favhub.zhihu_parsers import parse_items_page

FIXTURES = Path(__file__).parent / "fixtures" / "zhihu"
OBSERVED = datetime(2026, 7, 26, tzinfo=UTC)


def load_page(name: str):
    return parse_items_page(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def test_map_answer_builds_question_titled_item() -> None:
    favorite = load_page("items-page-answer-article.json").favorites[0]

    item = map_captured_item(favorite, collection_titles=("示例收藏夹",), observed_at=OBSERVED)

    assert item.platform == "zhihu"
    assert item.source_id == "answer-1776858097"
    assert item.canonical_url.endswith("/answer/1776858097")
    assert item.title == "示例标题22：用于验证解析与索引行为"
    assert item.author == "示例答主二"
    assert item.published_at == datetime.fromtimestamp(1699082533, tz=UTC)
    assert item.collections == ("示例收藏夹",)
    assert item.extractor_version == "zhihu-browser-v1"
    assert "<" not in item.body  # rendered, no HTML residue
    metadata = item.platform_metadata
    assert metadata["favorited_at"] == "2023-11-04T07:22:13Z"
    assert "favorited_at_estimated" not in metadata
    assert metadata["content_type_raw"] == "answer"
    assert metadata["voteup_count"] == 282
    assert metadata["question_id"] == "11986466"


def test_map_article_keeps_own_title_and_collects_images() -> None:
    favorite = load_page("items-page-answer-article.json").favorites[1]

    item = map_captured_item(favorite, collection_titles=("示例收藏夹",), observed_at=OBSERVED)

    assert item.source_id == "article-198662942"
    assert item.title.startswith("示例标题")
    assert "zhuanlan.zhihu.com" in item.canonical_url
    assert "## 图片" in item.body
    assert "zhimg.com" in item.body.split("## 图片", 1)[1]
    assert item.platform_metadata["content_type_raw"] == "article"


def test_map_unknown_type_degrades_to_excerpt() -> None:
    favorite = load_page("item-unknown-type.json").favorites[0]

    item = map_captured_item(favorite, collection_titles=("我的收藏",), observed_at=OBSERVED)

    assert item.source_id == "zvideo-987654321"
    assert item.title == "示例视频标题"
    assert item.canonical_url == "https://www.zhihu.com/zvideo/987654321"
    assert item.body == "视频摘要示例"
    assert item.published_at == OBSERVED  # unknown → observed time
    assert item.platform_metadata["content_type_raw"] == "zvideo"


def test_unsafe_degrade_type_still_yields_a_valid_source_id() -> None:
    from favhub.zhihu_models import ZhihuFavorite, ZhihuOther

    favorite = ZhihuFavorite(
        favorited_at=OBSERVED,
        content=ZhihuOther(
            type_raw="moments feed/复合类型",
            item_id="123",
            title="奇怪类型",
            excerpt=None,
            url="https://www.zhihu.com/x/123",
            author=None,
        ),
    )
    import re

    # Must not raise domain validation; the id stays SAFE_ID-clean while the
    # raw type is preserved in metadata.
    item = map_captured_item(favorite, collection_titles=(), observed_at=OBSERVED)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", item.source_id)
    assert item.source_id.endswith("-123")
    assert item.platform_metadata["content_type_raw"] == "moments feed/复合类型"


def test_map_video_answer_uses_video_title_as_body() -> None:
    from favhub.zhihu_models import ZhihuAnswer, ZhihuFavorite

    favorite = ZhihuFavorite(
        favorited_at=OBSERVED,
        content=ZhihuAnswer(
            answer_id="2188039034",
            url="https://www.zhihu.com/question/118452710/answer/2188039034",
            html="",
            excerpt=None,
            question_id="118452710",
            question_title="你看过最治愈的视频是什么？",
            author="某答主",
            voteup_count=669,
            created_at=OBSERVED,
            updated_at=None,
            video_title="看小豪猪啃着玉米棒子，还挺治愈的。",
        ),
    )
    item = map_captured_item(favorite, collection_titles=("我的收藏",), observed_at=OBSERVED)
    assert "## 视频" in item.body
    assert "看小豪猪" in item.body
    assert item.platform_metadata["video_title"] == "看小豪猪啃着玉米棒子，还挺治愈的。"


def test_deduplicate_sorts_merged_folder_titles() -> None:
    page = load_page("items-page-answer-article.json")
    answer = page.favorites[0]
    observations = deduplicate(
        {"9": [answer], "1": [answer]},
        {"9": "笔记", "1": "工具"},
    )
    assert observations[next(iter(observations))].collections == ("工具", "笔记")


def test_deduplicate_keeps_earliest_favorite_and_merges_folders() -> None:
    page = load_page("items-page-answer-article.json")
    answer = page.favorites[0]
    from dataclasses import replace

    earlier = replace(answer, favorited_at=datetime(2022, 1, 1, tzinfo=UTC))
    observations = deduplicate(
        {"100": [answer], "200": [earlier]},
        {"100": "示例收藏夹", "200": "编程"},
    )

    assert list(observations) == ["answer-1776858097"]
    observation = observations["answer-1776858097"]
    assert observation.favorite.favorited_at == datetime(2022, 1, 1, tzinfo=UTC)
    assert observation.collections == ("示例收藏夹", "编程")
