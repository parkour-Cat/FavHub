import json
import os
from dataclasses import dataclass
from pathlib import Path

INSTALL_SCHEMA_VERSION = 1
NATIVE_HOST_NAME = "com.favhub.browser"


@dataclass(frozen=True, slots=True)
class FavHubPaths:
    root: Path
    items: Path
    models: Path
    state: Path
    database: Path
    browser_pipe_descriptor: Path

    @classmethod
    def from_root(cls, root: Path) -> "FavHubPaths":
        resolved = root.expanduser().resolve()
        state = resolved / "state"
        return cls(
            root=resolved,
            items=resolved / "items",
            models=resolved / "models",
            state=state,
            database=state / "favhub.sqlite3",
            # Written only while an MCP process is serving the browser pipe;
            # it carries a per-run auth key, so it is runtime state, not config.
            browser_pipe_descriptor=state / "browser-pipe.json",
        )

    def ensure(self) -> None:
        self.items.mkdir(parents=True, exist_ok=True)
        self.models.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class InstallPaths:
    """User-level locations `favhub setup` manages.

    These are fixed rather than versioned so Chrome can be pointed at one
    directory once: an unpacked extension is loaded by path, and a path that
    moved with every release would need re-adding by hand each upgrade.
    """

    install_root: Path
    config_file: Path
    extension: Path
    native_host_dir: Path
    native_host_manifest: Path
    default_data_root: Path

    @classmethod
    def from_local_app_data(cls, local_app_data: Path | None = None) -> "InstallPaths":
        base = local_app_data or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        root = Path(base).expanduser().resolve() / "FavHub"
        native = root / "native-host"
        return cls(
            install_root=root,
            config_file=root / "install.json",
            extension=root / "extension",
            native_host_dir=native,
            native_host_manifest=native / f"{NATIVE_HOST_NAME}.json",
            default_data_root=root / "data",
        )

    def ensure(self) -> None:
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.native_host_dir.mkdir(parents=True, exist_ok=True)


def load_install_config(paths: InstallPaths) -> dict[str, object]:
    """Read the persisted install config, or an empty mapping when absent."""
    try:
        parsed = json.loads(paths.config_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def persisted_data_root(paths: InstallPaths) -> Path | None:
    """The data root a previous `favhub setup` selected, if any."""
    value = load_install_config(paths).get("dataRoot")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value)


def _manifest_version(manifest_file: Path) -> str | None:
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def packaged_extension_version() -> str | None:
    """The extension version this process ships, or None when unreadable.

    Deliberately the packaged copy rather than whatever sits in the user's
    install directory. Two reasons. It is the version this process would
    deploy, so it is the one its own protocol expectations were written
    against — the compatibility that actually matters. And reading the install
    directory made behaviour depend on the developer's machine: three
    integration tests went red the moment a real FavHub install was upgraded
    past the version their fixtures claimed, while CI stayed green.

    None is a real answer, not a failure: without something to compare
    against, refusing to collect would help nobody.
    """
    packaged = Path(__file__).resolve().parent / "browser_extension" / "manifest.json"
    return _manifest_version(packaged)


def installed_extension_version(paths: InstallPaths) -> str | None:
    """The version `favhub setup` last deployed, for reporting and diagnosis."""
    return _manifest_version(paths.extension / "manifest.json")


def save_install_config(paths: InstallPaths, data_root: Path) -> None:
    """Persist the selected data root, replacing the file atomically."""
    paths.ensure()
    payload = {
        "schemaVersion": INSTALL_SCHEMA_VERSION,
        "dataRoot": str(Path(data_root).expanduser().resolve()),
    }
    temporary = paths.config_file.with_name(f"{paths.config_file.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, paths.config_file)


__all__ = [
    "INSTALL_SCHEMA_VERSION",
    "NATIVE_HOST_NAME",
    "FavHubPaths",
    "InstallPaths",
    "installed_extension_version",
    "load_install_config",
    "packaged_extension_version",
    "persisted_data_root",
    "save_install_config",
]
