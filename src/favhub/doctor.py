"""Installation diagnosis.

The browser path spans four independently-updated pieces — the Python package,
the extension Chrome loaded, the Native Messaging registration, and each Agent's
MCP config — so a break usually shows up as "nothing happens" rather than an
error. This module names the component instead.

It is strictly read-only. Being able to run it without changing anything is the
point: the user should be able to ask what is wrong before deciding what to fix.
"""

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from favhub.chrome_setup import NATIVE_HOST_KEY, Registry, registered_manifest_path
from favhub.config import FavHubPaths, InstallPaths, persisted_data_root
from favhub.setup_service import SKILL_PREFIX, AgentHost, CommandRunner


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    FAILED = "failed"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str


REQUIRED_EXTENSION_FILES = (
    "manifest.json",
    "background.js",
    "native-client.js",
    "content-isolated.js",
    "bridge.js",
    "main-hook.js",
    "session-controller.js",
    "popup.html",
    "popup.js",
    "popup.css",
    "EXTENSION_ID",
)


def running_favhub_pid(root: Path) -> int | None:
    """The pid of the FavHub holding this data root, when one is.

    The descriptor is written for the browser pipe, but it is also the only
    record of who owns the root, and the answer is what a caller turned away by
    the lock actually needs. Absent, unreadable or naming a dead process all
    mean the same thing: nobody is holding it.
    """
    descriptor = FavHubPaths.from_root(root).browser_pipe_descriptor
    try:
        written = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    pid = written.get("pid")
    if isinstance(pid, int) and _pid_alive(pid):
        return pid
    return None


def _pid_alive(pid: int) -> bool:
    """Whether a pid is worth believing, erring towards "no".

    Windows recycles pids, so a stale descriptor's pid can land on a process
    this user has no right to open. `OpenProcess` fails there, and CPython
    reports that failure as SystemError rather than OSError — catching only
    OSError let it escape and took the whole report down with it. Every way of
    not knowing means the same thing here: nothing is holding the pipe.
    """
    try:
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError, SystemError):
        return False
    return True


def _extension_checks(paths: InstallPaths) -> list[Check]:
    missing = [name for name in REQUIRED_EXTENSION_FILES if not (paths.extension / name).is_file()]
    if missing:
        return [
            Check(
                "extension_files",
                CheckStatus.FAILED,
                f"missing from {paths.extension}: {', '.join(missing)}",
            ),
            Check("extension_identity", CheckStatus.FAILED, "extension files are incomplete"),
        ]
    checks = [Check("extension_files", CheckStatus.OK, str(paths.extension))]

    extension_id = (paths.extension / "EXTENSION_ID").read_text(encoding="utf-8").strip()
    try:
        manifest = json.loads(paths.native_host_manifest.read_text(encoding="utf-8"))
        origins = manifest.get("allowed_origins", [])
    except (OSError, json.JSONDecodeError, ValueError):
        origins = []
    expected = f"chrome-extension://{extension_id}/"
    if expected in origins:
        checks.append(Check("extension_identity", CheckStatus.OK, extension_id))
    else:
        # This is the failure that looks like nothing happening: Chrome starts
        # the relay, the relay is refused, and the popup just says inactive.
        checks.append(
            Check(
                "extension_identity",
                CheckStatus.FAILED,
                f"the native host does not allow {extension_id}; it allows {origins or 'nothing'}",
            )
        )
    return checks


