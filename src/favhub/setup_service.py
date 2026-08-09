"""What `favhub setup` installs, and the limits on what it will touch.

Three rules shape this module:

1. **Never hand-edit another product's configuration.** Codex and Claude Code
   each own an MCP config format; editing those files directly risks destroying
   servers FavHub knows nothing about. Registration goes through their own CLIs.
2. **Only replace what FavHub manages.** A Skill directory named ``favhub-*``
   is ours; anything else in the same folder is left exactly as found.
3. **Never claim a manual step is done.** Chrome will not let a script load an
   unpacked extension, so setup reports that step instead of pretending.
"""

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from favhub.chrome_setup import Registry, register_native_host, unregister_native_host
from favhub.config import InstallPaths, save_install_config

SKILL_PREFIX = "favhub-"
MAX_COMMAND_DETAIL = 500


class SetupError(RuntimeError):
    """Setup refused to proceed; nothing was applied."""


@dataclass(frozen=True, slots=True)
class AgentHost:
    name: str
    skills_dir: Path
    cli: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str]], CommandResult]


def run_command(command: list[str]) -> CommandResult:
    """Run one Agent CLI, reporting failure rather than raising.

    The program name is resolved through ``shutil.which`` first: on Windows an
    npm-installed CLI is a ``.CMD`` shim, and handing the bare name to
    CreateProcess fails with "file not found" even though the command works from
    a shell. A missing CLI is likewise a reportable condition, not a crash —
    setup should still finish for the hosts that are present.
    """
    program = shutil.which(command[0]) or command[0]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [program, *command[1:]],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return CommandResult(127, "", f"could not run {command[0]}: {error}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _package_dir(name: str) -> Path:
    return Path(str(resources.files("favhub").joinpath(name)))


def _repository_dir(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / name


def deploy_extension(paths: InstallPaths) -> Path:
    """Replace the managed extension directory in one step.

    Copying into place file by file would leave Chrome looking at a half-updated
    extension if anything failed midway, so a staging directory is built first
    and swapped in.
    """
    source = _package_dir("browser_extension")
    if not (source / "manifest.json").is_file():
        raise SetupError(f"packaged extension is missing its manifest: {source}")
    paths.ensure()
    staging = paths.install_root / "extension.staging"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(paths.extension, ignore_errors=True)
    staging.replace(paths.extension)
    return paths.extension


def _skill_sources() -> list[Path]:
    packaged = _package_dir("skills")
    root = packaged if packaged.is_dir() else _repository_dir("skills")
    if not root.is_dir():
        raise SetupError(f"no FavHub skills found to install: {root}")
    return sorted(path for path in root.iterdir() if path.is_dir())


def deploy_skills(hosts: Sequence[AgentHost]) -> dict[str, list[str]]:
    """Install FavHub's Skills for each host, touching nothing else."""
    sources = _skill_sources()
    installed: dict[str, list[str]] = {}
    for host in hosts:
        host.skills_dir.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for source in sources:
            target = host.skills_dir / source.name
            # Replace rather than merge: a file dropped in a newer release must
            # not survive as a stale leftover.
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
            names.append(source.name)
        installed[host.name] = names
    return installed


def _looks_like_favhub(payload: str) -> bool:
    """Decide whether an existing MCP entry is one FavHub wrote."""
    if "favhub-mcp" in payload:
        return True
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False
    return "favhub" in json.dumps(parsed).lower() and "favhub-mcp" in json.dumps(parsed)


def _add_command(host: AgentHost, data_root: Path) -> list[str]:
    absolute = str(data_root)
    if host.cli == "claude":
        return [
            host.cli,
            "mcp",
            "add",
            "--scope",
            "user",
            "favhub",
            "--",
            "favhub-mcp",
            "--root",
            absolute,
        ]
    return [host.cli, "mcp", "add", "favhub", "--", "favhub-mcp", "--root", absolute]


def register_mcp(
    hosts: Sequence[AgentHost],
    data_root: Path,
    *,
    runner: CommandRunner,
    replace: bool,
) -> dict[str, dict[str, str]]:
    """Register the FavHub MCP server with each detected Agent host."""
    absolute = data_root.expanduser().resolve()
    report: dict[str, dict[str, str]] = {}
    for host in hosts:
        existing = runner([host.cli, "mcp", "get", "favhub"])
        already = existing.returncode == 0 and existing.stdout.strip() != ""
        if already and not _looks_like_favhub(existing.stdout) and not replace:
            # Someone else's server is named favhub; refuse rather than clobber.
            report[host.name] = {
                "status": "left_alone",
                "detail": (
                    "an existing 'favhub' MCP entry was not written by FavHub; "
                    "pass --replace to override"
                ),
            }
            continue
        result = runner(_add_command(host, absolute))
        if result.returncode != 0:
            report[host.name] = {
                "status": "failed",
                # Bounded on purpose: CLI output can echo unrelated config.
                "detail": (result.stderr or result.stdout)[:MAX_COMMAND_DETAIL],
            }
            continue
        if not already:
            status = "added"
        elif replace and not _looks_like_favhub(existing.stdout):
            status = "replaced"
        else:
            status = "updated"
        report[host.name] = {"status": status, "detail": ""}
    return report


def run_setup(
    paths: InstallPaths,
    *,
    data_root: Path,
    hosts: Sequence[AgentHost],
    registry: Registry,
    runner: CommandRunner,
    native_host_executable: Path,
    replace: bool = False,
) -> dict[str, Any]:
    """Install everything scriptable, and report what the user must still do."""
    resolved = data_root.expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise SetupError(f"data root is not a directory: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)

    extension = deploy_extension(paths)
    extension_id = (extension / "EXTENSION_ID").read_text(encoding="utf-8").strip()
    register_native_host(paths, extension_id, native_host_executable, registry=registry)
    skills = deploy_skills(hosts)
    mcp = register_mcp(hosts, resolved, runner=runner, replace=replace)
    save_install_config(paths, resolved)

    return {
        "data_root": str(resolved),
        "extension_dir": str(extension),
        "extension_id": extension_id,
        "native_host_manifest": str(paths.native_host_manifest),
        "skills": skills,
        "mcp": mcp,
        # Chrome does not allow a script to load an unpacked extension, and
        # saying otherwise would send the user hunting for a failure that is
        # really an unfinished step.
        "extension_loaded_in_chrome": False,
        "manual_steps": [
            "Open chrome://extensions and turn on Developer mode.",
            f'Click "Load unpacked" and choose {paths.extension}.',
            "After a FavHub upgrade, click Reload on the FavHub Collector card.",
        ],
    }


def uninstall(
    paths: InstallPaths,
    *,
    hosts: Sequence[AgentHost],
    registry: Registry,
    runner: CommandRunner,
) -> dict[str, Any]:
    """Remove what setup installed. The library and notes are never touched."""
    unregister_native_host(paths, registry=registry)
    shutil.rmtree(paths.extension, ignore_errors=True)

    removed: dict[str, list[str]] = {}
    for host in hosts:
        names: list[str] = []
        if host.skills_dir.is_dir():
            for entry in sorted(host.skills_dir.iterdir()):
                if entry.is_dir() and entry.name.startswith(SKILL_PREFIX):
                    shutil.rmtree(entry, ignore_errors=True)
                    names.append(entry.name)
        removed[host.name] = names
        runner([host.cli, "mcp", "remove", "favhub"])

    paths.config_file.unlink(missing_ok=True)
    return {
        "skills_removed": removed,
        "data_root_preserved": True,
        "manual_steps": [
            "Open chrome://extensions and remove the FavHub Collector extension.",
        ],
    }


__all__ = [
    "MAX_COMMAND_DETAIL",
    "SKILL_PREFIX",
    "AgentHost",
    "CommandResult",
    "CommandRunner",
    "SetupError",
    "deploy_extension",
    "deploy_skills",
    "register_mcp",
    "run_command",
    "run_setup",
    "uninstall",
]
