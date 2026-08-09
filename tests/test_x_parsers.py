import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.capture import LOGIN_REQUIRED, PAGE_CHANGED, CaptureError
from favhub.x_models import XMedia, XTombstone, XTweet
from favhub.x_parsers import parse_bookmarks_page, parse_timeline_entry

FIXTURES = Path(__file__).parent / "fixtures" / "x"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_page_orders_tweets_and_reports_cursor() -> None:
    page = parse_bookmarks_page(load("bookmarks-page-1.json"))
    assert page.has_more is True
    assert page.bottom_cursor == "HBaKufT89rLF+DMAAA=="
    assert [t.tweet_id for t in page.tweets] == [
        "1011048574412526505",
        "1816024121722501770",
        "1112444407014444922",
    ]
    first = page.tweets[0]
    assert isinstance(first, XTweet)
    assert first.author == "示例作者一"
    assert first.handle == "example_one"
    assert first.created_at == datetime(2026, 7, 26, 8, 10, 25, tzinfo=UTC)
    # note_tweet full text takes precedence over the truncated legacy text.
    assert first.text.startswith("这是第1段示例正文")
    second = page.tweets[1]
    assert isinstance(second, XTweet)
    assert second.text.startswith("Example line 1 exercising")


def test_parse_second_page_uses_next_cursor() -> None:
    page = parse_bookmarks_page(load("bookmarks-page-2.json"))
    assert page.has_more is True
    assert page.bottom_cursor == "HBaKhaWZsNzp9zMAAA=="
    assert len(page.tweets) == 2


def test_terminal_page_with_only_cursors_ends_pagination() -> None:
    payload = {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {
                    "instructions": [
                        {
                            "type": "TimelineAddEntries",
                            "entries": [
                                {
                                    "entryId": "cursor-bottom-1",
                                    "content": {
                                        "entryType": "TimelineTimelineCursor",
                                        "cursorType": "Bottom",
                                        "value": "END==",
                                    },
                                }
                            ],
                        }
                    ]
                }
            }
        }
    }
    page = parse_bookmarks_page(payload)
    assert page.tweets == ()
    assert page.has_more is False
    assert page.bottom_cursor == "END=="


def test_images_entry_normalizes_media_with_optional_alt() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    assert isinstance(tweet, XTweet)
    assert tweet.tweet_id == "1232164438310380159"
    assert tweet.handle == "example_six"
    assert tweet.text.startswith("这是第1段示例正文")
    assert tweet.media == (
        XMedia(
            media_type="photo",
            url="https://pbs.twimg.com/media/eaigv5jeo74nCR3.jpg",
            alt=None,
        ),
    )


def test_quote_entry_extracts_visible_quoted_tweet() -> None:
    tweet = parse_timeline_entry(load("tweet-with-quote.json"))
    assert isinstance(tweet, XTweet)
    assert tweet.quoted is not None
    assert tweet.quoted.tweet_id == "1249345985356198626"
    assert tweet.quoted.author == "示例作者一"
    assert tweet.quoted.handle == "example_one"
    assert tweet.quoted.text.startswith("这是第1段示例正文")
    assert tweet.quoted.unavailable is False
    assert tweet.media and tweet.media[0].media_type == "video"


def test_tombstone_entry_is_typed_not_dropped() -> None:
    result = parse_timeline_entry(load("tombstone.json"))
    assert isinstance(result, XTombstone)
    assert result.tweet_id == "1141755651528528315"
    assert "withheld" in result.reason


def test_logged_out_is_login_required() -> None:
    with pytest.raises(CaptureError) as error:
        parse_bookmarks_page(load("logged-out.json"))
    assert error.value.code == LOGIN_REQUIRED


def test_incidental_logged_wording_is_not_login_required() -> None:
    payload = {"errors": [{"message": "Operation logged for audit", "code": 500}]}
    with pytest.raises(CaptureError) as error:
        parse_bookmarks_page(payload)
    assert error.value.code == PAGE_CHANGED


