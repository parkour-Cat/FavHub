"""Chrome Native Messaging registration.

Chrome finds a native host through a per-user registry value pointing at a
manifest, and that manifest names the one extension allowed to launch it. Both
halves are written here.

Everything stays in ``HKCU``: registration must never need administrator rights
and must never affect other accounts on the machine.

The registry is reached through an injected adapter so tests exercise the real
ordering and rollback against a fake hive. Nothing in the test suite touches the
actual registry.
"""

import json
import os
import re
from pathlib import Path
from typing import Protocol

from favhub.config import NATIVE_HOST_NAME, InstallPaths

NATIVE_HOST_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{NATIVE_HOST_NAME}"

# Chrome extension ids are 32 characters drawn from 'a'-'p'.
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")


class ChromeSetupError(RuntimeError):
    """Registration could not be completed; nothing was left half-applied."""


class Registry(Protocol):
    def set_default(self, key: str, value: str) -> None: ...

    def delete_key(self, key: str) -> None: ...

    def read_default(self, key: str) -> str | None: ...


class FakeRegistry:
    """An in-memory hive for tests, and the default in non-Windows runs."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.written_keys: list[str] = []

    def set_default(self, key: str, value: str) -> None:
        self.values[key] = value
        self.written_keys.append(key)

    def delete_key(self, key: str) -> None:
        self.values.pop(key, None)

    def read_default(self, key: str) -> str | None:
        return self.values.get(key)


class WindowsRegistry:
    """The real HKCU hive. Only constructed by the CLI, never by tests."""

    def set_default(self, key: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as handle:
            winreg.SetValueEx(handle, "", 0, winreg.REG_SZ, value)

    def delete_key(self, key: str) -> None:
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            return

    def read_default(self, key: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _type = winreg.QueryValueEx(handle, "")
        except FileNotFoundError:
            return None
        return str(value)


def native_host_manifest(extension_id: str, executable: Path) -> dict[str, object]:
    """Build the host manifest for exactly one pinned extension."""
    if not _EXTENSION_ID.match(extension_id or ""):
        raise ChromeSetupError(f"not a Chrome extension id: {extension_id!r}")
    return {
        "name": NATIVE_HOST_NAME,
        "description": "FavHub browser capture relay",
        "path": str(executable),
        "type": "stdio",
        # One origin only. A wildcard here would let any extension the user ever
        # installs start the relay and talk to their library.
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def register_native_host(
    paths: InstallPaths,
    extension_id: str,
    executable: Path,
    *,
    registry: Registry,
) -> None:
    """Write the manifest and point Chrome at it, or leave nothing behind."""
    manifest = native_host_manifest(extension_id, executable)
    paths.ensure()
    target = paths.native_host_manifest
    previous = target.read_text(encoding="utf-8") if target.is_file() else None

    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)

    try:
        registry.set_default(NATIVE_HOST_KEY, str(target))
    except OSError as error:
        # A manifest Chrome cannot find is just a confusing orphan; put the
        # filesystem back the way it was.
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(previous, encoding="utf-8")
        raise ChromeSetupError(f"could not write the Chrome registry value: {error}") from error


def unregister_native_host(paths: InstallPaths, *, registry: Registry) -> None:
    """Remove only FavHub's own key and manifest."""
    registry.delete_key(NATIVE_HOST_KEY)
    paths.native_host_manifest.unlink(missing_ok=True)


def registered_manifest_path(*, registry: Registry) -> str | None:
    return registry.read_default(NATIVE_HOST_KEY)


__all__ = [
    "NATIVE_HOST_KEY",
    "ChromeSetupError",
    "FakeRegistry",
    "Registry",
    "WindowsRegistry",
    "native_host_manifest",
    "register_native_host",
    "registered_manifest_path",
    "unregister_native_host",
]
