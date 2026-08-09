"""The extension package is user-facing surface, so its limits are tested.

Permissions, allowlisted origins, and the fixed extension identity are the
things a review is most likely to wave through and a user is least able to
audit, so they are pinned here rather than trusted.
"""

import base64
import hashlib
import json
import re
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "src" / "favhub" / "browser_extension"
MANIFEST = EXTENSION / "manifest.json"

EXPECTED_ORIGINS = {
    "https://x.com/*",
    "https://twitter.com/*",
    "https://www.bilibili.com/*",
    # Favourites live on the account's own space page, and its URL is where the
    # account id the folder listing needs is read from.
    "https://space.bilibili.com/*",
    "https://api.bilibili.com/*",
    # Subtitle documents are served from this CDN rather than the API host, and
    # a saved video's transcript is the only thing fetched from it.
    "https://aisubtitle.hdslb.com/*",
    "https://www.zhihu.com/*",
}


def manifest() -> dict[str, object]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def code_of(name: str) -> str:
    """Source with `//` comments removed, so prose about a hazard is not read as
    the hazard itself — these files explain the traps they avoid."""
    source = (EXTENSION / name).read_text(encoding="utf-8")
    return "\n".join(re.sub(r"//.*$", "", line) for line in source.splitlines())


def test_manifest_uses_minimal_permissions() -> None:
    parsed = manifest()
    assert parsed["manifest_version"] == 3
    assert set(parsed["permissions"]) == {"nativeMessaging", "storage"}
    assert "<all_urls>" not in json.dumps(parsed)
    assert not ({"cookies", "history", "debugger"} & set(parsed["permissions"]))


def test_the_extension_declares_no_broad_or_unexpected_origins() -> None:
    parsed = manifest()
    assert set(parsed["host_permissions"]) == EXPECTED_ORIGINS
    for origin in parsed["host_permissions"]:
        assert origin.startswith("https://")
        assert "*." not in origin


def test_content_scripts_are_limited_to_the_declared_origins() -> None:
    parsed = manifest()
    declared: set[str] = set()
    for entry in parsed["content_scripts"]:
        declared.update(entry["matches"])
        # The isolated bridge must load before the page can issue its first
        # collection request, or a passive run would miss the opening page.
        assert entry["run_at"] == "document_start"
    assert declared <= EXPECTED_ORIGINS


def test_the_page_world_hook_is_not_a_declared_content_script() -> None:
    """The hook is injected per session, and only for passive platforms.

    Declaring it in the manifest would run it on Bilibili and Zhihu too, where
    active pagination means no page-world code is needed at all.
    """
    parsed = manifest()
    for entry in parsed["content_scripts"]:
        assert entry.get("world", "ISOLATED") == "ISOLATED"
        assert "main-hook.js" not in entry["js"]
    assert "main-hook.js" in parsed["web_accessible_resources"][0]["resources"]


def test_the_extension_identity_is_pinned() -> None:
    """A fixed key means the Native Messaging allowlist can name one id."""
    parsed = manifest()
    key = str(parsed["key"])
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    extension_id = "".join(chr(ord("a") + int(character, 16)) for character in digest)
    assert len(extension_id) == 32
    assert extension_id == (EXTENSION / "EXTENSION_ID").read_text(encoding="utf-8").strip()


def test_every_referenced_file_exists() -> None:
    parsed = manifest()
    referenced = {str(parsed["background"]["service_worker"])}
    for entry in parsed["content_scripts"]:
        referenced.update(str(name) for name in entry["js"])
    for entry in parsed["web_accessible_resources"]:
        referenced.update(str(name) for name in entry["resources"])
    referenced.add(str(parsed["action"]["default_popup"]))
    for name in referenced:
        assert (EXTENSION / name).is_file(), name


def test_no_extension_source_mentions_a_credential_field() -> None:
    for path in EXTENSION.rglob("*.js"):
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in ("document.cookie", "sessdata", "z_c0", "authorization", "bearer"):
            assert forbidden not in text, f"{path.name} mentions {forbidden}"