def test_changed_schema_is_page_changed_not_empty() -> None:
    with pytest.raises(CaptureError) as error:
        parse_bookmarks_page(load("page-changed.json"))
    assert error.value.code == PAGE_CHANGED


def test_non_mapping_payload_is_page_changed() -> None:
    with pytest.raises(CaptureError) as error:
        parse_bookmarks_page("<html>login</html>")
    assert error.value.code == PAGE_CHANGED


def test_tweet_without_rest_id_is_page_changed() -> None:
    entry = {
        "entryId": "tweet-1",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {
                "tweet_results": {"result": {"__typename": "Tweet", "legacy": {"full_text": "x"}}}
            },
        },
    }
    with pytest.raises(CaptureError) as error:
        parse_timeline_entry(entry)
    assert error.value.code == PAGE_CHANGED


def test_invalid_created_at_is_page_changed() -> None:
    entry = json.loads(json.dumps(load("tweet-with-images.json")))
    entry["content"]["itemContent"]["tweet_results"]["result"]["legacy"]["created_at"] = "昨天"
    with pytest.raises(CaptureError) as error:
        parse_timeline_entry(entry)
    assert error.value.code == PAGE_CHANGED


def _entry_with_result(result: object) -> dict:
    return {
        "entryId": "tweet-1",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"tweet_results": {"result": result}},
        },
    }


def _minimal_tweet(**overrides: object) -> dict:
    result: dict = {
        "__typename": "Tweet",
        "rest_id": "1234567890",
        "core": {"user_results": {"result": {"core": {"name": "作者", "screen_name": "author"}}}},
        "legacy": {"full_text": "正文", "created_at": "Sun Jul 26 08:10:25 +0000 2026"},
    }
    result.update(overrides)
    return result


def _page_of(entries: list) -> dict:
    return {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": entries}]}
            }
        }
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"data": "not-a-mapping"},
        {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": "no"}}}},
        {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": ["no"]}}}},
        {
            "data": {
                "bookmark_timeline_v2": {
                    "timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": "no"}]}
                }
            }
        },
        _page_of(["no"]),
        _page_of([{"entryId": "e"}]),
        _page_of(
            [
                {
                    "entryId": "cursor-bottom-1",
                    "content": {
                        "entryType": "TimelineTimelineCursor",
                        "cursorType": "Bottom",
                    },
                }
            ]
        ),
        _page_of([{"entryId": "e", "content": {"entryType": "SomethingNew"}}]),
        {"errors": [{"message": "Something exploded", "code": 500}]},
    ],
)
def test_malformed_pages_are_page_changed(payload: dict) -> None:
    with pytest.raises(CaptureError) as error:
        parse_bookmarks_page(payload)
    assert error.value.code == PAGE_CHANGED


def test_non_add_entries_instructions_are_ignored() -> None:
    payload = {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {
                    "instructions": [
                        {"type": "TimelineClearCache"},
                        {
                            "type": "TimelineAddEntries",
                            "entries": [_entry_with_result(_minimal_tweet())],
                        },
                    ]
                }
            }
        }
    }
    page = parse_bookmarks_page(payload)
    assert len(page.tweets) == 1


