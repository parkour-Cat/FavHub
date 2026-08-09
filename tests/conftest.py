"""Suite-wide guarantees that hold no matter what a test forgets.

The one here exists because it was learned the hard way: the browser launcher
was reachable through `Application.open`, so a full suite run opened a real
Chrome tab for every test that started a collection run and buried the user's
browser. Fixing the default was necessary but not sufficient — anything that
reaches a real `spawn` again should fail loudly here rather than on a desktop.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from favhub import browser_launcher


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "spawns_browser: exercises the launcher itself; stubs subprocess instead.",
    )


@pytest.fixture(autouse=True)
def never_open_a_real_browser(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """No test may launch a browser, however indirectly it gets there."""
    if request.node.get_closest_marker("spawns_browser") is not None:
        # These test `_spawn` itself and stub `subprocess.Popen` one level down,
        # so no process is created there either.
        yield
        return

    def refuse(executable: Path, url: str) -> None:
        raise AssertionError(
            f"a test tried to open a real browser: {executable} {url}. "
            "Inject open_page/spawn instead of reaching the default."
        )

    # Two independent barriers, because one silently failed once already: even
    # if something reaches a spawn that is not this one, it finds no browser to
    # start. A guard against opening windows on a real desktop is worth the
    # redundancy — the failure is loud for the developer and invisible in CI.
    monkeypatch.setattr(browser_launcher, "_spawn", refuse)
    monkeypatch.setattr(browser_launcher, "find_chrome", lambda: None)
    yield
