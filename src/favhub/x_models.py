"""Pure value types for X (bookmarks) captures.

No browser, network, or storage concerns; parsers in ``x_parsers`` produce
these from redacted structured responses, and ``x_mapping`` turns them into
platform-neutral captured items.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class XMedia:
    media_type: str  # "photo" | "video" | "animated_gif" (poster URL only)
    url: str
    alt: str | None


@dataclass(frozen=True, slots=True)
class XQuotedTweet:
    tweet_id: str | None
    author: str | None
    handle: str | None
    text: str
    unavailable: bool = False


@dataclass(frozen=True, slots=True)
class XTweet:
    tweet_id: str
    text: str
    author: str | None
    handle: str | None
    created_at: datetime
    media: tuple[XMedia, ...]
    quoted: XQuotedTweet | None


@dataclass(frozen=True, slots=True)
class XTombstone:
    tweet_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class XBookmarksPage:
    tweets: tuple[XTweet | XTombstone, ...]
    bottom_cursor: str | None
    has_more: bool
