import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.capture import SOURCE_UNAVAILABLE
from favhub.x_mapping import map_captured_item
from favhub.x_parsers import parse_timeline_entry

FIXTURES = Path(__file__).parent / "fixtures" / "x"
OBSERVED = datetime(2026, 7, 26, 12, tzinfo=UTC)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_map_tweet_with_image_description_emits_ocr_asset() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    description = "Fiverr 订单列表截图：AI 服务 关键词 报价"

    item = map_captured_item(tweet, image_descriptions=[description], observed_at=OBSERVED)

    assert item.platform == "x"
    assert item.source_id == "1232164438310380159"
    assert item.canonical_url == "https://x.com/example_six/status/1232164438310380159"
    assert item.extractor_version == "x-browser-v1"
    assert item.title.startswith("这是第1段示例正文")
    assert len(item.title) <= 80
    assert item.body.startswith("这是第1段示例正文")
    assert len(item.assets) == 1
    asset = item.assets[0]
    assert asset.relative_path == "ocr/0001.md"
    assert description in asset.text
    assert item.platform_metadata is not None
    media = item.platform_metadata["media"]
    assert media[0]["type"] == "photo"
    assert media[0]["ocr_status"] == "available"
    assert media[0]["url"] == "https://pbs.twimg.com/media/eaigv5jeo74nCR3.jpg"


def test_map_tweet_without_description_marks_missing() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    item = map_captured_item(tweet, image_descriptions=None, observed_at=OBSERVED)
    assert item.assets == ()
    assert item.platform_metadata is not None
    assert item.platform_metadata["media"][0]["ocr_status"] == "missing"


def test_map_quoted_tweet_inlines_quote_section() -> None:
    tweet = parse_timeline_entry(load("tweet-with-quote.json"))
    item = map_captured_item(tweet, image_descriptions=None, observed_at=OBSERVED)
    assert "## 引用推文" in item.body
    assert "@example_one" in item.body
    assert "这是第1段示例正文" in item.body
    assert item.platform_metadata is not None
    assert item.platform_metadata["quoted_tweet_id"] == "1249345985356198626"
    # Video media keeps a poster URL only and is never downloaded.
    assert item.platform_metadata["media"][0]["type"] == "video"
    assert item.platform_metadata["media"][0]["ocr_status"] == "skipped"


def test_map_tombstone_keeps_metadata_with_stable_code() -> None:
    tombstone = parse_timeline_entry(load("tombstone.json"))
    item = map_captured_item(tombstone, image_descriptions=None, observed_at=OBSERVED)
    assert item.source_id == "1141755651528528315"
    assert item.canonical_url == "https://x.com/i/web/status/1141755651528528315"
    assert item.published_at == OBSERVED
    assert item.platform_metadata is not None
    assert item.platform_metadata["source_status"] == SOURCE_UNAVAILABLE
    assert item.platform_metadata["published_at_estimated"] is True
    assert "withheld" in item.platform_metadata["tombstone_reason"]
    assert "1141755651528528315" in item.title


def test_description_count_must_match_media_count() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    with pytest.raises(ValueError, match="image_descriptions"):
        map_captured_item(tweet, image_descriptions=["a", "b"], observed_at=OBSERVED)


def test_failed_ocr_attempt_is_recorded_per_image() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    item = map_captured_item(
        tweet, image_descriptions=[None], observed_at=OBSERVED, failed_indexes=(0,)
    )
    assert item.assets == ()
    assert item.platform_metadata is not None
    assert item.platform_metadata["media"][0]["ocr_status"] == "failed"


@pytest.mark.parametrize("failed", [(5,), ("0",)])
def test_failed_indexes_must_be_valid(failed: tuple) -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    with pytest.raises(ValueError, match="failed_indexes"):
        map_captured_item(
            tweet, image_descriptions=None, observed_at=OBSERVED, failed_indexes=failed
        )


def test_failed_index_conflicts_with_description() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    with pytest.raises(ValueError, match="conflicts"):
        map_captured_item(
            tweet, image_descriptions=["描述"], observed_at=OBSERVED, failed_indexes=(0,)
        )


def test_x_favorited_at_is_estimated_from_observation() -> None:
    tweet = parse_timeline_entry(load("tweet-with-images.json"))
    item = map_captured_item(tweet, image_descriptions=None, observed_at=OBSERVED)
    assert item.platform_metadata["favorited_at"] == "2026-07-26T12:00:00Z"
    assert item.platform_metadata["favorited_at_estimated"] is True

    tombstone = parse_timeline_entry(load("tombstone.json"))
    tomb_item = map_captured_item(tombstone, image_descriptions=None, observed_at=OBSERVED)
    assert tomb_item.platform_metadata["favorited_at_estimated"] is True
