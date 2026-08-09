"""Collect the user's public GitHub stars, in this process.

Every other platform needs an Agent and a browser because collection borrows a
logged-in session that only the browser holds. GitHub needs neither: the
starred list is a public REST endpoint. Having the Agent fetch and forward
those pages was an accident of symmetry, and it had a real cost — an optional
token could not be used without the token passing through the Agent's context.

So the requests happen here. The token is read from the environment at request
time, attached to exactly one host, and never returned, logged, or written
anywhere. Unauthenticated is 60 requests an hour counted per IP address, which
a shared address exhausts on someone else's behalf; a token makes it 5000 and
makes it the user's own.

Public stars only. A token widens what the starred endpoint returns to include
privately visible stars, and quietly collecting more because the authentication
changed would be a decision nobody made.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from favhub.capture import (
    LOGIN_REQUIRED,
    PAGE_CHANGED,
    RATE_LIMITED,
    SOURCE_UNAVAILABLE,
    CaptureError,
)
from favhub.domain import CapturedItem, SyncMode
from favhub.github_mapping import map_captured_item
from favhub.github_models import GithubStar
from favhub.github_parsers import parse_starred_page
from favhub.sync_module import StartSyncRequest, SyncModule

# Read from the environment rather than any FavHub config file: a value FavHub
# never stores is one it can never leak, and the user can scope, rotate, or
# revoke it without touching this project at all.
TOKEN_ENV = "FAVHUB_GITHUB_TOKEN"

API_HOST = "https://api.github.com"
RAW_HOST = "https://raw.githubusercontent.com"
STAR_ACCEPT = "application/vnd.github.star+json"
USER_AGENT = "favhub"
PAGE_SIZE = 100

# Tried in order; a repository with none of them is collected with its
# description alone rather than treated as a failure.
README_NAMES = ("README.md", "readme.md", "README.rst", "README")

# Below this the run stops rather than spending the user's last requests and
# leaving them with nothing for anything else.
MIN_REMAINING = 2


@dataclass(frozen=True, slots=True)
class GithubPage:
    stars: tuple[GithubStar, ...]
    has_next: bool
    remaining: int | None


@dataclass(frozen=True, slots=True)
class CollectResult:
    items: tuple[CapturedItem, ...]
    observed_end: bool
    max_scan_reached: bool
    scanned: int
    readmes_missing: int


def _headers(accept: str, token: str | None) -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _open(
    url: str,
    headers: Mapping[str, str],
    opener: Callable[[urllib.request.Request], Any],
) -> tuple[int, Mapping[str, str], bytes]:
    request = urllib.request.Request(url, headers=dict(headers))  # noqa: S310 - https only
    try:
        with opener(request) as response:
            return int(response.status), dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        # The body is what distinguishes a rate limit from a missing user, and
        # the parsers already read those shapes. The request headers are not
        # touched here, so the token cannot travel into an error message.
        return int(error.code), dict(error.headers or {}), error.read()


def _remaining(headers: Mapping[str, str]) -> int | None:
    value = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _has_next(headers: Mapping[str, str]) -> bool:
    link = headers.get("link") or headers.get("Link") or ""
    return 'rel="next"' in link


def fetch_starred_page(
    login: str,
    page: int,
    *,
    token: str | None,
    opener: Callable[[urllib.request.Request], Any],
) -> GithubPage:
    """One page of the public starred list, newest first."""
    url = f"{API_HOST}/users/{login}/starred?per_page={PAGE_SIZE}&page={page}"
    status, headers, body = _open(url, _headers(STAR_ACCEPT, token), opener)
    # Status before body. "This user does not exist" and "this endpoint changed
    # shape" are different problems with different answers, and deciding
    # between them by whether the error happened to carry parseable JSON would
    # report the first as the second whenever the body was empty.
    if status == 404:
        raise CaptureError(SOURCE_UNAVAILABLE, "user or resource not found")
    if status == 401:
        # Named, because the alternative is a rejected token surfacing as
        # "the page changed" and sending the user to read GitHub's API docs
        # about a problem that lives in their environment.
        raise CaptureError(LOGIN_REQUIRED, f"GitHub rejected the token in {TOKEN_ENV}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CaptureError(PAGE_CHANGED, "starred response was not JSON") from error
    # parse_starred_page turns an error envelope into the right typed failure,
    # including the rate-limit body that a 403 carries.
    return GithubPage(
        stars=parse_starred_page(payload),
        has_next=_has_next(headers),
        remaining=_remaining(headers),
    )


def fetch_readme(
    star: GithubStar,
    *,
    opener: Callable[[urllib.request.Request], Any],
) -> str | None:
    """The repository's README, or None when it has none FavHub can name.

    Deliberately no token, and deliberately a different host: the raw CDN
    serves public files without authentication, so sending the user's
    credential there would be pure oversharing. It also costs no API quota,
    which is why a full run is three requests rather than three hundred.
    """
    for name in README_NAMES:
        url = f"{RAW_HOST}/{star.full_name}/{star.default_branch}/{name}"
        status, _headers_out, body = _open(url, _headers("text/plain", None), opener)
        if status == 200:
            return body.decode("utf-8", errors="replace")
    return None


def collect_stars(
    login: str,
    *,
    frontier: tuple[str, ...] = (),
    max_scan_items: int | None = None,
    token: str | None = None,
    opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    pause_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectResult:
    """Walk the starred list newest-first until the frontier, cap, or the end."""
    known = set(frontier)
    items: list[CapturedItem] = []
    scanned = 0
    missing = 0
    observed_end = False
    max_scan_reached = False

    for page_number, page in enumerate(_pages(login, token=token, opener=opener), start=1):
        if page_number > 1 and pause_seconds:
            sleep(pause_seconds)
        if not page.stars:
            observed_end = True
            break
        for star in page.stars:
            source_id = star.full_name.replace("/", "__")
            if source_id in known:
                # Everything below a known star was collected by an earlier
                # run: the list is ordered by when it was starred.
                observed_end = True
                return _result(items, observed_end, max_scan_reached, scanned, missing)
            readme = fetch_readme(star, opener=opener)
            if readme is None:
                missing += 1
            items.append(map_captured_item(star, readme_text=readme, observed_at=now()))
            scanned += 1
            if max_scan_items is not None and scanned >= max_scan_items:
                max_scan_reached = True
                return _result(items, observed_end, max_scan_reached, scanned, missing)
        if page.remaining is not None and page.remaining < MIN_REMAINING and page.has_next:
            # Stopping short is reported as a truncated scan, so the frontier
            # does not advance past what was never read.
            max_scan_reached = True
            return _result(items, observed_end, max_scan_reached, scanned, missing)
        if not page.has_next:
            observed_end = True
            break

    return _result(items, observed_end, max_scan_reached, scanned, missing)


# The submit contract caps a batch at 20, so a long run lands as several
# durable receipts rather than one all-or-nothing write.
BATCH_SIZE = 20

# How many of the newest confirmed ids become the next run's stopping line.
FRONTIER_IDS = 20


def run_sync(
    sync: SyncModule,
    *,
    login: str,
    mode: str = "incremental",
    max_scan_items: int | None = None,
    token: str | None = None,
    opener: Callable[[urllib.request.Request], Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Collect stars into the library, start to finish, in this process.

    Shared by the CLI and the MCP tool rather than written twice. The MCP one
    exists because the running server holds the data root lock for its whole
    lifetime, so a CLI-only command could be used exactly when the user was not
    using FavHub.

    The token arrives as an argument and leaves in nothing: the returned
    mapping reports only whether one was used.
    """
    started = sync.start_sync(
        StartSyncRequest(
            platforms=("github",),
            mode=SyncMode(mode),
            published_since=None,
            published_until=None,
            max_scan_items=max_scan_items,
        )
    )
    job_id = started.job_id
    try:
        collected = collect_stars(
            login,
            frontier=started.frontiers["github"],
            max_scan_items=max_scan_items,
            token=token,
            opener=opener,
        )
    except CaptureError as error:
        sync.pause_sync(job_id, "github", error.code, error.message)
        return {
            "job_id": job_id,
            "authenticated": token is not None,
            "error": {"code": error.code, "message": error.message},
            "status": sync.get_status(job_id),
        }

    for index in range(0, len(collected.items), BATCH_SIZE):
        sync.submit_batch(
            job_id,
            "github",
            f"github-batch-{index // BATCH_SIZE + 1:04d}",
            list(collected.items[index : index + BATCH_SIZE]),
        )
    sync.finish_scan(
        job_id,
        "github",
        observed_end=collected.observed_end,
        max_scan_reached=collected.max_scan_reached,
        visible_total=None,
        # A truncated scan advances nothing, or the next run would skip
        # whatever this one never reached.
        frontier_ids=(
            ()
            if collected.max_scan_reached
            else tuple(item.source_id for item in collected.items[:FRONTIER_IDS])
        ),
    )
    return {
        "job_id": job_id,
        "authenticated": token is not None,
        "readmes_missing": collected.readmes_missing,
        "status": sync.get_status(job_id),
    }


def _result(
    items: list[CapturedItem],
    observed_end: bool,
    max_scan_reached: bool,
    scanned: int,
    missing: int,
) -> CollectResult:
    return CollectResult(
        items=tuple(items),
        observed_end=observed_end and not max_scan_reached,
        max_scan_reached=max_scan_reached,
        scanned=scanned,
        readmes_missing=missing,
    )


def _pages(
    login: str,
    *,
    token: str | None,
    opener: Callable[[urllib.request.Request], Any],
) -> Iterator[GithubPage]:
    page_number = 1
    while True:
        page = fetch_starred_page(login, page_number, token=token, opener=opener)
        yield page
        if not page.has_next:
            return
        page_number += 1


__all__ = [
    "BATCH_SIZE",
    "MIN_REMAINING",
    "PAGE_SIZE",
    "RATE_LIMITED",
    "TOKEN_ENV",
    "CollectResult",
    "GithubPage",
    "collect_stars",
    "fetch_readme",
    "fetch_starred_page",
    "run_sync",
]
