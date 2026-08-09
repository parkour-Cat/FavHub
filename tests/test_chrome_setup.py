import json
from pathlib import Path

import pytest

from favhub.chrome_setup import (
    NATIVE_HOST_KEY,
    ChromeSetupError,
    FakeRegistry,
    native_host_manifest,
    register_native_host,
    unregister_native_host,
)
from favhub.config import InstallPaths

EXTENSION_ID = "abjlifflomnolgbngicokdhphnnggmim"


@pytest.fixture
def paths(tmp_path: Path) -> InstallPaths:
    built = InstallPaths.from_local_app_data(tmp_path)
    built.ensure()
    return built


def test_the_manifest_allows_exactly_one_extension(paths: InstallPaths) -> None:
    manifest = native_host_manifest(EXTENSION_ID, Path(r"C:\tools\favhub-native-host.exe"))
    assert manifest["name"] == "com.favhub.browser"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID}/"]
    assert manifest["path"].endswith("favhub-native-host.exe")


def test_a_malformed_extension_id_is_refused() -> None:
    for bad in ["", "TOO-SHORT", "x" * 31, "abc!" + "a" * 28, "A" * 32]:
        with pytest.raises(ChromeSetupError):
            native_host_manifest(bad, Path("host.exe"))


def test_registration_writes_the_manifest_and_points_the_key_at_it(
    paths: InstallPaths,
) -> None:
    registry = FakeRegistry()
    executable = Path(r"C:\tools\favhub-native-host.exe")
    register_native_host(paths, EXTENSION_ID, executable, registry=registry)

    written = json.loads(paths.native_host_manifest.read_text(encoding="utf-8"))
    assert written["allowed_origins"] == [f"chrome-extension://{EXTENSION_ID}/"]
    assert registry.values[NATIVE_HOST_KEY] == str(paths.native_host_manifest)


def test_registration_is_repeatable(paths: InstallPaths) -> None:
    registry = FakeRegistry()
    for _ in range(3):
        register_native_host(paths, EXTENSION_ID, Path("host.exe"), registry=registry)
    assert len(registry.values) == 1


def test_registration_replaces_a_stale_manifest_atomically(paths: InstallPaths) -> None:
    registry = FakeRegistry()
    paths.native_host_manifest.write_text('{"name": "old", "path": "gone.exe"}', encoding="utf-8")
    register_native_host(paths, EXTENSION_ID, Path("host.exe"), registry=registry)
    written = json.loads(paths.native_host_manifest.read_text(encoding="utf-8"))
    assert written["name"] == "com.favhub.browser"
    assert not list(paths.native_host_dir.glob("*.tmp"))


def test_a_registry_failure_does_not_leave_a_dangling_manifest(paths: InstallPaths) -> None:
    class Refusing(FakeRegistry):
        def set_default(self, key: str, value: str) -> None:
            raise OSError("access denied")

    with pytest.raises(ChromeSetupError):
        register_native_host(paths, EXTENSION_ID, Path("host.exe"), registry=Refusing())
    assert not paths.native_host_manifest.exists()


def test_a_registry_failure_restores_a_previous_manifest(paths: InstallPaths) -> None:
    class Refusing(FakeRegistry):
        def set_default(self, key: str, value: str) -> None:
            raise OSError("access denied")

    original = '{"name": "com.favhub.browser", "path": "previous.exe"}'
    paths.native_host_manifest.write_text(original, encoding="utf-8")
    with pytest.raises(ChromeSetupError):
        register_native_host(paths, EXTENSION_ID, Path("host.exe"), registry=Refusing())
    assert paths.native_host_manifest.read_text(encoding="utf-8") == original


def test_unregister_removes_only_favhub(paths: InstallPaths) -> None:
    registry = FakeRegistry()
    registry.set_default(r"Software\Google\Chrome\NativeMessagingHosts\com.other.host", "keep.json")
    register_native_host(paths, EXTENSION_ID, Path("host.exe"), registry=registry)

    unregister_native_host(paths, registry=registry)
    assert NATIVE_HOST_KEY not in registry.values
    assert r"Software\Google\Chrome\NativeMessagingHosts\com.other.host" in registry.values
    assert not paths.native_host_manifest.exists()


def test_unregister_is_idempotent(paths: InstallPaths) -> None:
    registry = FakeRegistry()
    unregister_native_host(paths, registry=registry)
    unregister_native_host(paths, registry=registry)


def test_the_registry_key_is_the_current_user_hive_only() -> None:
    """Never HKLM: setup must not need administrator rights or affect others."""
    assert NATIVE_HOST_KEY.startswith(r"Software\Google\Chrome\NativeMessagingHosts")
    assert "HKEY_LOCAL_MACHINE" not in NATIVE_HOST_KEY
    assert "HKLM" not in NATIVE_HOST_KEY


def test_the_fake_registry_is_what_tests_use(paths: InstallPaths) -> None:
    """A guard against a future edit defaulting these tests onto the real hive."""
    registry = FakeRegistry()
    register_native_host(paths, EXTENSION_ID, Path("host.exe"), registry=registry)
    assert isinstance(registry.values, dict)
    assert registry.written_keys == [NATIVE_HOST_KEY]
