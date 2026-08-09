import importlib
from pathlib import Path

import pytest

from favhub.application import Application
from favhub.embedding import EmbeddingProfile
from favhub.maintenance import StartupMaintenance
from favhub.root_lock import DataRootBusyError


def test_application_instances_for_same_root_are_mutually_exclusive(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first = Application.open(root)
    try:
        assert (root / "state" / "favhub.lock").is_file()
        task_id = first.queue.enqueue("x", "42", "enrich_item", "hash-1")
        assert first.queue.claim_next() is not None

        # A distinct type, not a bare RuntimeError: the MCP server degrades on
        # a busy root and dies on everything else, so the two must be telling
        # apart without reading the message.
        with pytest.raises(DataRootBusyError, match="already in use"):
            Application.open(root)

        row = first.database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "running"
    finally:
        first.close()


def test_close_releases_root_lock_and_next_open_runs_maintenance(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first = Application.open(root)
    task_id = first.queue.enqueue("x", "42", "enrich_item", "hash-1")
    assert first.queue.claim_next() is not None
    first.close()

    second = Application.open(root)
    try:
        row = second.database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending"
    finally:
        second.close()


def test_next_open_marks_interrupted_embedding_build_failed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first = Application.open(root)
    assert first.embedding_profiles is not None
    profile = EmbeddingProfile(
        id="profile-1",
        provider="fake",
        provider_version="1",
        model="fake-model",
        dimensions=2,
        normalization="l2",
        max_input_tokens=8,
        segment_tokens=2,
        overlap_tokens=1,
        artifact_digest="a" * 64,
    )
    first.embedding_profiles.activate(profile)
    first.database.connection.execute(
        """INSERT INTO embedding_build_runs(
               id, profile_id, status, max_items, counts_json,
               error_json, started_at, finished_at
           ) VALUES ('interrupted', ?, 'running', 10, '{}', NULL, '2026-01-01', NULL)""",
        (profile.id,),
    )
    first.close()

    second = Application.open(root)
    try:
        row = second.database.connection.execute(
            "SELECT status, error_json, finished_at FROM embedding_build_runs WHERE id = ?",
            ("interrupted",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "failed"
        assert '"code": "build_interrupted"' in row["error_json"]
        assert row["finished_at"] is not None
    finally:
        second.close()


def test_application_open_does_not_import_or_construct_fastembed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = importlib.import_module

    def guarded_import(name: str, package: str | None = None):
        if name == "fastembed" or name.startswith("fastembed."):
            raise AssertionError("FastEmbed must be loaded only on explicit initialization")
        return original_import(name, package)

    monkeypatch.setattr(importlib, "import_module", guarded_import)
    app = Application.open(tmp_path / "root")
    try:
        assert app.embedding_profiles is not None
        assert app.embedding_runtime is not None
        assert app.embedding_profiles.active() is None
    finally:
        app.close()


def test_startup_failure_releases_database_and_root_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = StartupMaintenance.run

    def fail(_self: StartupMaintenance) -> None:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(StartupMaintenance, "run", fail)
    root = tmp_path / "root"
    with pytest.raises(RuntimeError, match="startup failed"):
        Application.open(root)

    monkeypatch.setattr(StartupMaintenance, "run", original_run)
    app = Application.open(root)
    app.close()
