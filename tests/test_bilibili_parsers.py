import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.bilibili_models import (
    LOGIN_REQUIRED,
    MALFORMED_SUBTITLE,
    PAGE_CHANGED,
    SOURCE_UNAVAILABLE,
    SUBTITLE_UNAVAILABLE,
    BilibiliCaptureError,
)
from favhub.bilibili_parsers import (
    parse_folders,
    parse_resource_page,
    parse_subtitle,
    parse_video_detail,
)

FIXTURES = Path(__file__).parent / "fixtures" / "bilibili"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_folders_extracts_identity_and_names() -> None:
    folders = parse_folders(load("folders.json"))
    assert [(f.scope_id, f.title, f.media_count) for f in folders] == [
        ("108963847", "默认收藏夹", 597),
        ("2225813947", "Example 1", 38),
    ]


def test_parse_resource_page_orders_entries_and_reports_more() -> None:
    page = parse_resource_page(load("resources-page-1.json"))
    assert page.has_more is True
    assert [entry.bvid for entry in page.entries] == [
        "BV11dzv1qkfv",
        "BV1bkz2gvaz6",
        "BV1qvughybhv",
        "BV1aluwgpp23",
        "BV1ha97wj5am",
    ]
    first = page.entries[0]
    assert first.title == "示例标题17：用于验证解析与索引行为"
    assert first.author == "示例UP主四"
    assert first.published_at == datetime.fromtimestamp(1781695620, tz=UTC)


def test_parse_video_detail_normalizes_fields() -> None:
    video = parse_video_detail(load("video-detail.json"))
    assert video.bvid == "BV1bkz2gvaz6"
    assert video.title.startswith("示例标题")
    assert video.author == "示例UP主二"
    assert video.published_at == datetime.fromtimestamp(1783079085, tz=UTC)
    assert video.cid == 39620968956
    assert "示例简介" in video.description


def test_parse_subtitle_reads_live_document_shape() -> None:
    subtitle = parse_subtitle(load("subtitle.json"))
    # Live subtitle documents carry the language under "lang", not "lan".
    assert subtitle.language == "zh"
    assert len(subtitle.cues) == 20
    starts = [cue.start for cue in subtitle.cues]
    assert starts == sorted(starts)
    assert subtitle.cues[0].content == "示例内容2，用于验"


def test_parse_subtitle_sorts_and_dedupes_cues() -> None:
    payload = {
        "lan": "zh-CN",
        "body": [
            {"from": 0.0, "to": 2.8, "content": "第一句"},
            {"from": 2.8, "to": 6.4, "content": "重复句"},
            {"from": 2.8, "to": 6.4, "content": "重复句"},
            {"from": 10.5, "to": 14.0, "content": "最后一句"},
            {"from": 6.4, "to": 10.5, "content": "中间乱序"},
        ],
    }
    subtitle = parse_subtitle(payload)
    assert subtitle.language == "zh-CN"
    starts = [cue.start for cue in subtitle.cues]
    assert starts == sorted(starts)
    # The exact duplicate cue at 2.8 is removed.
    assert len(subtitle.cues) == 4
    assert subtitle.cues[-1].start == 10.5


def test_login_required_is_typed() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_folders(load("login-required.json"))
    assert error.value.code == LOGIN_REQUIRED


def test_changed_schema_is_typed_not_empty_list() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_folders(load("page-changed.json"))
    assert error.value.code == PAGE_CHANGED


def test_non_mapping_payload_is_page_changed() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_folders("<html>login</html>")
    assert error.value.code == PAGE_CHANGED


def test_resource_page_missing_bvid_is_page_changed() -> None:
    payload = {"code": 0, "data": {"medias": [{"title": "x", "pubtime": 1}], "has_more": False}}
    with pytest.raises(BilibiliCaptureError) as error:
        parse_resource_page(payload)
    assert error.value.code == PAGE_CHANGED


def test_video_detail_unavailable_code_is_source_unavailable() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_video_detail({"code": -404, "message": "not found", "data": None})
    assert error.value.code == SOURCE_UNAVAILABLE


def test_video_detail_login_code_is_login_required() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_video_detail({"code": -101, "message": "not logged in", "data": None})
    assert error.value.code == LOGIN_REQUIRED


def test_subtitle_without_cues_is_unavailable() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_subtitle({"lan": "zh-CN", "body": []})
    assert error.value.code == SUBTITLE_UNAVAILABLE


def test_subtitle_non_finite_timestamp_is_malformed() -> None:
    payload = {"lan": "zh", "body": [{"from": 0.0, "to": float("inf"), "content": "x"}]}
    with pytest.raises(BilibiliCaptureError) as error:
        parse_subtitle(payload)
    assert error.value.code == MALFORMED_SUBTITLE


def test_subtitle_error_envelope_is_malformed() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_subtitle({"code": 62002, "message": "稿件不可见"})
    assert error.value.code == MALFORMED_SUBTITLE


def test_subtitle_login_envelope_is_login_required() -> None:
    with pytest.raises(BilibiliCaptureError) as error:
        parse_subtitle({"code": -101, "message": "账号未登录"})
    assert error.value.code == LOGIN_REQUIRED


def test_subtitle_language_override() -> None:
    subtitle = parse_subtitle({"body": [{"from": 0.0, "to": 1.0, "content": "hi"}]}, language="en")
    assert subtitle.language == "en"


def test_resource_entries_expose_fav_time() -> None:
    page = parse_resource_page(load("resources-page-1.json"))
    first = page.entries[0]
    assert first.fav_time == datetime.fromtimestamp(1784957583, tz=UTC)


def test_missing_or_invalid_fav_time_is_none() -> None:
    payload = {
        "code": 0,
        "data": {
            "medias": [
                {"bvid": "BV1NOFAVTIME", "title": "t", "pubtime": 1},
                {"bvid": "BV1BADFAVTIM", "title": "t", "pubtime": 1, "fav_time": "昨天"},
            ],
            "has_more": False,
        },
    }
    page = parse_resource_page(payload)
    assert page.entries[0].fav_time is None
    assert page.entries[1].fav_time is None


def test_absurd_numeric_fav_time_is_none() -> None:
    payload = {
        "code": 0,
        "data": {
            "medias": [{"bvid": "BV1HUGEFAVTI", "title": "t", "pubtime": 1, "fav_time": 1e18}],
            "has_more": False,
        },
    }
    page = parse_resource_page(payload)
    assert page.entries[0].fav_time is None
