"""Pure parsers for Zhihu collections-API responses.

Payloads come from user-run, same-origin console captures on a logged-in
zhihu.com page; no credential is ever present in them. Error envelopes become
typed :class:`CaptureError` values, never empty success. ``paging.is_end`` is
the only end signal — deleted favorites shrink pages below the limit on
non-final pages, so short pages must never terminate a scan.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from favhub.capture import LOGIN_REQUIRED, PAGE_CHANGED, RATE_LIMITED, CaptureError
from favhub.zhihu_models import (
    ZhihuAnswer,
    ZhihuArticle,
    ZhihuCollection,
    ZhihuFavorite,
    ZhihuItemsPage,
    ZhihuOther,
)

_MAX_EPOCH_SECONDS = 4102444800  # 2100-01-01: rejects garbage without overflow


_LOGIN_ERROR_CODES = frozenset({100, 101})
_RATE_ERROR_CODES = frozenset({4039})


def parse_collections_page(payload: object) -> tuple[ZhihuCollection, ...]:
    page, data = _required_page(payload)
    del page
    return tuple(_parse_collection(entry) for entry in data)


def parse_items_page(payload: object) -> ZhihuItemsPage:
    page, data = _required_page(payload)
    paging = page.get("paging")
    if not isinstance(paging, Mapping) or not isinstance(paging.get("is_end"), bool):
        # A dropped paging block must never read as a quiet end of scan.
        raise CaptureError(PAGE_CHANGED, "paging.is_end is missing")
    return ZhihuItemsPage(
        favorites=tuple(_parse_favorite(entry) for entry in data),
        is_end=paging["is_end"],
    )


def _required_page(payload: object) -> tuple[Mapping[str, Any], list[Any]]:
    if not isinstance(payload, Mapping):
        raise CaptureError(PAGE_CHANGED, "response is not an object")
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message", ""))
        name = str(error.get("name", ""))
        code = error.get("code")
        if code in _LOGIN_ERROR_CODES or "登录" in message or "Authentication" in name:
            raise CaptureError(LOGIN_REQUIRED, "session is logged out")
        if code in _RATE_ERROR_CODES or "频繁" in message or "异常" in message:
            raise CaptureError(RATE_LIMITED, "request frequency limited")
        raise CaptureError(PAGE_CHANGED, "unexpected error envelope")
    data = payload.get("data")
    if not isinstance(data, list):
        raise CaptureError(PAGE_CHANGED, "data is not an array")
    return payload, data


def _parse_collection(entry: object) -> ZhihuCollection:
    if not isinstance(entry, Mapping):
        raise CaptureError(PAGE_CHANGED, "collection entry is not an object")
    identifier = entry.get("id")
    if not isinstance(identifier, int) or isinstance(identifier, bool):
        raise CaptureError(PAGE_CHANGED, "collection id is not a number")
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise CaptureError(PAGE_CHANGED, "collection has no title")
    item_count = entry.get("item_count")
    return ZhihuCollection(
        scope_id=str(identifier),
        title=title,
        item_count=item_count if isinstance(item_count, int) else 0,
        is_default=bool(entry.get("is_default")),
    )


def _parse_favorite(entry: object) -> ZhihuFavorite:
    if not isinstance(entry, Mapping):
        raise CaptureError(PAGE_CHANGED, "favorite entry is not an object")
    favorited_at = _iso_datetime(entry.get("created"))
    if favorited_at is None:
        raise CaptureError(PAGE_CHANGED, "favorite has no created time")
    content = entry.get("content")
    if not isinstance(content, Mapping):
        raise CaptureError(PAGE_CHANGED, "favorite has no content")
    return ZhihuFavorite(favorited_at=favorited_at, content=_parse_content(content))


def _parse_content(content: Mapping[str, Any]) -> ZhihuAnswer | ZhihuArticle | ZhihuOther:
    type_raw = str(content.get("type") or "")
    if type_raw == "answer":
        question = content.get("question")
        question_map = question if isinstance(question, Mapping) else {}
        title = question_map.get("title")
        if not isinstance(title, str) or not title.strip():
            raise CaptureError(PAGE_CHANGED, "answer has no question title")
        return ZhihuAnswer(
            answer_id=_required_id(content),
            url=_required_url(content),
            html=_optional_html(content),
            excerpt=_optional_str(content, "excerpt"),
            question_id=_optional_id(question_map.get("id")),
            question_title=title,
            author=_author_name(content),
            voteup_count=_optional_int(content, "voteup_count"),
            created_at=_epoch_datetime(content.get("created_time")),
            updated_at=_epoch_datetime(content.get("updated_time")),
            video_title=_attachment_video_title(content),
        )
    if type_raw == "article":
        title = content.get("title")
        if not isinstance(title, str) or not title.strip():
            raise CaptureError(PAGE_CHANGED, "article has no title")
        return ZhihuArticle(
            article_id=_required_id(content),
            title=title,
            url=_required_url(content),
            html=_optional_html(content),
            excerpt=_optional_str(content, "excerpt"),
            author=_author_name(content),
            voteup_count=_optional_int(content, "voteup_count"),
            created_at=_epoch_datetime(content.get("created")),
            updated_at=_epoch_datetime(content.get("updated")),
            image_url=_optional_str(content, "image_url"),
        )
    # pin / zvideo / future types: degrade, never fail the page.
    title = _optional_str(content, "title")
    url = _optional_str(content, "url")
    identifier = content.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int | str):
        item_id = None
    else:
        item_id = str(identifier).strip() or None
    if item_id is None:
        # No id in a degraded entry: derive a stable one so the item keeps a
        # deterministic identity across scans instead of failing the page.
        digest = sha256(f"{type_raw}|{url or ''}|{title or ''}".encode()).hexdigest()
        item_id = digest[:16]
    return ZhihuOther(
        type_raw=type_raw or "unknown",
        item_id=item_id,
        title=title,
        excerpt=_optional_str(content, "excerpt"),
        url=url,
        author=_author_name(content),
    )


def _optional_id(value: object) -> str | None:
    # Live contract: numeric ids arrive as int in some payloads (collection
    # ids) and as decimal strings in others (question ids).
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    text = str(value).strip()
    return text if text.isdigit() else None


def _required_id(content: Mapping[str, Any]) -> str:
    identifier = content.get("id")
    if isinstance(identifier, bool) or not isinstance(identifier, int | str):
        raise CaptureError(PAGE_CHANGED, "content id is missing")
    text = str(identifier).strip()
    if not text:
        raise CaptureError(PAGE_CHANGED, "content id is empty")
    return text


def _required_url(content: Mapping[str, Any]) -> str:
    url = content.get("url")
    if not isinstance(url, str) or not url.startswith("http"):
        raise CaptureError(PAGE_CHANGED, "content url is missing")
    return url


def _optional_html(content: Mapping[str, Any]) -> str:
    # Live contract: video answers legitimately carry an empty body string —
    # the content is the VIDEO attachment. Empty is honest, never a failure.
    html = content.get("content")
    return html if isinstance(html, str) else ""


def _attachment_video_title(content: Mapping[str, Any]) -> str | None:
    attachment = content.get("attachment")
    if not isinstance(attachment, Mapping) or attachment.get("type") != "VIDEO":
        return None
    video = attachment.get("video")
    if not isinstance(video, Mapping):
        return None
    title = video.get("title")
    return title if isinstance(title, str) and title.strip() else None


def _author_name(content: Mapping[str, Any]) -> str | None:
    author = content.get("author")
    if isinstance(author, Mapping):
        name = author.get("name")
        if isinstance(name, str) and name.strip():
            return name
    return None


def _optional_str(content: Mapping[str, Any], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _optional_int(content: Mapping[str, Any], key: str) -> int | None:
    value = content.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _epoch_datetime(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 < value <= _MAX_EPOCH_SECONDS:
        return None
    return datetime.fromtimestamp(value, tz=UTC)
