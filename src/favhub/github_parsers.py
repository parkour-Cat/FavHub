"""Pure parsers for GitHub starred-API responses.

Payloads come from the public REST API (``Accept:
application/vnd.github.star+json``). No credential is ever involved. Error
envelopes become typed :class:`CaptureError` values, never empty success:
rate-limit bodies pause the platform, an unknown user is
``source_unavailable``, and any other non-array shape is ``page_changed``.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from favhub.capture import PAGE_CHANGED, RATE_LIMITED, SOURCE_UNAVAILABLE, CaptureError
from favhub.github_models import GithubStar


def parse_starred_page(payload: object) -> tuple[GithubStar, ...]:
    if isinstance(payload, Mapping):
        message = str(payload.get("message", "")).casefold()
        if "rate limit" in message:
            raise CaptureError(RATE_LIMITED, "API rate limit exceeded")
        if "not found" in message:
            raise CaptureError(SOURCE_UNAVAILABLE, "user or resource not found")
        raise CaptureError(PAGE_CHANGED, "unexpected error envelope")
    if not isinstance(payload, list):
        raise CaptureError(PAGE_CHANGED, "starred response is not an array")
    return tuple(_parse_star(entry) for entry in payload)


def _parse_star(entry: object) -> GithubStar:
    if not isinstance(entry, Mapping):
        raise CaptureError(PAGE_CHANGED, "starred entry is not an object")
    repo = entry.get("repo")
    if not isinstance(repo, Mapping):
        raise CaptureError(PAGE_CHANGED, "starred entry has no repo")
    full_name = _required_string(repo, "full_name")
    owner = repo.get("owner")
    owner_login = owner.get("login") if isinstance(owner, Mapping) else None
    topics_raw = repo.get("topics")
    topics = (
        tuple(topic for topic in topics_raw if isinstance(topic, str))
        if isinstance(topics_raw, list)
        else ()
    )
    stargazers = repo.get("stargazers_count")
    return GithubStar(
        starred_at=_required_datetime(entry, "starred_at"),
        full_name=full_name,
        html_url=_required_string(repo, "html_url"),
        owner=owner_login
        if isinstance(owner_login, str) and owner_login
        else full_name.split("/", 1)[0],
        default_branch=_required_string(repo, "default_branch"),
        created_at=_required_datetime(repo, "created_at"),
        description=_optional_string(repo, "description"),
        language=_optional_string(repo, "language"),
        topics=topics,
        pushed_at=_optional_datetime(repo, "pushed_at"),
        stargazers_count=stargazers
        if isinstance(stargazers, int) and not isinstance(stargazers, bool)
        else None,
        archived=bool(repo.get("archived")),
        fork=bool(repo.get("fork")),
    )


def _required_string(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CaptureError(PAGE_CHANGED, f"missing or invalid field: {key}")
    return value


def _optional_string(source: Mapping[str, Any], key: str) -> str | None:
    value = source.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _required_datetime(source: Mapping[str, Any], key: str) -> datetime:
    value = source.get(key)
    if not isinstance(value, str):
        raise CaptureError(PAGE_CHANGED, f"missing timestamp: {key}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureError(PAGE_CHANGED, f"invalid timestamp: {key}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CaptureError(PAGE_CHANGED, f"naive timestamp: {key}")
    return parsed


def _optional_datetime(source: Mapping[str, Any], key: str) -> datetime | None:
    value = source.get(key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
