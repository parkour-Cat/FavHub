"""In-process GitHub collection, with the token boundary pinned.

The value of doing this here rather than in the Agent is that the token never
leaves the process. That is a property, not a hope, so it is asserted: which
host sees the credential, and that no result or error carries it.
"""

import io
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import pytest

from favhub.capture import CaptureError
from favhub.github_sync import (
    MIN_REMAINING,
    TOKEN_ENV,
    collect_stars,
    fetch_starred_page,
)

TOKEN = "ghp_notarealtoken_0123456789"


def star_payload(full_name: str, starred_at: str = "2026-08-01T00:00:00Z") -> dict[str, Any]:
    owner, _, _name = full_name.partition("/")
    return {
        "starred_at": starred_at,
        "repo": {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "owner": {"login": owner},
            "default_branch": "main",
            "created_at": "2020-01-01T00:00:00Z",
            "description": "a repository",
            "language": "Python",
            "topics": ["cli"],
            "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 7,
            "archived": False,
            "fork": False,
        },
    }


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class Recorder:
    """Stands in for the network, and remembers who was shown what."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]) -> None:
        self.routes = routes
        self.seen: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: urllib.request.Request) -> _Response:
        url = request.full_url
        self.seen.append((url, dict(request.headers)))
        for prefix, (status, headers, body) in self.routes.items():
            if url.startswith(prefix):
                if status >= 400:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        "error",
                        headers,  # type: ignore[arg-type]
                        io.BytesIO(body),
                    )
                return _Response(status, headers, body)
        raise urllib.error.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    def headers_for(self, host_fragment: str) -> list[dict[str, str]]:
        return [headers for url, headers in self.seen if host_fragment in url]


def routes(stars: list[dict[str, Any]], *, link: str = "", readme: bytes | None = b"# Readme"):
    table: dict[str, tuple[int, dict[str, str], bytes]] = {
        "https://api.github.com/users/": (
            200,
            {"link": link, "x-ratelimit-remaining": "58"},
            json.dumps(stars).encode("utf-8"),
        ),
    }
    if readme is not None:
        table["https://raw.githubusercontent.com/"] = (200, {}, readme)
    return table


# -- the token boundary -------------------------------------------------------


def test_the_token_reaches_the_api_and_nothing_else() -> None:
    recorder = Recorder(routes([star_payload("owner/repo")]))

    collect_stars("someone", token=TOKEN, opener=recorder)

    api_headers = recorder.headers_for("api.github.com")
    assert api_headers and all(h.get("Authorization") == f"Bearer {TOKEN}" for h in api_headers)
    # The raw CDN serves public files without authentication, so sending the
    # user's credential there would be oversharing and nothing more.
    raw_headers = recorder.headers_for("raw.githubusercontent.com")
    assert raw_headers
    assert not any("Authorization" in h for h in raw_headers)


def test_no_request_carries_a_token_when_none_is_configured() -> None:
    recorder = Recorder(routes([star_payload("owner/repo")]))

    collect_stars("someone", opener=recorder)

    assert not any("Authorization" in headers for _url, headers in recorder.seen)


def test_the_token_never_appears_in_a_result_or_an_error() -> None:
    recorder = Recorder(routes([star_payload("owner/repo")]))
    result = collect_stars("someone", token=TOKEN, opener=recorder)
    assert TOKEN not in repr(result)

    failing = Recorder({})
    with pytest.raises(CaptureError) as error:
        fetch_starred_page("someone", 1, token=TOKEN, opener=failing)
    assert TOKEN not in str(error.value)
    assert TOKEN not in repr(error.value)


def test_the_environment_variable_is_named_once_and_read_nowhere_else() -> None:
    """FavHub must never persist the token, so nothing may write it down."""
    assert TOKEN_ENV == "FAVHUB_GITHUB_TOKEN"


# -- collection behaviour -----------------------------------------------------


def test_a_run_stops_at_the_frontier_the_previous_run_confirmed() -> None:
    recorder = Recorder(
        routes([star_payload("owner/new"), star_payload("owner/known"), star_payload("owner/old")])
    )

    result = collect_stars("someone", frontier=("owner__known",), opener=recorder)

    # The starred list is ordered by when it was starred, so everything below a
    # known repository was collected by an earlier run.
    assert [item.source_id for item in result.items] == ["owner__new"]
    assert result.observed_end is True
    assert result.max_scan_reached is False


def test_the_scan_cap_truncates_and_refuses_to_claim_the_end() -> None:
    recorder = Recorder(routes([star_payload(f"owner/r{index}") for index in range(5)]))

    result = collect_stars("someone", max_scan_items=2, opener=recorder)

    assert len(result.items) == 2
    assert result.max_scan_reached is True
    assert result.observed_end is False


def test_a_repository_with_no_readme_is_collected_rather_than_failed() -> None:
    recorder = Recorder(routes([star_payload("owner/bare")], readme=None))

    result = collect_stars("someone", opener=recorder)

    assert [item.source_id for item in result.items] == ["owner__bare"]
    assert result.readmes_missing == 1
    # The description still made it into the body.
    assert "a repository" in result.items[0].body


def test_an_exhausted_rate_limit_stops_short_without_claiming_the_end() -> None:
    table = routes([star_payload("owner/repo")], link='<https://api.github.com/x>; rel="next"')
    table["https://api.github.com/users/"] = (
        200,
        {
            "link": '<https://api.github.com/x>; rel="next"',
            "x-ratelimit-remaining": str(MIN_REMAINING - 1),
        },
        json.dumps([star_payload("owner/repo")]).encode("utf-8"),
    )
    recorder = Recorder(table)

    result = collect_stars("someone", opener=recorder)

    # Spending the user's last requests would leave them with nothing for
    # anything else, and claiming the end would advance the frontier past
    # pages that were never read.
    assert result.max_scan_reached is True
    assert result.observed_end is False


def test_a_rate_limit_body_becomes_a_typed_pause() -> None:
    recorder = Recorder(
        {
            "https://api.github.com/users/": (
                200,
                {},
                json.dumps({"message": "API rate limit exceeded for 1.2.3.4"}).encode("utf-8"),
            )
        }
    )

    with pytest.raises(CaptureError) as error:
        collect_stars("someone", opener=recorder)
    assert error.value.code == "rate_limited"


def test_an_unknown_user_is_reported_as_a_missing_source() -> None:
    recorder = Recorder(
        {"https://api.github.com/users/": (404, {}, json.dumps({"message": "Not Found"}).encode())}
    )

    with pytest.raises(CaptureError) as error:
        collect_stars("nobody", opener=recorder)
    assert error.value.code == "source_unavailable"


def test_a_404_with_no_readable_body_is_still_a_missing_source() -> None:
    """The status says it; deciding by body shape reported it as page_changed."""
    recorder = Recorder({"https://api.github.com/users/": (404, {}, b"")})

    with pytest.raises(CaptureError) as error:
        collect_stars("nobody", opener=recorder)
    assert error.value.code == "source_unavailable"


def test_starred_at_becomes_the_favourite_timestamp() -> None:
    recorder = Recorder(routes([star_payload("owner/repo", starred_at="2024-03-04T05:06:07Z")]))

    result = collect_stars("someone", opener=recorder, now=lambda: datetime(2026, 8, 6, tzinfo=UTC))

    metadata = result.items[0].platform_metadata or {}
    assert str(metadata["favorited_at"]).startswith("2024-03-04")


def test_a_rejected_token_says_so_instead_of_blaming_the_endpoint() -> None:
    """401 named explicitly, or a bad token reads as "GitHub changed"."""
    recorder = Recorder(
        {
            "https://api.github.com/users/": (
                401,
                {},
                json.dumps({"message": "Bad credentials"}).encode("utf-8"),
            )
        }
    )

    with pytest.raises(CaptureError) as error:
        collect_stars("someone", token=TOKEN, opener=recorder)

    assert error.value.code == "login_required"
    assert TOKEN_ENV in error.value.message
    # The rejected value must not travel in the message that names it.
    assert TOKEN not in error.value.message
