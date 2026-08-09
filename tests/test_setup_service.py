import json
from pathlib import Path

import pytest

from favhub.chrome_setup import FakeRegistry
from favhub.config import InstallPaths, installed_extension_version, persisted_data_root
from favhub.setup_service import (
    AgentHost,
    CommandResult,
    SetupError,
    deploy_extension,
    deploy_skills,
    register_mcp,
    run_setup,
    uninstall,
)

EXTENSION_ID = "abjlifflomnolgbngicokdhphnnggmim"


@pytest.fixture
def paths(tmp_path: Path) -> InstallPaths:
    return InstallPaths.from_local_app_data(tmp_path / "local")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    root.mkdir()
    return root


def fake_runner(script: dict[str, CommandResult] | None = None):
    """Record every command instead of running it."""
    calls: list[list[str]] = []
    responses = script or {}

    def run(command: list[str]) -> CommandResult:
        calls.append(list(command))
        key = " ".join(command[:3])
        return responses.get(key, CommandResult(0, "", ""))

    run.calls = calls  # type: ignore[attr-defined]
    return run


# -- extension deployment ------------------------------------------------------


def test_the_extension_is_deployed_to_the_fixed_directory(paths: InstallPaths) -> None:
    deploy_extension(paths)
    assert (paths.extension / "manifest.json").is_file()
    assert (paths.extension / "background.js").is_file()
    assert (paths.extension / "adapters" / "x.js").is_file()
    manifest = json.loads((paths.extension / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3


def test_redeploying_replaces_stale_files_and_leaves_no_temporary(
    paths: InstallPaths,
) -> None:
    deploy_extension(paths)
    stale = paths.extension / "removed-in-this-release.js"
    stale.write_text("// old", encoding="utf-8")
    deploy_extension(paths)
    assert not stale.exists()
    assert (paths.extension / "manifest.json").is_file()
    assert not list(paths.install_root.glob("extension.*.tmp"))


def test_the_deployed_extension_keeps_the_pinned_identity(paths: InstallPaths) -> None:
    """A changed id would silently stop the native host from answering."""
    deploy_extension(paths)
    manifest = json.loads((paths.extension / "manifest.json").read_text(encoding="utf-8"))
    assert "key" in manifest
    assert (paths.extension / "EXTENSION_ID").read_text(encoding="utf-8").strip() == EXTENSION_ID


# -- skill deployment ----------------------------------------------------------


def test_skills_are_installed_for_a_detected_host(tmp_path: Path, paths: InstallPaths) -> None:
    home = tmp_path / "home"
    host = AgentHost(name="claude", skills_dir=home / ".claude" / "skills", cli="claude")
    host.skills_dir.mkdir(parents=True)
    deployed = deploy_skills([host])
    assert "favhub-ask" in deployed["claude"]
    assert (host.skills_dir / "favhub-ask" / "SKILL.md").is_file()


def test_unrelated_skills_are_never_touched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    host = AgentHost(name="codex", skills_dir=home / ".codex" / "skills", cli="codex")
    host.skills_dir.mkdir(parents=True)
    mine = host.skills_dir / "someone-elses-skill"
    mine.mkdir()
    (mine / "SKILL.md").write_text("keep me", encoding="utf-8")

    deploy_skills([host])
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "keep me"


def test_an_existing_favhub_skill_is_replaced_not_merged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    host = AgentHost(name="codex", skills_dir=home / ".codex" / "skills", cli="codex")
    host.skills_dir.mkdir(parents=True)
    stale = host.skills_dir / "favhub-ask"
    stale.mkdir()
    (stale / "REMOVED.md").write_text("from an older release", encoding="utf-8")

    deploy_skills([host])
    assert not (stale / "REMOVED.md").exists()
    assert (stale / "SKILL.md").is_file()


def test_a_missing_skills_directory_is_created(tmp_path: Path) -> None:
    host = AgentHost(name="claude", skills_dir=tmp_path / "home" / ".claude" / "skills", cli="c")
    deploy_skills([host])
    assert (host.skills_dir / "favhub-ask" / "SKILL.md").is_file()


# -- MCP registration ----------------------------------------------------------


def test_mcp_is_registered_through_each_products_own_cli(data_root: Path) -> None:
    runner = fake_runner()
    hosts = [
        AgentHost(name="codex", skills_dir=Path("unused"), cli="codex"),
        AgentHost(name="claude", skills_dir=Path("unused"), cli="claude"),
    ]
    register_mcp(hosts, data_root, runner=runner, replace=False)
    commands = [" ".join(call) for call in runner.calls]
    assert any(c.startswith("codex mcp get favhub") for c in commands)
    assert any("codex mcp add favhub" in c for c in commands)
    assert any("claude mcp add --scope user favhub" in c for c in commands)
    # The absolute root is what the relay and MCP must agree on.
    assert all(str(data_root.resolve()) in c for c in commands if "mcp add" in c)


def test_no_configuration_file_is_edited_by_hand(data_root: Path, tmp_path: Path) -> None:
    """Hand-editing another product's config risks destroying unrelated servers."""
    runner = fake_runner()
    host = AgentHost(name="codex", skills_dir=tmp_path, cli="codex")
    register_mcp([host], data_root, runner=runner, replace=False)
    for call in runner.calls:
        assert call[0] == "codex"
        assert "mcp" in call


def test_an_existing_foreign_entry_is_left_alone_without_replace(data_root: Path) -> None:
    runner = fake_runner({"codex mcp get": CommandResult(0, '{"command": "some-other-tool"}', "")})
    host = AgentHost(name="codex", skills_dir=Path("unused"), cli="codex")
    report = register_mcp([host], data_root, runner=runner, replace=False)
    assert report["codex"]["status"] == "left_alone"
    assert not any("mcp add" in " ".join(call) for call in runner.calls)


def test_an_existing_favhub_entry_is_refreshed(data_root: Path) -> None:
    runner = fake_runner(
        {
            "codex mcp get": CommandResult(
                0, '{"command": "favhub-mcp", "args": ["--root", "x"]}', ""
            )
        }
    )
    host = AgentHost(name="codex", skills_dir=Path("unused"), cli="codex")
    report = register_mcp([host], data_root, runner=runner, replace=False)
    assert report["codex"]["status"] == "updated"
    assert any("mcp add" in " ".join(call) for call in runner.calls)


def test_replace_overrides_a_foreign_entry(data_root: Path) -> None:
    runner = fake_runner({"codex mcp get": CommandResult(0, '{"command": "some-other-tool"}', "")})
    host = AgentHost(name="codex", skills_dir=Path("unused"), cli="codex")
    report = register_mcp([host], data_root, runner=runner, replace=True)
    assert report["codex"]["status"] == "replaced"


def test_a_failing_cli_is_reported_not_swallowed(data_root: Path) -> None:
    runner = fake_runner({"codex mcp add": CommandResult(1, "", "codex: command failed")})
    host = AgentHost(name="codex", skills_dir=Path("unused"), cli="codex")
    report = register_mcp([host], data_root, runner=runner, replace=False)
    assert report["codex"]["status"] == "failed"
    assert "command failed" in report["codex"]["detail"]


def test_command_output_is_bounded_so_unrelated_config_cannot_leak(data_root: Path) -> None:
    runner = fake_runner({"codex mcp add": CommandResult(1, "", "x" * 5000)})
    host = AgentHost(name="codex", skills_dir=Path("unused"), cli="codex")
    report = register_mcp([host], data_root, runner=runner, replace=False)
    assert len(report["codex"]["detail"]) <= 500


# -- full setup ----------------------------------------------------------------


def test_setup_persists_the_selected_data_root(paths: InstallPaths, data_root: Path) -> None:
    report = run_setup(
        paths,
        data_root=data_root,
        hosts=[],
        registry=FakeRegistry(),
        runner=fake_runner(),
        native_host_executable=Path("favhub-native-host.exe"),
    )
    assert persisted_data_root(paths) == data_root.resolve()
    assert report["data_root"] == str(data_root.resolve())


def test_setup_reports_the_manual_chrome_step_without_claiming_it_is_done(
    paths: InstallPaths, data_root: Path
) -> None:
    report = run_setup(
        paths,
        data_root=data_root,
        hosts=[],
        registry=FakeRegistry(),
        runner=fake_runner(),
        native_host_executable=Path("favhub-native-host.exe"),
    )
    manual = report["manual_steps"]
    assert any("chrome://extensions" in step for step in manual)
    assert any(str(paths.extension) in step for step in manual)
    assert report["extension_loaded_in_chrome"] is False


def test_setup_is_repeatable(paths: InstallPaths, data_root: Path) -> None:
    registry = FakeRegistry()
    for _ in range(2):
        run_setup(
            paths,
            data_root=data_root,
            hosts=[],
            registry=registry,
            runner=fake_runner(),
            native_host_executable=Path("favhub-native-host.exe"),
        )
    assert (paths.extension / "manifest.json").is_file()
    assert paths.native_host_manifest.is_file()


def test_setup_refuses_a_data_root_that_is_not_a_directory(
    paths: InstallPaths, tmp_path: Path
) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    with pytest.raises(SetupError):
        run_setup(
            paths,
            data_root=not_a_dir,
            hosts=[],
            registry=FakeRegistry(),
            runner=fake_runner(),
            native_host_executable=Path("favhub-native-host.exe"),
        )


# -- uninstall -----------------------------------------------------------------


def test_uninstall_removes_favhub_and_preserves_the_library(
    paths: InstallPaths, data_root: Path, tmp_path: Path
) -> None:
    (data_root / "items").mkdir()
    (data_root / "items" / "notes.md").write_text("mine", encoding="utf-8")
    host = AgentHost(name="codex", skills_dir=tmp_path / "skills", cli="codex")
    host.skills_dir.mkdir(parents=True)
    keep = host.skills_dir / "unrelated"
    keep.mkdir()

    registry = FakeRegistry()
    run_setup(
        paths,
        data_root=data_root,
        hosts=[host],
        registry=registry,
        runner=fake_runner(),
        native_host_executable=Path("favhub-native-host.exe"),
    )
    report = uninstall(paths, hosts=[host], registry=registry, runner=fake_runner())

    assert not paths.extension.exists()
    assert not paths.native_host_manifest.exists()
    assert not (host.skills_dir / "favhub-ask").exists()
    assert keep.exists()
    assert (data_root / "items" / "notes.md").read_text(encoding="utf-8") == "mine"
    assert any("chrome://extensions" in step for step in report["manual_steps"])


def test_run_command_resolves_a_windows_cli_shim_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An npm CLI on Windows is a .CMD shim; the bare name fails CreateProcess."""
    from favhub import setup_service

    seen: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_which(name: str) -> str | None:
        return r"D:\npm\codex.CMD" if name == "codex" else None

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        seen.append(list(command))
        return Completed()

    monkeypatch.setattr(setup_service.shutil, "which", fake_which)
    monkeypatch.setattr(setup_service.subprocess, "run", fake_run)
    setup_service.run_command(["codex", "mcp", "get", "favhub"])
    assert seen[0][0] == r"D:\npm\codex.CMD"


def test_run_command_reports_a_missing_cli_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from favhub import setup_service

    monkeypatch.setattr(setup_service.shutil, "which", lambda _name: None)

    def explode(_command, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError(2, "The system cannot find the file specified")

    monkeypatch.setattr(setup_service.subprocess, "run", explode)
    result = setup_service.run_command(["nowhere", "mcp", "get", "favhub"])
    assert result.returncode == 127
    assert "could not run nowhere" in result.stderr


def test_installed_extension_version_reads_what_setup_deployed(tmp_path: Path) -> None:
    paths = InstallPaths.from_local_app_data(tmp_path)
    paths.extension.mkdir(parents=True, exist_ok=True)
    (paths.extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "version": "0.4.2"}), encoding="utf-8"
    )
    assert installed_extension_version(paths) == "0.4.2"


def test_installed_extension_version_is_none_when_there_is_nothing_to_compare(
    tmp_path: Path,
) -> None:
    """A source checkout has no installed copy, and must still be able to run."""
    paths = InstallPaths.from_local_app_data(tmp_path)
    assert installed_extension_version(paths) is None

    paths.extension.mkdir(parents=True, exist_ok=True)
    (paths.extension / "manifest.json").write_text("{ not json", encoding="utf-8")
    assert installed_extension_version(paths) is None

    (paths.extension / "manifest.json").write_text(
        json.dumps({"manifest_version": 3}), encoding="utf-8"
    )
    assert installed_extension_version(paths) is None
