import json
from pathlib import Path

import pytest

from favhub.chrome_setup import NATIVE_HOST_KEY, FakeRegistry
from favhub.config import InstallPaths, save_install_config
from favhub.doctor import CheckStatus, run_doctor
from favhub.setup_service import AgentHost, CommandResult, run_setup

EXTENSION_ID = "abjlifflomnolgbngicokdhphnnggmim"


def fake_runner(script: dict[str, CommandResult] | None = None):
    responses = script or {}

    def run(command: list[str]) -> CommandResult:
        return responses.get(" ".join(command[:3]), CommandResult(0, "", ""))

    return run


@pytest.fixture
def installed(tmp_path: Path) -> tuple[InstallPaths, Path, FakeRegistry]:
    paths = InstallPaths.from_local_app_data(tmp_path / "local")
    data_root = tmp_path / "library"
    data_root.mkdir()
    registry = FakeRegistry()
    executable = tmp_path / "favhub-native-host.exe"
    executable.write_text("", encoding="utf-8")
    run_setup(
        paths,
        data_root=data_root,
        hosts=[],
        registry=registry,
        runner=fake_runner(),
        native_host_executable=executable,
    )
    return paths, data_root, registry


def by_name(report: dict) -> dict[str, dict]:
    return {check["name"]: check for check in report["checks"]}


def test_a_complete_install_reports_every_component_ok(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, _data_root, registry = installed
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    checks = by_name(report)
    for name in [
        "python_package",
        "data_root",
        "extension_files",
        "extension_identity",
        "native_host_manifest",
        "chrome_registry",
        "native_executable",
    ]:
        assert checks[name]["status"] == CheckStatus.OK, checks[name]
    assert report["ok"] is True


def test_the_report_is_json_serialisable(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, _root, registry = installed
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    assert json.loads(json.dumps(report))["ok"] is True


def test_a_missing_extension_is_reported_not_hidden(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, _root, registry = installed
    (paths.extension / "background.js").unlink()
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    assert by_name(report)["extension_files"]["status"] == CheckStatus.FAILED
    assert report["ok"] is False


def test_a_registry_value_pointing_elsewhere_is_a_failure(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, _root, registry = installed
    registry.set_default(NATIVE_HOST_KEY, r"C:\somewhere\else.json")
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    check = by_name(report)["chrome_registry"]
    assert check["status"] == CheckStatus.FAILED
    assert "else.json" in check["detail"]


def test_a_manifest_allowing_a_different_extension_is_caught(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    """The commonest silent breakage: id drift between manifest and extension."""
    paths, _root, registry = installed
    manifest = json.loads(paths.native_host_manifest.read_text(encoding="utf-8"))
    manifest["allowed_origins"] = ["chrome-extension://" + "p" * 32 + "/"]
    paths.native_host_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    check = by_name(report)["extension_identity"]
    assert check["status"] == CheckStatus.FAILED
    assert EXTENSION_ID in check["detail"]


def test_a_missing_native_executable_is_reported(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, _root, registry = installed
    manifest = json.loads(paths.native_host_manifest.read_text(encoding="utf-8"))
    manifest["path"] = r"C:\gone\favhub-native-host.exe"
    paths.native_host_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    assert by_name(report)["native_executable"]["status"] == CheckStatus.FAILED


def test_a_data_root_that_vanished_is_reported(
    installed: tuple[InstallPaths, Path, FakeRegistry], tmp_path: Path
) -> None:
    paths, _root, registry = installed
    save_install_config(paths, tmp_path / "not-there")
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    assert by_name(report)["data_root"]["status"] == CheckStatus.FAILED


def test_a_running_pipe_is_reported_and_a_stale_descriptor_is_not_mistaken_for_one(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, data_root, registry = installed
    state = data_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "browser-pipe.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pipe": r"\\.\pipe\favhub-test",
                "authKey": "YWJj",
                # A pid that cannot be alive: a descriptor left by a crash must
                # not read as "FavHub is running".
                "pid": 999_999_999,
                "protocolVersion": 1,
            }
        ),
        encoding="utf-8",
    )
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    check = by_name(report)["runtime_descriptor"]
    assert check["status"] == CheckStatus.WARNING
    assert "stale" in check["detail"].lower()


def test_a_pid_that_cannot_be_inspected_is_not_running_rather_than_a_crash(
    installed: tuple[InstallPaths, Path, FakeRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows recycles pids, and the one in a stale descriptor can land on a
    process this user may not open. `OpenProcess` then fails in a way CPython
    surfaces as SystemError, not OSError — so catching OSError alone let it
    escape and took down the whole report. Diagnosing an install is the one
    thing that has to work when everything else is broken.
    """
    paths, data_root, registry = installed
    state = data_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "browser-pipe.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pipe": r"\\.\pipe\favhub-test",
                "authKey": "YWJj",
                "pid": 4,
                "protocolVersion": 1,
            }
        ),
        encoding="utf-8",
    )

    def refuses(_pid: int, _signal: int) -> None:
        raise SystemError("<class 'OSError'> returned a result with an exception set")

    monkeypatch.setattr("favhub.doctor.os.kill", refuses)

    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    check = by_name(report)["runtime_descriptor"]
    assert check["status"] == CheckStatus.WARNING
    assert "stale" in check["detail"].lower()


def test_no_descriptor_is_informational_not_a_failure(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    """FavHub is only expected to be running while an Agent session is open."""
    paths, _root, registry = installed
    report = run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    check = by_name(report)["runtime_descriptor"]
    assert check["status"] == CheckStatus.INFO
    assert report["ok"] is True


def test_agent_hosts_are_checked_for_skills_and_mcp(
    installed: tuple[InstallPaths, Path, FakeRegistry], tmp_path: Path
) -> None:
    paths, _root, registry = installed
    host = AgentHost(name="codex", skills_dir=tmp_path / "skills", cli="codex")
    host.skills_dir.mkdir(parents=True)
    (host.skills_dir / "favhub-ask").mkdir()

    runner = fake_runner({"codex mcp get": CommandResult(0, '{"command": "favhub-mcp"}', "")})
    report = run_doctor(paths, registry=registry, runner=runner, hosts=[host])
    checks = by_name(report)
    assert checks["codex_skills"]["status"] == CheckStatus.OK
    assert checks["codex_mcp"]["status"] == CheckStatus.OK


def test_a_host_without_favhub_registered_is_a_warning_not_a_failure(
    installed: tuple[InstallPaths, Path, FakeRegistry], tmp_path: Path
) -> None:
    paths, _root, registry = installed
    host = AgentHost(name="claude", skills_dir=tmp_path / "skills", cli="claude")
    runner = fake_runner({"claude mcp get": CommandResult(1, "", "not found")})
    report = run_doctor(paths, registry=registry, runner=runner, hosts=[host])
    checks = by_name(report)
    assert checks["claude_mcp"]["status"] == CheckStatus.WARNING
    assert checks["claude_skills"]["status"] == CheckStatus.WARNING


def test_doctor_never_writes_anything(
    installed: tuple[InstallPaths, Path, FakeRegistry],
) -> None:
    paths, _root, registry = installed
    before = sorted(path.name for path in paths.install_root.rglob("*"))
    keys_before = dict(registry.values)
    run_doctor(paths, registry=registry, runner=fake_runner(), hosts=[])
    assert sorted(path.name for path in paths.install_root.rglob("*")) == before
    assert registry.values == keys_before
