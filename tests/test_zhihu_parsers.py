import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.capture import LOGIN_REQUIRED, PAGE_CHANGED, RATE_LIMITED, CaptureError
from favhub.html_text import render_text
from favhub.zhihu_models import ZhihuAnswer, ZhihuArticle, ZhihuOther
from favhub.zhihu_parsers import parse_collections_page, parse_items_page

FIXTURES = Path(__file__).parent / "fixtures" / "zhihu"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_collections_page_extracts_scopes() -> None:
    collections = parse_collections_page(load("collections-page-1.json"))
    assert [c.scope_id for c in collections] == ["753110114", "844560325", "210943971"]
    first = collections[0]
    assert first.title == "我的收藏"
    assert first.item_count == 104
    assert first.is_default is True


def test_parse_items_page_answer_and_article() -> None:
    page = parse_items_page(load("items-page-answer-article.json"))
    assert page.is_end is True
    answer_fav, article_fav = page.favorites

    # 2023-11-04T15:22:13+08:00 → aware UTC
    assert answer_fav.favorited_at == datetime(2023, 11, 4, 7, 22, 13, tzinfo=UTC)
    answer = answer_fav.content
    assert isinstance(answer, ZhihuAnswer)
    assert answer.answer_id == "1776858097"
    assert answer.question_title == "示例标题22：用于验证解析与索引行为"
    assert answer.url.endswith("/answer/1776858097")
    assert answer.author == "示例答主二"
    assert answer.voteup_count == 282
    assert answer.created_at is not None and answer.created_at.tzinfo is not None
    assert len(answer.html) > 100

    article = article_fav.content
    assert isinstance(article, ZhihuArticle)
    assert article.title.startswith("示例标题")
    assert "zhuanlan.zhihu.com" in article.url
    assert article.author == "示例名称5"
    assert article.created_at is not None and article.created_at.tzinfo is not None
    assert len(article.html) > 100


def test_short_page_is_not_end() -> None:
    page = parse_items_page(load("items-page-short-not-end.json"))
    assert page.is_end is False
    assert len(page.favorites) == 3


def test_unknown_content_type_degrades_without_failing_the_page() -> None:
    page = parse_items_page(load("item-unknown-type.json"))
    other = page.favorites[0].content
    assert isinstance(other, ZhihuOther)
    assert other.type_raw == "zvideo"
    assert other.title == "示例视频标题"
    assert other.excerpt == "视频摘要示例"
    assert other.url == "https://www.zhihu.com/zvideo/987654321"


@pytest.mark.parametrize(
    ("fixture", "expected_code"),
    [
        ("login-required.json", LOGIN_REQUIRED),
        ("rate-limited.json", RATE_LIMITED),
        ("page-changed.json", PAGE_CHANGED),
    ],
)
def test_error_envelopes_are_typed(fixture: str, expected_code: str) -> None:
    for parse in (parse_collections_page, parse_items_page):
        with pytest.raises(CaptureError) as error:
            parse(load(fixture))
        assert error.value.code == expected_code


def test_non_mapping_payload_is_page_changed() -> None:
    with pytest.raises(CaptureError) as error:
        parse_items_page("<html>")
    assert error.value.code == PAGE_CHANGED


def test_missing_or_malformed_paging_is_page_changed_not_quiet_end() -> None:
    entry = load("item-unknown-type.json")["data"][0]
    for paging in (None, "oops", {}, {"is_end": "yes"}):
        payload: dict = {"data": [entry]}
        if paging is not None:
            payload["paging"] = paging
        with pytest.raises(CaptureError) as error:
            parse_items_page(payload)
        assert error.value.code == PAGE_CHANGED


def test_error_envelopes_map_by_code_without_keyword_wording() -> None:
    login = {"error": {"message": "unexpected wording", "code": 100}}
    limited = {"error": {"message": "slow down please", "code": 4039}}
    with pytest.raises(CaptureError) as first:
        parse_items_page(login)
    assert first.value.code == LOGIN_REQUIRED
    with pytest.raises(CaptureError) as second:
        parse_items_page(limited)
    assert second.value.code == RATE_LIMITED


def test_video_answer_with_empty_body_parses_and_keeps_video_title() -> None:
    # Live contract: video answers carry an empty `content` string and a
    # VIDEO attachment; they must never fail the page.
    payload = {
        "data": [
            {
                "created": "2022-08-16T10:00:00+08:00",
                "content": {
                    "type": "answer",
                    "id": "2188039034",
                    "url": "https://www.zhihu.com/question/118452710/answer/2188039034",
                    "content": "",
                    "excerpt": "",
                    "question": {"id": "118452710", "title": "你看过最治愈的视频是什么？"},
                    "author": {"name": "某答主"},
                    "voteup_count": 669,
                    "created_time": 1660000000,
                    "updated_time": 1660000000,
                    "attachment": {
                        "type": "VIDEO",
                        "video": {"title": "看小豪猪啃着玉米棒子，还挺治愈的。"},
                    },
                },
            }
        ],
        "paging": {"is_end": True, "totals": 1},
    }
    answer = parse_items_page(payload).favorites[0].content
    assert isinstance(answer, ZhihuAnswer)
    assert answer.html == ""
    assert answer.video_title == "看小豪猪啃着玉米棒子，还挺治愈的。"


def test_degraded_entry_without_id_still_parses_with_stable_identity() -> None:
    payload = {
        "data": [
            {
                "created": "2026-01-01T12:00:00+08:00",
                "content": {
                    "type": "pin",
                    "title": "无 id 的想法",
                    "url": "https://www.zhihu.com/pin/unknown",
                },
            }
        ],
        "paging": {"is_end": True, "totals": 1},
    }
    first = parse_items_page(payload).favorites[0].content
    second = parse_items_page(payload).favorites[0].content
    assert isinstance(first, ZhihuOther)
    assert first.item_id and first.item_id == second.item_id  # deterministic


def test_render_text_paragraphs_lists_and_code() -> None:
    html = (
        "<p>第一段</p><p>第二段<br/>换行</p>"
        "<ol><li>甲</li><li>乙</li></ol>"
        "<ul><li>丙</li></ul>"
        "<pre><code>print('hi')\nprint('bye')</code></pre>"
    )
    text, images = render_text(html)
    assert images == ()
    assert "第一段\n\n第二段\n换行" in text
    assert "1. 甲\n2. 乙" in text
    assert "- 丙" in text
    assert "```\nprint('hi')\nprint('bye')\n```" in text


def test_render_text_links_and_images() -> None:
    html = (
        '<p>见 <a href="https://link.zhihu.com/?target=https%3A//example.com/a%3Fx%3D1">'
        '示例站</a> 与 <a href="https://zhuanlan.zhihu.com/p/1">专栏</a></p>'
        '<figure><img src="https://pica.zhimg.com/v2-abc_1440w.jpg" data-rawwidth="1080"/></figure>'
    )
    text, images = render_text(html)
    assert "示例站 (https://example.com/a?x=1)" in text  # redirect unwrapped
    assert "专栏 (https://zhuanlan.zhihu.com/p/1)" in text
    assert images == ("https://pica.zhimg.com/v2-abc_1440w.jpg",)
    assert "zhimg" not in text  # images never enter the text body


def test_render_text_is_deterministic_on_real_fixture_html() -> None:
    page = load("items-page-answer-article.json")
    for item in page["data"]:
        html = item["content"]["content"]
        first = render_text(html)
        assert first == render_text(html)
        text, _ = first
        assert text.strip()
        assert "<" not in text  # no residual tags