@pytest.mark.parametrize(
    "entry",
    [
        {"entryId": "e", "content": {"entryType": "TimelineTimelineItem"}},
        {
            "entryId": "e",
            "content": {
                "entryType": "TimelineTimelineItem",
                "itemContent": {"tweet_results": {}},
            },
        },
        _entry_with_result({"__typename": "TweetPreviewCard"}),
        _entry_with_result({"__typename": "TweetWithVisibilityResults"}),
        _entry_with_result(_minimal_tweet(legacy="not-a-mapping")),
        _entry_with_result(
            _minimal_tweet(legacy={"full_text": 5, "created_at": "Sun Jul 26 08:10:25 +0000 2026"})
        ),
        _entry_with_result(_minimal_tweet(legacy={"full_text": "正文"})),
        _entry_with_result(
            _minimal_tweet(
                legacy={
                    "full_text": "正文",
                    "created_at": "Sun Jul 26 08:10:25 +0000 2026",
                    "extended_entities": {"media": "no"},
                }
            )
        ),
        _entry_with_result(
            _minimal_tweet(
                legacy={
                    "full_text": "正文",
                    "created_at": "Sun Jul 26 08:10:25 +0000 2026",
                    "extended_entities": {"media": [{"type": "photo"}]},
                }
            )
        ),
        _entry_with_result(
            _minimal_tweet(
                legacy={
                    "full_text": "正文",
                    "created_at": "Sun Jul 26 08:10:25 +0000 2026",
                    "extended_entities": {"media": [{"media_url_https": "https://x/y.jpg"}]},
                }
            )
        ),
        _entry_with_result(_minimal_tweet(quoted_status_result={"result": {"__typename": "Odd"}})),
        {
            "entryId": "tweet-not-numeric",
            "content": {
                "entryType": "TimelineTimelineItem",
                "itemContent": {
                    "tweet_results": {"result": {"__typename": "TweetTombstone", "tombstone": {}}}
                },
            },
        },
    ],
)
def test_malformed_entries_are_page_changed(entry: dict) -> None:
    with pytest.raises(CaptureError) as error:
        parse_timeline_entry(entry)
    assert error.value.code == PAGE_CHANGED


def test_visibility_wrapper_unwraps_to_tweet() -> None:
    wrapped = _entry_with_result(
        {"__typename": "TweetWithVisibilityResults", "tweet": _minimal_tweet()}
    )
    tweet = parse_timeline_entry(wrapped)
    assert isinstance(tweet, XTweet)
    assert tweet.tweet_id == "1234567890"


def test_author_falls_back_to_user_legacy_layout() -> None:
    result = _minimal_tweet(
        core={
            "user_results": {
                "result": {"legacy": {"name": "旧版作者", "screen_name": "legacy_author"}}
            }
        }
    )
    tweet = parse_timeline_entry(_entry_with_result(result))
    assert isinstance(tweet, XTweet)
    assert tweet.author == "旧版作者"
    assert tweet.handle == "legacy_author"


def test_quoted_tombstone_is_marked_unavailable() -> None:
    result = _minimal_tweet(
        legacy={
            "full_text": "正文",
            "created_at": "Sun Jul 26 08:10:25 +0000 2026",
            "quoted_status_id_str": "999",
        },
        quoted_status_result={
            "result": {
                "__typename": "TweetTombstone",
                "tombstone": {"text": {"text": "quoted gone"}},
            }
        },
    )
    tweet = parse_timeline_entry(_entry_with_result(result))
    assert isinstance(tweet, XTweet)
    assert tweet.quoted is not None
    assert tweet.quoted.unavailable is True
    assert tweet.quoted.tweet_id == "999"
    assert tweet.quoted.text == "quoted gone"


def test_quoted_visibility_wrapper_unwraps() -> None:
    result = _minimal_tweet(
        quoted_status_result={
            "result": {
                "__typename": "TweetWithVisibilityResults",
                "tweet": _minimal_tweet(rest_id="777"),
            }
        }
    )
    tweet = parse_timeline_entry(_entry_with_result(result))
    assert isinstance(tweet, XTweet)
    assert tweet.quoted is not None
    assert tweet.quoted.tweet_id == "777"


def test_empty_tweet_result_with_id_is_tombstone() -> None:
    entry = {
        "entryId": "tweet-2079000000000000000",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"tweet_results": {}},
        },
    }
    result = parse_timeline_entry(entry)
    assert isinstance(result, XTombstone)
    assert result.tweet_id == "2079000000000000000"
    assert result.reason == "empty tweet result"
