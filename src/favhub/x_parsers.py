"""Pure parsers for redacted X Bookmarks GraphQL responses.

Every function accepts an already-decoded structured payload and returns
typed values or raises :class:`CaptureError` with a stable code. Parsers
never import network, browser, or FavHub storage modules and never interpret
an abnormal response (auth errors, changed schema) as an empty success.

Live-probe contract notes (see ``scripts/probe_x_contract.md``):

- author identity lives under ``core.user_results.result.core``;
- long tweets carry their full text in ``note_tweet``; ``legacy.full_text``
  is truncated and only a fallback;
- ``ext_alt_text`` is rarely present and strictly optional;
- deleted/withheld bookmarks appear as ``TweetTombstone`` results.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from favhub.capture import LOGIN_REQUIRED, PAGE_CHANGED, CaptureError
from favhub.x_models import (
    XBookmarksPage,
    XMedia,
    XQuotedTweet,
    XTombstone,
    XTweet,
)

_CREATED_AT_FORMAT = "%a %b %d %H:%M:%S %z %Y"
# X error codes that mean the session is not authenticated.
_LOGIN_ERROR_CODES = frozenset({32, 215, 353})


def _require_mapping(payload: object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CaptureError(PAGE_CHANGED, "response is not a structured object")
    return payload


def _check_errors(payload: Mapping[str, Any]) -> None:
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        code = error.get("code")
        message = str(error.get("message", "")).casefold()
        if (isinstance(code, int) and code in _LOGIN_ERROR_CODES) or (
            "authenticate" in message or "logged out" in message
        ):
            raise CaptureError(LOGIN_REQUIRED, "session is not logged in")
    raise CaptureError(PAGE_CHANGED, "response carries an unexpected error envelope")


def parse_bookmarks_page(payload: object) -> XBookmarksPage:
    mapping = _require_mapping(payload)
    _check_errors(mapping)
    data = mapping.get("data")
    if not isinstance(data, Mapping):
        raise CaptureError(PAGE_CHANGED, "response data is missing or not an object")
    timeline = (data.get("bookmark_timeline_v2") or {}).get("timeline")
    if not isinstance(timeline, Mapping):
        raise CaptureError(PAGE_CHANGED, "bookmark timeline is missing")
    instructions = timeline.get("instructions")
    if not isinstance(instructions, list):
        raise CaptureError(PAGE_CHANGED, "timeline instructions are missing")
    tweets: list[XTweet | XTombstone] = []
    bottom_cursor: str | None = None
    for instruction in instructions:
        if not isinstance(instruction, Mapping):
            raise CaptureError(PAGE_CHANGED, "timeline instruction is not an object")
        if instruction.get("type") != "TimelineAddEntries":
            continue
        entries = instruction.get("entries")
        if not isinstance(entries, list):
            raise CaptureError(PAGE_CHANGED, "timeline entries are missing")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise CaptureError(PAGE_CHANGED, "timeline entry is not an object")
            content = entry.get("content")
            if not isinstance(content, Mapping):
                raise CaptureError(PAGE_CHANGED, "timeline entry content is missing")
            entry_type = content.get("entryType")
            if entry_type == "TimelineTimelineCursor":
                if content.get("cursorType") == "Bottom":
                    value = content.get("value")
                    if not isinstance(value, str) or not value:
                        raise CaptureError(PAGE_CHANGED, "bottom cursor has no value")
                    bottom_cursor = value
            elif entry_type == "TimelineTimelineItem":
                tweets.append(parse_timeline_entry(entry))
            else:
                raise CaptureError(PAGE_CHANGED, f"unknown entry type: {entry_type}")
    return XBookmarksPage(
        tweets=tuple(tweets),
        bottom_cursor=bottom_cursor,
        has_more=bool(tweets),
    )


def parse_timeline_entry(entry: object) -> XTweet | XTombstone:
    mapping = _require_mapping(entry)
    content = mapping.get("content")
    if not isinstance(content, Mapping):
        raise CaptureError(PAGE_CHANGED, "timeline entry content is missing")
    item_content = content.get("itemContent")
    if not isinstance(item_content, Mapping):
        raise CaptureError(PAGE_CHANGED, "timeline item content is missing")
    result = (item_content.get("tweet_results") or {}).get("result")
    if not isinstance(result, Mapping):
        # Full-scale finding (2026-07-26): vanished bookmarks can appear as
        # entries with an empty tweet_results and no tombstone. When the
        # entry id still carries the tweet id, treat it as item-level
        # unavailability rather than schema drift.
        entry_id = str(mapping.get("entryId", ""))
        tweet_id = entry_id.removeprefix("tweet-")
        if tweet_id.isdigit():
            return XTombstone(tweet_id=tweet_id, reason="empty tweet result")
        raise CaptureError(PAGE_CHANGED, "tweet result is missing")
    result, typename = _unwrap_visibility(result)
    if typename == "TweetTombstone":
        return _parse_tombstone(mapping, result)
    if typename != "Tweet":
        raise CaptureError(PAGE_CHANGED, f"unknown tweet typename: {typename}")
    return _parse_tweet(result)


def _unwrap_visibility(result: Mapping[str, Any]) -> tuple[Mapping[str, Any], object]:
    typename = result.get("__typename")
    if typename != "TweetWithVisibilityResults":
        return result, typename
    inner = result.get("tweet")
    if not isinstance(inner, Mapping):
        raise CaptureError(PAGE_CHANGED, "visibility wrapper has no tweet")
    return inner, "Tweet"


def _tombstone_text(result: Mapping[str, Any]) -> str:
    text = ((result.get("tombstone") or {}).get("text") or {}).get("text")
    return text if isinstance(text, str) and text.strip() else "unavailable"


def _parse_tombstone(entry: Mapping[str, Any], result: Mapping[str, Any]) -> XTombstone:
    entry_id = str(entry.get("entryId", ""))
    tweet_id = entry_id.removeprefix("tweet-")
    if not tweet_id or not tweet_id.isdigit():
        raise CaptureError(PAGE_CHANGED, "tombstone entry has no tweet id")
    return XTombstone(tweet_id=tweet_id, reason=_tombstone_text(result))


def _parse_tweet(result: Mapping[str, Any]) -> XTweet:
    tweet_id = result.get("rest_id")
    if not isinstance(tweet_id, str) or not tweet_id.isdigit():
        raise CaptureError(PAGE_CHANGED, "tweet result has no rest_id")
    legacy = result.get("legacy")
    if not isinstance(legacy, Mapping):
        raise CaptureError(PAGE_CHANGED, "tweet legacy block is missing")
    author, handle = _author_fields(result)
    quoted = _parse_quoted(result, legacy)
    return XTweet(
        tweet_id=tweet_id,
        text=_tweet_text(result, legacy),
        author=author,
        handle=handle,
        created_at=_created_at(legacy),
        media=_parse_media(legacy),
        quoted=quoted,
    )


def _tweet_text(result: Mapping[str, Any], legacy: Mapping[str, Any]) -> str:
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    note_text = note.get("text") if isinstance(note, Mapping) else None
    if isinstance(note_text, str) and note_text.strip():
        return note_text
    full_text = legacy.get("full_text")
    if not isinstance(full_text, str):
        raise CaptureError(PAGE_CHANGED, "tweet full_text is missing")
    return full_text


def _author_fields(result: Mapping[str, Any]) -> tuple[str | None, str | None]:
    user = (((result.get("core") or {}).get("user_results") or {}).get("result")) or {}
    if not isinstance(user, Mapping):
        return None, None
    core = user.get("core")
    name: object = None
    handle: object = None
    if isinstance(core, Mapping):
        name = core.get("name")
        handle = core.get("screen_name")
    if not isinstance(name, str) or not name.strip():
        # Older layout fallback: identity under the user legacy block.
        user_legacy = user.get("legacy")
        if isinstance(user_legacy, Mapping):
            name = user_legacy.get("name")
            handle = handle or user_legacy.get("screen_name")
    clean_name = name if isinstance(name, str) and name.strip() else None
    clean_handle = handle if isinstance(handle, str) and handle.strip() else None
    return clean_name, clean_handle


def _created_at(legacy: Mapping[str, Any]) -> datetime:
    value = legacy.get("created_at")
    if not isinstance(value, str):
        raise CaptureError(PAGE_CHANGED, "tweet created_at is missing")
    try:
        return datetime.strptime(value, _CREATED_AT_FORMAT)
    except ValueError as exc:
        raise CaptureError(PAGE_CHANGED, "tweet created_at has an unknown format") from exc


def _parse_media(legacy: Mapping[str, Any]) -> tuple[XMedia, ...]:
    raw = (legacy.get("extended_entities") or {}).get("media")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CaptureError(PAGE_CHANGED, "tweet media is not a list")
    media: list[XMedia] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise CaptureError(PAGE_CHANGED, "tweet media entry is not an object")
        media_type = entry.get("type")
        url = entry.get("media_url_https")
        if not isinstance(media_type, str) or not media_type:
            raise CaptureError(PAGE_CHANGED, "tweet media entry has no type")
        if not isinstance(url, str) or not url:
            raise CaptureError(PAGE_CHANGED, "tweet media entry has no URL")
        alt = entry.get("ext_alt_text")
        media.append(
            XMedia(
                media_type=media_type,
                url=url,
                alt=alt if isinstance(alt, str) and alt.strip() else None,
            )
        )
    return tuple(media)


def _parse_quoted(result: Mapping[str, Any], legacy: Mapping[str, Any]) -> XQuotedTweet | None:
    quoted_wrapper = result.get("quoted_status_result")
    if not isinstance(quoted_wrapper, Mapping):
        return None
    quoted = quoted_wrapper.get("result")
    if not isinstance(quoted, Mapping):
        return None
    quoted, typename = _unwrap_visibility(quoted)
    if typename == "TweetTombstone":
        quoted_id = legacy.get("quoted_status_id_str")
        return XQuotedTweet(
            tweet_id=quoted_id if isinstance(quoted_id, str) else None,
            author=None,
            handle=None,
            text=_tombstone_text(quoted),
            unavailable=True,
        )
    if typename != "Tweet":
        raise CaptureError(PAGE_CHANGED, f"unknown quoted typename: {typename}")
    quoted_id = quoted.get("rest_id")
    quoted_legacy = quoted.get("legacy")
    if not isinstance(quoted_legacy, Mapping):
        raise CaptureError(PAGE_CHANGED, "quoted tweet legacy block is missing")
    author, handle = _author_fields(quoted)
    return XQuotedTweet(
        tweet_id=quoted_id if isinstance(quoted_id, str) else None,
        author=author,
        handle=handle,
        text=_tweet_text(quoted, quoted_legacy),
        unavailable=False,
    )
