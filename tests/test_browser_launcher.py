"""The launch is the whole zero-click story, so its edges are pinned here.

Nothing in this file starts a real browser: the locate/spawn pair is injected so
the decisions — which URL, when to give up — are testable without one.
"""

from pathlib import Path

import pytest

from favhub import browser_launcher
from favhub.browser_launcher import (
    FAVHUB_FRAGMENT,
    collection_url,
    open_collection_page,
)


def recorder() -> tuple[list[tuple[Path, str]], object]:
    seen: list[tuple[Path, str]] = []

    def spawn(executable: Path, url: str) -> None:
        seen.append((executable, url))

    return seen, spawn


def test_the_url_carries_the_marker_as_a_fragment() -> None:
    """A fragment is never sent to the server, so the platform cannot see it."""
    url = collection_url("x")
    assert url is not None
    assert url.endswith(f"#{FAVHUB_FRAGMENT}")
    assert "?" not in url, "a query string would be sent to the platform"


def test_every_browser_platform_has_a_page_that_can_be_opened() -> None:
    """A URL FavHub can name without knowing whose account it is.

    Bilibili's favourites live under the account's own space id, which is
    exactly what FavHub does not know — so its entry is the home page, and the
    adapter asks the platform who it is once the extension is awake.
    """
    for platform in ("x", "zhihu", "bilibili"):
        assert collection_url(platform) is not None, platform
    # GitHub is collected through its API and needs no browser at all.
    assert collection_url("github") is None


def test_opening_reports_the_url_it_launched() -> None:
    seen, spawn = recorder()
    chrome = Path("C:/chrome.exe")
    opened = open_collection_page("x", locate=lambda: chrome, spawn=spawn)
    assert opened == collection_url("x")
    assert seen == [(chrome, opened)]


def test_a_platform_without_a_page_launches_nothing() -> None:
    seen, spawn = recorder()
    assert open_collection_page("github", locate=lambda: Path("C:/chrome.exe"), spawn=spawn) is None
    assert seen == []


def test_bilibili_opens_a_page_that_does_not_name_an_account() -> None:
    """The launched URL must not contain anyone's id, since FavHub has none."""
    url = collection_url("bilibili")
    assert url == "https://www.bilibili.com/#favhub-opened"
    assert "space.bilibili.com" not in url


def test_no_browser_on_the_machine_is_reported_not_raised() -> None:
    """The session is already waiting; opening the page by hand still works."""
    seen, spawn = recorder()
    assert open_collection_page("x", locate=lambda: None, spawn=spawn) is None
    assert seen == []


def test_a_browser_that_will_not_start_never_takes_down_the_run() -> None:
    def explode(_executable: Path, _url: str) -> None:
        raise OSError("browser is broken")

    assert open_collection_page("x", locate=lambda: Path("C:/chrome.exe"), spawn=explode) is None


def test_a_gateway_opens_no_window_unless_it_was_explicitly_asked_to() -> None:
    """Opening a browser is a side effect on a real desktop, so it is opt-in.

    This defaulted to the real launcher once, and a full test-suite run buried
    the user's browser in tabs — every test that started a run through an
    Application opened one. The default is inert now, and `Application.open` is
    the single place that asks for the real thing.
    """
    import inspect

    from favhub.browser_gateway import BrowserGateway

    default = inspect.signature(BrowserGateway).parameters["open_page"].default
    assert default is not browser_launcher.open_collection_page
    assert default("x") is None


def test_the_application_is_the_one_place_that_opens_a_window() -> None:
    source = (Path(__file__).resolve().parents[1] / "src" / "favhub" / "application.py").read_text(
        encoding="utf-8"
    )
    assert "open_page=open_collection_page" in source


def test_patching_the_module_actually_stops_a_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Late binding is what makes the suite-wide guard real.

    With `spawn=_spawn` as a default argument this passed nothing to the patch
    and opened a window anyway, which is exactly how a full suite run kept
    filling the user's browser after the guard was supposedly in place.
    """
    attempts: list[str] = []

    def refuse(_executable: Path, url: str) -> None:
        attempts.append(url)
        raise AssertionError("must not launch")

    monkeypatch.setattr(browser_launcher, "find_chrome", lambda: Path("C:/chrome.exe"))
    monkeypatch.setattr(browser_launcher, "_spawn", refuse)
    with pytest.raises(AssertionError, match="must not launch"):
        open_collection_page("x")
    assert attempts == [collection_url("x")]


@pytest.mark.spawns_browser
def test_the_launcher_never_passes_anything_but_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """No profile flags, no automation switches, no user data directory.

    Anything extra here would either point Chrome at a different profile than
    the one holding the extension, or make the user's own browser look automated.
    """
    recorded: list[list[str]] = []

    class FakePopen:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            recorded.append(command)

    executable = Path("C:/chrome.exe")
    monkeypatch.setattr(browser_launcher.subprocess, "Popen", FakePopen)
    browser_launcher._spawn(executable, "https://x.com/i/bookmarks#favhub-opened")
    assert recorded == [[str(executable), "https://x.com/i/bookmarks#favhub-opened"]]
