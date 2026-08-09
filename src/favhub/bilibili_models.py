"""Pure value types for Bilibili captures.

These types carry no browser, network, or storage concerns. Parsers in
``bilibili_parsers`` produce them from redacted structured responses, and the
mapper in ``bilibili_mapping`` turns them into platform-neutral captured items.
"""

from dataclasses import dataclass
from datetime import datetime

# Stable capture error codes live in favhub.capture and are re-exported here
# for backward compatibility with existing imports.
from favhub.capture import (  # noqa: F401
    CAPTURE_ERROR_CODES,
    LOGIN_REQUIRED,
    MALFORMED_SUBTITLE,
    PAGE_CHANGED,
    SOURCE_UNAVAILABLE,
    SUBTITLE_UNAVAILABLE,
    CaptureError,
)


class BilibiliCaptureError(CaptureError):
    """Backward-compatible alias for the shared capture error."""


@dataclass(frozen=True, slots=True)
class BilibiliFolder:
    scope_id: str
    title: str
    media_count: int


@dataclass(frozen=True, slots=True)
class BilibiliListEntry:
    bvid: str
    title: str
    author: str | None
    author_mid: str | None
    intro: str
    cover_url: str | None
    published_at: datetime
    fav_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class BilibiliResourcePage:
    entries: tuple[BilibiliListEntry, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class BilibiliVideo:
    bvid: str
    aid: int | None
    title: str
    author: str | None
    author_mid: str | None
    description: str
    cover_url: str | None
    published_at: datetime
    duration: int | None
    cid: int | None


@dataclass(frozen=True, slots=True)
class BilibiliSubtitleCue:
    start: float
    end: float
    content: str


@dataclass(frozen=True, slots=True)
class BilibiliSubtitle:
    language: str
    cues: tuple[BilibiliSubtitleCue, ...]


__all__ = [
    "CAPTURE_ERROR_CODES",
    "LOGIN_REQUIRED",
    "MALFORMED_SUBTITLE",
    "PAGE_CHANGED",
    "SOURCE_UNAVAILABLE",
    "SUBTITLE_UNAVAILABLE",
    "BilibiliCaptureError",
    "BilibiliFolder",
    "BilibiliListEntry",
    "BilibiliResourcePage",
    "BilibiliSubtitle",
    "BilibiliSubtitleCue",
    "BilibiliVideo",
    "CaptureError",
]
