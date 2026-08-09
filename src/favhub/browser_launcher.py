"""Open the platform's saved-items page so a run needs nothing from the user.

The extension cannot be woken from here. Chrome starts a Native Messaging host,
never the reverse, and an MV3 service worker sleeps until the browser gives it
an event — so FavHub has no way to say "a run is waiting" to a browser that is
already open. What it *can* do is cause the one event that does wake the
extension: a page load on the collection route.

That is the whole trick. `favhub-mcp` launches the user's Chrome at the saved
items page, the content script runs as it always has, and the run starts with
no click anywhere. Nothing here talks to the extension, and nothing here needs
a new permission.

A browser that will not start is never fatal: the session still waits, and
opening the page by hand still works exactly as before.
"""

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

# Read by the content script to tell a tab FavHub opened from one the user did.
# A fragment, deliberately: fragments are not sent to the server, so this marker
# is invisible to the platform while still being readable in the page.
FAVHUB_FRAGMENT = "favhub-opened"

# The page whose load wakes the extension, per platform. Every one of these is
# fixed and user-independent, which is the whole requirement: FavHub can only
# open a URL it can name without knowing whose account it is.
COLLECTION_URLS = {
    "x": "https://x.com/i/bookmarks",
    # The account's own collections live under /mine; bare /collections is the
    # discovery page and holds other people's.
    "zhihu": "https://www.zhihu.com/collections/mine",
    # Not the favourites page: that lives under the account's own space id,
    # which FavHub cannot know in advance. The home page serves just as well
    # because Bilibili is collected actively — the adapter asks the platform
    # who it is and then fetches through the API, never reading the page.
    "bilibili": "https://www.bilibili.com/",
}

_APP_PATHS = r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"


def collection_url(platform: str) -> str | None:
    """The page whose load wakes the extension for this platform."""
    base = COLLECTION_URLS.get(platform)
    return None if base is None else f"{base}#{FAVHUB_FRAGMENT}"


def _registered_chrome() -> Path | None:
    if os.name != "nt":
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _APP_PATHS) as handle:
                value, _kind = winreg.QueryValueEx(handle, "")
        except OSError:
            continue
        candidate = Path(str(value))
        if candidate.is_file():
            return candidate
    return None


def find_chrome() -> Path | None:
    """Locate the Chrome the user actually runs, or report that there is none."""
    registered = _registered_chrome()
    if registered is not None:
        return registered

    if os.name == "nt":
        roots = [
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        ]
        for root in roots:
            if not root:
                continue
            candidate = Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"
            if candidate.is_file():
                return candidate

    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _spawn(executable: Path, url: str) -> None:
    # Detached and silent: this process outlives the request that started it,
    # and its output belongs to the user's browser, not to the MCP stream.
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(executable), url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
        close_fds=True,
    )


def open_collection_page(
    platform: str,
    *,
    locate: Callable[[], Path | None] | None = None,
    spawn: Callable[[Path, str], None] | None = None,
) -> str | None:
    """Open the platform's saved-items page; return the URL, or None if not.

    Returning None is an ordinary outcome, not an error: the run is already
    waiting, and the user opening the page themselves still starts it.

    `locate` and `spawn` resolve at call time rather than as default arguments.
    A default argument binds the function object once, at import, so replacing
    the module attribute afterwards has no effect — a test guard that patched
    `_spawn` looked like it worked while every call still opened a real window.
    """
    url = collection_url(platform)
    if url is None:
        return None
    executable = (locate or find_chrome)()
    if executable is None:
        return None
    try:
        (spawn or _spawn)(executable, url)
    except OSError:
        # A browser that will not launch must never take down a run that is
        # otherwise ready; the page can still be opened by hand.
        return None
    return url


__all__ = [
    "COLLECTION_URLS",
    "FAVHUB_FRAGMENT",
    "collection_url",
    "find_chrome",
    "open_collection_page",
]