def test_no_extension_source_contacts_a_remote_host() -> None:
    for path in EXTENSION.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text
        for remote in ("googleapis", "cdn.", "unpkg", "jsdelivr"):
            assert remote not in text


@pytest.mark.parametrize(
    "name",
    [
        "background.js",
        "native-client.js",
        "content-isolated.js",
        "bridge.js",
        "main-hook.js",
        "session-controller.js",
        "popup.html",
        "popup.js",
        "popup.css",
    ],
)
def test_the_package_ships_every_core_file(name: str) -> None:
    assert (EXTENSION / name).is_file()


def test_the_extension_is_packaged_with_the_python_distribution() -> None:
    project = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    assert "browser_extension" in text


def test_the_built_wheel_ships_the_extension_and_every_skill(tmp_path: Path) -> None:
    """`favhub setup` copies from the installed package, so it must be inside it."""
    import subprocess
    import zipfile

    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"uv build unavailable: {result.stderr[:200]}")

    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    names = set(zipfile.ZipFile(wheels[0]).namelist())

    for required in [
        "favhub/browser_extension/manifest.json",
        "favhub/browser_extension/EXTENSION_ID",
        "favhub/browser_extension/background.js",
        "favhub/browser_extension/native-client.js",
        "favhub/browser_extension/content-isolated.js",
        "favhub/browser_extension/main-hook.js",
        "favhub/browser_extension/session-controller.js",
        "favhub/browser_extension/adapters/x.js",
        "favhub/browser_extension/adapters/bilibili.js",
        "favhub/browser_extension/popup.html",
        "favhub/browser_extension/popup.js",
        "favhub/browser_extension/popup.css",
    ]:
        assert required in names, required

    skills = {name.split("/")[2] for name in names if name.startswith("favhub/skills/")}
    expected = {path.name for path in (project / "skills").iterdir() if path.is_dir()}
    assert expected <= skills, f"missing from the wheel: {sorted(expected - skills)}"


def test_manifest_content_scripts_are_not_es_modules() -> None:
    """MV3 has no `type: "module"` for content_scripts.

    A top-level `export`/`import` there is a syntax error, so Chrome silently
    never runs the file: the extension loads, looks healthy, and does nothing.
    Only a live run catches it, which is why it is pinned here instead.
    """
    module_syntax = re.compile(r"^\s*(?:export|import)\s", re.MULTILINE)
    for entry in manifest()["content_scripts"]:
        for name in entry["js"]:
            source = (EXTENSION / name).read_text(encoding="utf-8")
            assert not module_syntax.search(source), (
                f"{name} is a manifest content script and must not use ES module syntax"
            )


def test_every_extension_url_a_content_script_names_is_web_accessible() -> None:
    """`getURL` only resolves; the page still needs the resource allowlisted."""
    parsed = manifest()
    accessible: set[str] = set()
    for entry in parsed["web_accessible_resources"]:
        accessible.update(str(resource) for resource in entry["resources"])

    for entry in parsed["content_scripts"]:
        for name in entry["js"]:
            source = (EXTENSION / name).read_text(encoding="utf-8")
            for referenced in re.findall(r'getURL\("([^"]+)"\)', source):
                assert referenced in accessible, f"{referenced} is named but not web-accessible"


def test_no_content_script_loads_its_own_code_by_dynamic_import() -> None:
    """Chromium applies the *page's* CSP to a dynamic import from a content
    script, so a site with a strict policy blocks it and the extension goes
    quiet with no error anyone sees. Shared code is declared in the manifest
    instead, which is why bridge.js precedes content-isolated.js there.
    """
    for entry in manifest()["content_scripts"]:
        for name in entry["js"]:
            assert not re.search(r"\bimport\s*\(", code_of(name)), (
                f"{name} loads code by dynamic import, which the page's CSP can block"
            )


def test_the_isolated_world_shares_the_bridge_before_it_is_used() -> None:
    """Declaration order is load order, and the entry point reads the bridge
    synchronously — reversing them would leave it undefined."""
    scripts = [entry["js"] for entry in manifest()["content_scripts"]]
    for names in scripts:
        assert names.index("bridge.js") < names.index("content-isolated.js")
