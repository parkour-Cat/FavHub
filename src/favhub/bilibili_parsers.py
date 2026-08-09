"""Pure parsers for redacted Bilibili structured responses.

Every function accepts an already-decoded structured payload (a mapping) and
returns typed values or raises :class:`BilibiliCaptureError` with a stable
code. Parsers never import ``requests``, Playwright, Selenium, or FavHub
storage modules, never touch the network, and never interpret an abnormal
response (login HTML, changed schema, error code) as an empty success.
"""

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from favhub.bilibili_models import (
    LOGIN_REQUIRED,
    MALFORMED_SUBTITLE,
    PAGE_CHANGED,
    SOURCE_UNAVAILABLE,
    SUBTITLE_UNAVAILABLE,
    BilibiliCaptureError,
    BilibiliFolder,
    BilibiliListEntry,
    BilibiliResourcePage,
    BilibiliSubtitle,
    BilibiliSubtitleCue,
    BilibiliVideo,
)

# Bilibili API `code` values that mean the session is logged out.
_LOGIN_CODES = frozenset({-101, -400})
# `code` values that mean the specific resource is gone/hidden, not a schema drift.
_UNAVAILABLE_CODES = frozenset({-403, -404, 62002, 62004})


def _require_mapping(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise BilibiliCaptureError(PAGE_CHANGED, "response is not a structured object")
    return payload


def _check_api_code(payload: Mapping[str, Any], *, unavailable_is_source: bool) -> None:
    """Reject non-zero Bilibili API envelopes with a typed code."""
    if "code" not in payload:
        return
    code = payload.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise BilibiliCaptureError(PAGE_CHANGED, "response code is not an integer")
    if code == 0:
        return
    if code in _LOGIN_CODES:
        raise BilibiliCaptureError(LOGIN_REQUIRED, "session is not logged in")
    if unavailable_is_source and code in _UNAVAILABLE_CODES:
        raise BilibiliCaptureError(SOURCE_UNAVAILABLE, "resource is unavailable")
    raise BilibiliCaptureError(PAGE_CHANGED, f"unexpected response code: {code}")


def _require_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise BilibiliCaptureError(PAGE_CHANGED, "response data is missing or not an object")
    return data


def _require_str(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BilibiliCaptureError(PAGE_CHANGED, f"missing or invalid field: {key}")
    return value


def _optional_str(source: Mapping[str, Any], key: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BilibiliCaptureError(PAGE_CHANGED, f"field is not a string: {key}")
    return value or None


def _require_int(source: Mapping[str, Any], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise BilibiliCaptureError(PAGE_CHANGED, f"missing or invalid integer: {key}")
    return value


def _optional_int(source: Mapping[str, Any], key: str) -> int | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise BilibiliCaptureError(PAGE_CHANGED, f"field is not an integer: {key}")
    return value


def _optional_epoch_to_datetime(value: object) -> datetime | None:
    """Lenient epoch parse for optional fields: anything invalid becomes None."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_to_datetime(source: Mapping[str, Any], key: str) -> datetime:
    value = source.get(key)
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise BilibiliCaptureError(PAGE_CHANGED, f"missing or non-finite timestamp: {key}")
    if value < 0:
        raise BilibiliCaptureError(PAGE_CHANGED, f"negative timestamp: {key}")
    return datetime.fromtimestamp(value, tz=UTC)


def parse_folders(payload: object) -> tuple[BilibiliFolder, ...]:
    mapping = _require_mapping(payload)
    _check_api_code(mapping, unavailable_is_source=False)
    data = _require_data(mapping)
    raw_list = data.get("list")
    if not isinstance(raw_list, list):
        raise BilibiliCaptureError(PAGE_CHANGED, "folder list is missing or not a list")
    folders: list[BilibiliFolder] = []
    for entry in raw_list:
        if not isinstance(entry, Mapping):
            raise BilibiliCaptureError(PAGE_CHANGED, "folder entry is not an object")
        scope = entry.get("id")
        if not isinstance(scope, int) or isinstance(scope, bool):
            raise BilibiliCaptureError(PAGE_CHANGED, "folder id is missing or invalid")
        folders.append(
            BilibiliFolder(
                scope_id=str(scope),
                title=_require_str(entry, "title"),
                media_count=_require_int(entry, "media_count"),
            )
        )
    return tuple(folders)


def parse_resource_page(payload: object) -> BilibiliResourcePage:
    mapping = _require_mapping(payload)
    _check_api_code(mapping, unavailable_is_source=False)
    data = _require_data(mapping)
    raw_medias = data.get("medias")
    if not isinstance(raw_medias, list):
        raise BilibiliCaptureError(PAGE_CHANGED, "medias is missing or not a list")
    has_more = data.get("has_more", False)
    if not isinstance(has_more, bool):
        raise BilibiliCaptureError(PAGE_CHANGED, "has_more is not a boolean")
    entries: list[BilibiliListEntry] = []
    for media in raw_medias:
        if not isinstance(media, Mapping):
            raise BilibiliCaptureError(PAGE_CHANGED, "media entry is not an object")
        bvid = media.get("bvid") or media.get("bv_id")
        if not isinstance(bvid, str) or not bvid.strip():
            raise BilibiliCaptureError(PAGE_CHANGED, "media entry is missing bvid")
        upper = media.get("upper")
        author = None
        author_mid = None
        if isinstance(upper, Mapping):
            author = _optional_str(upper, "name")
            upper_mid = upper.get("mid")
            author_mid = str(upper_mid) if isinstance(upper_mid, int) else None
        intro = media.get("intro")
        fav_time = _optional_epoch_to_datetime(media.get("fav_time"))
        entries.append(
            BilibiliListEntry(
                bvid=bvid,
                title=_require_str(media, "title"),
                author=author,
                author_mid=author_mid,
                intro=intro if isinstance(intro, str) else "",
                cover_url=_optional_str(media, "cover"),
                published_at=_epoch_to_datetime(media, "pubtime"),
                fav_time=fav_time,
            )
        )
    return BilibiliResourcePage(entries=tuple(entries), has_more=has_more)


def parse_video_detail(payload: object) -> BilibiliVideo:
    mapping = _require_mapping(payload)
    _check_api_code(mapping, unavailable_is_source=True)
    data = _require_data(mapping)
    owner = data.get("owner")
    author = None
    author_mid = None
    if isinstance(owner, Mapping):
        author = _optional_str(owner, "name")
        owner_mid = owner.get("mid")
        author_mid = str(owner_mid) if isinstance(owner_mid, int) else None
    description = data.get("desc")
    return BilibiliVideo(
        bvid=_require_str(data, "bvid"),
        aid=_optional_int(data, "aid"),
        title=_require_str(data, "title"),
        author=author,
        author_mid=author_mid,
        description=description if isinstance(description, str) else "",
        cover_url=_optional_str(data, "pic"),
        published_at=_epoch_to_datetime(data, "pubdate"),
        duration=_optional_int(data, "duration"),
        cid=_optional_int(data, "cid"),
    )


def parse_subtitle(payload: object, *, language: str | None = None) -> BilibiliSubtitle:
    mapping = _require_mapping(payload)
    # A subtitle document that is really an error envelope: a logged-out
    # session is a platform-level condition, anything else is malformed.
    code = mapping.get("code")
    if "code" in mapping and code not in (0, None):
        if isinstance(code, int) and not isinstance(code, bool) and code in _LOGIN_CODES:
            raise BilibiliCaptureError(LOGIN_REQUIRED, "session is not logged in")
        raise BilibiliCaptureError(MALFORMED_SUBTITLE, "subtitle response is an error envelope")
    body = mapping.get("body")
    if not isinstance(body, list):
        raise BilibiliCaptureError(MALFORMED_SUBTITLE, "subtitle body is missing or not a list")
    resolved_language = language
    if resolved_language is None:
        # Live probe finding: player track metadata uses "lan" but the
        # subtitle document itself carries "lang".
        for language_key in ("lan", "lang"):
            value = mapping.get(language_key)
            if isinstance(value, str) and value.strip():
                resolved_language = value
                break
        else:
            resolved_language = "unknown"
    seen: set[tuple[float, float, str]] = set()
    cues: list[BilibiliSubtitleCue] = []
    for raw in body:
        if not isinstance(raw, Mapping):
            raise BilibiliCaptureError(MALFORMED_SUBTITLE, "subtitle cue is not an object")
        start = _finite_number(raw.get("from"))
        end = _finite_number(raw.get("to"))
        content = raw.get("content")
        if not isinstance(content, str):
            raise BilibiliCaptureError(MALFORMED_SUBTITLE, "subtitle cue content is not a string")
        key = (start, end, content)
        if key in seen:
            continue
        seen.add(key)
        cues.append(BilibiliSubtitleCue(start=start, end=end, content=content))
    if not cues:
        raise BilibiliCaptureError(SUBTITLE_UNAVAILABLE, "subtitle has no cues")
    cues.sort(key=lambda cue: (cue.start, cue.end))
    return BilibiliSubtitle(language=resolved_language, cues=tuple(cues))


def _finite_number(value: object) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise BilibiliCaptureError(MALFORMED_SUBTITLE, "subtitle timestamp is not finite")
    return float(value)
