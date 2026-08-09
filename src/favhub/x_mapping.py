"""Map parsed X values into platform-neutral captured items.

Pure module: no network, browser, or filesystem access. One bookmark becomes
one :class:`CapturedItem`; visible quoted tweets are inlined into the body;
image binaries are never downloaded — each image keeps its URL/alt in
``platform_metadata`` and an Agent-provided OCR/visual description becomes an
``ocr/NNNN.md`` text asset.
"""

from collections.abc import Collection, Sequence
from datetime import datetime

from favhub.capture import SOURCE_UNAVAILABLE
from favhub.domain import CapturedAsset, CapturedItem, isoformat, sha256_text
from favhub.x_models import XTombstone, XTweet

EXTRACTOR_VERSION = "x-browser-v1"
_TITLE_LIMIT = 80


def _status_fallback_url(tweet_id: str) -> str:
    """Handle-less permalink used for tombstones and unknown authors."""
    return f"https://x.com/i/web/status/{tweet_id}"


def map_captured_item(
    tweet: XTweet | XTombstone,
    *,
    image_descriptions: Sequence[str | None] | None,
    observed_at: datetime,
    extractor_version: str = EXTRACTOR_VERSION,
    failed_indexes: Collection[int] | None = None,
) -> CapturedItem:
    """Map one bookmark to a captured item.

    ``image_descriptions`` aligns with ``tweet.media`` (``None`` = not
    attempted). ``failed_indexes`` marks media whose OCR/visual description
    was attempted but failed, yielding ``ocr_status: "failed"``.
    """
    if isinstance(tweet, XTombstone):
        return _map_tombstone(tweet, observed_at, extractor_version)
    return _map_tweet(tweet, image_descriptions, observed_at, extractor_version, failed_indexes)


def _map_tombstone(
    tombstone: XTombstone, observed_at: datetime, extractor_version: str
) -> CapturedItem:
    return CapturedItem(
        platform="x",
        source_id=tombstone.tweet_id,
        canonical_url=_status_fallback_url(tombstone.tweet_id),
        title=f"已失效推文 {tombstone.tweet_id}",
        author=None,
        # The platform hides the original timestamp; the estimate is flagged.
        published_at=observed_at,
        observed_at=observed_at,
        body="",
        collections=(),
        extractor_version=extractor_version,
        platform_metadata={
            "source_status": SOURCE_UNAVAILABLE,
            "tombstone_reason": tombstone.reason,
            "published_at_estimated": True,
            "favorited_at": isoformat(observed_at),
            "favorited_at_estimated": True,
        },
    )


def _map_tweet(
    tweet: XTweet,
    image_descriptions: Sequence[str | None] | None,
    observed_at: datetime,
    extractor_version: str,
    failed_indexes: Collection[int] | None,
) -> CapturedItem:
    if image_descriptions is not None and len(image_descriptions) != len(tweet.media):
        raise ValueError(
            "image_descriptions must have one entry per media item "
            f"({len(image_descriptions)} != {len(tweet.media)})"
        )
    failed = frozenset(failed_indexes or ())
    for index in failed:
        if not isinstance(index, int) or not 0 <= index < len(tweet.media):
            raise ValueError(f"failed_indexes entry out of range: {index!r}")
        if image_descriptions is not None and image_descriptions[index] is not None:
            raise ValueError(f"failed_indexes entry {index} conflicts with a provided description")

    body_parts = [tweet.text.strip()]
    if tweet.quoted is not None:
        if tweet.quoted.unavailable:
            quote_section = "## 引用推文\n\n（引用的推文已不可见）"
        else:
            quoted_by = (
                f"{tweet.quoted.author or ''} @{tweet.quoted.handle}"
                if tweet.quoted.handle
                else (tweet.quoted.author or "未知作者")
            )
            quote_section = f"## 引用推文\n\n{quoted_by}：\n\n{tweet.quoted.text.strip()}"
        body_parts.append(quote_section)
    if tweet.media:
        lines = [
            f"[{index}] {media.media_type}: {media.alt or media.url}"
            for index, media in enumerate(tweet.media, start=1)
        ]
        body_parts.append("## 媒体\n\n" + "\n".join(lines))

    assets: list[CapturedAsset] = []
    media_metadata: list[dict[str, object]] = []
    for index, media in enumerate(tweet.media):
        description = image_descriptions[index] if image_descriptions is not None else None
        if description is not None and description.strip():
            text = f"# 图{index + 1} OCR/视觉描述\n\n{description.strip()}\n"
            assets.append(
                CapturedAsset(
                    relative_path=f"ocr/{index + 1:04d}.md",
                    media_type="text/markdown",
                    text=text,
                    sha256=sha256_text(text),
                )
            )
            ocr_status = "available"
        elif index in failed:
            ocr_status = "failed"
        elif media.media_type == "photo":
            ocr_status = "missing"
        else:
            ocr_status = "skipped"
        entry: dict[str, object] = {
            "type": media.media_type,
            "url": media.url,
            "ocr_status": ocr_status,
        }
        if media.alt is not None:
            entry["alt"] = media.alt
        media_metadata.append(entry)

    platform_metadata: dict[str, object] = {
        "source_status": "available",
        # X exposes no bookmark timestamp; first-sync time is the proxy.
        "favorited_at": isoformat(observed_at),
        "favorited_at_estimated": True,
    }
    if tweet.handle is not None:
        platform_metadata["author_handle"] = tweet.handle
    if media_metadata:
        platform_metadata["media"] = media_metadata
    if tweet.quoted is not None and tweet.quoted.tweet_id:
        platform_metadata["quoted_tweet_id"] = tweet.quoted.tweet_id

    canonical_url = (
        f"https://x.com/{tweet.handle}/status/{tweet.tweet_id}"
        if tweet.handle
        else _status_fallback_url(tweet.tweet_id)
    )
    return CapturedItem(
        platform="x",
        source_id=tweet.tweet_id,
        canonical_url=canonical_url,
        title=_title(tweet),
        author=tweet.author,
        published_at=tweet.created_at,
        observed_at=observed_at,
        body="\n\n".join(part for part in body_parts if part),
        collections=(),
        extractor_version=extractor_version,
        platform_metadata=platform_metadata,
        assets=tuple(assets),
        # Only a capture that was handed descriptions knows what the OCR set
        # should be. A plain sync passes none and must leave what is already
        # there alone, or every image description in the library disappears the
        # next time its tweet is refreshed.
        authoritative_asset_roots=("ocr",) if image_descriptions is not None else (),
    )


def _title(tweet: XTweet) -> str:
    for line in tweet.text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_TITLE_LIMIT]
    return f"推文 {tweet.tweet_id}"
