import tomllib
from pathlib import Path

from favhub.config import FavHubPaths


def test_paths_are_derived_from_one_root(tmp_path: Path) -> None:
    paths = FavHubPaths.from_root(tmp_path / "knowledge")

    assert paths.root == (tmp_path / "knowledge").resolve()
    assert paths.items == paths.root / "items"
    assert paths.state == paths.root / "state"
    assert paths.models == paths.root / "models"
    assert paths.database == paths.state / "favhub.sqlite3"


def test_ensure_creates_only_managed_top_level_directories(tmp_path: Path) -> None:
    paths = FavHubPaths.from_root(tmp_path / "knowledge")

    paths.ensure()

    assert paths.items.is_dir()
    assert paths.state.is_dir()
    assert paths.models.is_dir()
    assert {entry.name for entry in paths.root.iterdir()} == {"items", "state", "models"}
    assert not paths.database.exists()


def test_embedding_optional_dependency_is_exactly_pinned() -> None:
    with Path("pyproject.toml").open("rb") as pyproject:
        project = tomllib.load(pyproject)["project"]

    assert project["optional-dependencies"]["embedding"] == [
        "fastembed>=0.8,<0.9",
        "numpy>=2,<3",
    ]
