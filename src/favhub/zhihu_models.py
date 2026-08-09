"""Typed observations parsed from Zhihu collections-API payloads."""

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "ZhihuAnswer",
    "ZhihuArticle",
    "ZhihuCollection",
    "ZhihuFavorite",
    "ZhihuItemsPage",
    "ZhihuOther",
]


@dataclass(frozen=True, slots=True)
class ZhihuCollection:
    scope_id: str
    title: str
    item_count: int
    is_default: bool


@dataclass(frozen=True, slots=True)
class ZhihuAnswer:
    answer_id: str
    url: str
    html: str  # empty for video answers (the content is the attachment)
    excerpt: str | None
    question_id: str | None
    question_title: str
    author: str | None
    voteup_count: int | None
    created_at: datetime | None
    updated_at: datetime | None
    video_title: str | None = None


@dataclass(frozen=True, slots=True)
class ZhihuArticle:
    article_id: str
    title: str
    url: str
    html: str
    excerpt: str | None
    author: str | None
    voteup_count: int | None
    created_at: datetime | None
    updated_at: datetime | None
    image_url: str | None


@dataclass(frozen=True, slots=True)
class ZhihuOther:
    """A content type outside answer/article (pin, zvideo, …), kept degraded."""

    type_raw: str
    item_id: str
    title: str | None
    excerpt: str | None
    url: str | None
    author: str | None


@dataclass(frozen=True, slots=True)
class ZhihuFavorite:
    favorited_at: datetime
    content: ZhihuAnswer | ZhihuArticle | ZhihuOther


@dataclass(frozen=True, slots=True)
class ZhihuItemsPage:
    favorites: tuple[ZhihuFavorite, ...]
    is_end: bool