def _native_host_checks(paths: InstallPaths, registry: Registry) -> list[Check]:
    checks: list[Check] = []
    try:
        manifest = json.loads(paths.native_host_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return [
            Check("native_host_manifest", CheckStatus.FAILED, f"unreadable: {error}"),
            Check("chrome_registry", CheckStatus.FAILED, "no manifest to point at"),
            Check("native_executable", CheckStatus.FAILED, "no manifest to read"),
        ]
    checks.append(Check("native_host_manifest", CheckStatus.OK, str(paths.native_host_manifest)))

    registered = registered_manifest_path(registry=registry)
    if registered is None:
        checks.append(Check("chrome_registry", CheckStatus.FAILED, f"{NATIVE_HOST_KEY} is not set"))
    elif Path(registered) != paths.native_host_manifest:
        checks.append(Check("chrome_registry", CheckStatus.FAILED, f"points at {registered}"))
    else:
        checks.append(Check("chrome_registry", CheckStatus.OK, registered))

    executable = Path(str(manifest.get("path", "")))
    if executable.is_file():
        checks.append(Check("native_executable", CheckStatus.OK, str(executable)))
    else:
        checks.append(Check("native_executable", CheckStatus.FAILED, f"not found: {executable}"))
    return checks


def _data_root_checks(paths: InstallPaths) -> list[Check]:
    root = persisted_data_root(paths)
    if root is None:
        return [
            Check(
                "data_root", CheckStatus.FAILED, "no data root has been selected; run favhub setup"
            ),
            Check("runtime_descriptor", CheckStatus.INFO, "no data root to inspect"),
        ]
    if not root.is_dir():
        return [
            Check("data_root", CheckStatus.FAILED, f"does not exist: {root}"),
            Check("runtime_descriptor", CheckStatus.INFO, "no data root to inspect"),
        ]
    checks = [Check("data_root", CheckStatus.OK, str(root))]

    descriptor = FavHubPaths.from_root(root).browser_pipe_descriptor
    try:
        written = json.loads(descriptor.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        # Not running is the normal state: FavHub serves the pipe only while an
        # Agent session holds the MCP process open.
        checks.append(
            Check("runtime_descriptor", CheckStatus.INFO, "FavHub is not currently running")
        )
        return checks

    pid = written.get("pid")
    if isinstance(pid, int) and _pid_alive(pid):
        checks.append(Check("runtime_descriptor", CheckStatus.OK, f"FavHub is running (pid {pid})"))
    else:
        checks.append(
            Check(
                "runtime_descriptor",
                CheckStatus.WARNING,
                f"stale descriptor from pid {pid}; FavHub is not running",
            )
        )
    return checks


def _host_checks(hosts: Sequence[AgentHost], runner: CommandRunner) -> list[Check]:
    checks: list[Check] = []
    for host in hosts:
        installed = (
            sorted(
                entry.name
                for entry in host.skills_dir.iterdir()
                if entry.is_dir() and entry.name.startswith(SKILL_PREFIX)
            )
            if host.skills_dir.is_dir()
            else []
        )
        checks.append(
            Check(
                f"{host.name}_skills",
                CheckStatus.OK if installed else CheckStatus.WARNING,
                ", ".join(installed) if installed else f"no FavHub skills in {host.skills_dir}",
            )
        )
        result = runner([host.cli, "mcp", "get", "favhub"])
        registered = result.returncode == 0 and "favhub" in result.stdout
        checks.append(
            Check(
                f"{host.name}_mcp",
                CheckStatus.OK if registered else CheckStatus.WARNING,
                "registered" if registered else "favhub is not registered as an MCP server",
            )
        )
    return checks


def run_doctor(
    paths: InstallPaths,
    *,
    registry: Registry,
    runner: CommandRunner,
    hosts: Sequence[AgentHost],
) -> dict[str, Any]:
    """Inspect every component and report one result each."""
    from favhub import __name__ as package_name

    checks: list[Check] = [
        Check("python_package", CheckStatus.OK, f"{package_name} is importable"),
        *_data_root_checks(paths),
        *_extension_checks(paths),
        *_native_host_checks(paths, registry),
        *_host_checks(hosts, runner),
    ]
    return {
        # Warnings are things the user may not want; only failures mean broken.
        "ok": all(check.status is not CheckStatus.FAILED for check in checks),
        "install_root": str(paths.install_root),
        "checks": [asdict(check) for check in checks],
    }


__all__ = ["REQUIRED_EXTENSION_FILES", "Check", "CheckStatus", "run_doctor"]
