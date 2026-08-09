from pathlib import Path

import pytest

from favhub.config import FavHubPaths
from favhub.database import Database
from favhub.embedding import EmbeddingProfile, encode_float32
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.embedding_runtime import (
    EmbeddingDependencyUnavailableError,
    EmbeddingModelCacheMissingError,
    EmbeddingRuntime,
    EmbeddingRuntimeError,
)


def profile(digest: str, *, profile_id: str = "profile-1") -> EmbeddingProfile:
    return EmbeddingProfile(
        id=profile_id,
        provider="fastembed",
        provider_version="0.8",
        model="intfloat/multilingual-e5-small",
        dimensions=2,
        normalization="l2",
        max_input_tokens=512,
        segment_tokens=480,
        overlap_tokens=32,
        artifact_digest=digest,
    )


def test_profile_insert_activate_and_idempotence(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        store = EmbeddingProfileStore(db)
        first = profile("a" * 64)
        assert store.activate(first) is True
        assert store.active() == first
        assert store.activate(first) is False
        assert store.get(first.id) == first
        assert (
            db.connection.execute(
                "SELECT COUNT(*) FROM embedding_profiles WHERE is_active = 1"
            ).fetchone()[0]
            == 1
        )
    finally:
        db.close()


def test_changed_digest_replaces_active_profile(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        store = EmbeddingProfileStore(db)
        first = profile("a" * 64)
        second = profile("b" * 64, profile_id="profile-2")
        assert store.activate(first)
        assert store.activate(second)
        assert store.active() == second
        assert (
            db.connection.execute(
                "SELECT is_active FROM embedding_profiles WHERE id = ?", (first.id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_same_id_different_identity_preserves_active_profile_and_vectors(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        store = EmbeddingProfileStore(db)
        first = profile("a" * 64)
        assert store.activate(first)
        connection = db.connection
        connection.execute(
            """INSERT INTO items(platform, source_id, content_hash, item_dir,
               published_at, first_seen_at, index_input_hash)
               VALUES ('x', 'item', 'hash', '.', '2026-01-01', '2026-01-01', 'input')"""
        )
        cursor = connection.execute(
            """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
               line_start, line_end, text, input_hash, created_at)
               VALUES ('x', 'item', 0, 'content.md', 1, 1, 'text', 'input', '2026-01-01')"""
        )
        connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (int(cursor.lastrowid), first.id, encode_float32((1.0, 0.0), dimensions=2)),
        )

        conflicting = profile("b" * 64, profile_id=first.id)
        with pytest.raises(ValueError, match="identity"):
            store.activate(conflicting)
        assert store.active() == first
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id = ?", (first.id,)
            ).fetchone()[0]
            == 1
        )
    finally:
        db.close()


def test_inactive_profile_with_same_identity_can_be_reactivated(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        store = EmbeddingProfileStore(db)
        first = profile("a" * 64)
        second = profile("b" * 64, profile_id="profile-2")
        assert store.activate(first)
        assert store.activate(second)
        assert store.activate(first)
        assert store.active() == first
        assert store.get(second.id) == second
    finally:
        db.close()


def test_summary_without_active_profile(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        summary = EmbeddingProfileStore(db).summary()
        assert summary.state == "disabled"
        assert summary.active_profile is None
        assert summary.current_chunks == 0
    finally:
        db.close()


class FakeProvider:
    name = "fake"
    version = "1"
    dimensions = 2
    max_input_tokens = 512
    cache_dir = None

    def __init__(self, **kwargs: object):
        self.kwargs = kwargs

    def embed_queries(self, texts: tuple[str, ...]):
        return ((1.0, 0.0),) * len(texts)

    def embed_documents(self, texts: tuple[str, ...]):
        return ((1.0, 0.0),) * len(texts)

    def artifact_digest(self) -> str:
        return "c" * 64


def test_runtime_initialize_returns_profile_without_activating(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        providers: list[FakeProvider] = []

        def factory(**kwargs: object) -> FakeProvider:
            provider = FakeProvider(**kwargs)
            providers.append(provider)
            return provider

        runtime = EmbeddingRuntime(paths, store, provider_factory=factory)
        created = runtime.initialize()
        assert created.artifact_digest == "c" * 64
        assert len(providers) == 1
        assert providers[0].kwargs["local_files_only"] is False
        assert store.active() is None
        assert store.activate(created)
    finally:
        db.close()


def test_runtime_initialize_runs_english_and_chinese_query_document_probes(
    tmp_path: Path,
) -> None:
    class Probed(FakeProvider):
        queries: list[tuple[str, ...]] = []
        documents: list[tuple[str, ...]] = []

        def embed_queries(self, texts: tuple[str, ...]):
            self.queries.append(texts)
            return super().embed_queries(texts)

        def embed_documents(self, texts: tuple[str, ...]):
            self.documents.append(texts)
            return super().embed_documents(texts)

    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        provider = Probed()
        runtime = EmbeddingRuntime(paths, store, provider_factory=lambda **_: provider)
        runtime.initialize()
        assert provider.queries == [("FavHub embedding probe", "收藏内容语义检索探针")]
        assert provider.documents == [("FavHub embedding probe", "收藏内容语义检索探针")]
    finally:
        db.close()


def test_runtime_load_active_checks_cache_identity_and_failure_modes(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        active = profile("c" * 64)
        assert store.activate(active)
        with pytest.raises(EmbeddingModelCacheMissingError):
            EmbeddingRuntime(
                paths, store, provider_factory=lambda **_: FakeProvider()
            ).load_active()

        (paths.models / "model.onnx").write_bytes(b"model")
        calls: list[dict[str, object]] = []

        def factory(**kwargs: object) -> FakeProvider:
            calls.append(kwargs)
            return FakeProvider(**kwargs)

        loaded = EmbeddingRuntime(paths, store, provider_factory=factory).load_active()
        assert isinstance(loaded, FakeProvider)
        assert calls[0]["local_files_only"] is True

        class Mismatch(FakeProvider):
            def artifact_digest(self) -> str:
                return "d" * 64

        with pytest.raises(EmbeddingModelCacheMissingError, match="identity"):
            EmbeddingRuntime(paths, store, provider_factory=lambda **_: Mismatch()).load_active()

        def missing(**_: object) -> FakeProvider:
            raise ImportError("fastembed missing")

        with pytest.raises(EmbeddingDependencyUnavailableError):
            EmbeddingRuntime(paths, store, provider_factory=missing).load_active()
    finally:
        db.close()


def test_a_cached_provider_is_not_re_verified_against_the_files_on_disk(
    tmp_path: Path,
) -> None:
    """Hashing the model again on every call is 464 MB of nothing.

    The digest earns its keep once, when the provider is built from those
    files. After that the provider is an ONNX session resident in memory, and
    re-reading model.onnx says nothing about it. Doing so per call — which is
    per batch — cost 1.35s and a 448 MB transient allocation each time, and is
    where bad_alloc failures in long builds came from.
    """
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        assert store.activate(profile("c" * 64))
        (paths.models / "model.onnx").write_bytes(b"model")

        digests = 0

        class Counting(FakeProvider):
            def artifact_digest(self) -> str:
                nonlocal digests
                digests += 1
                return "c" * 64

        constructed = 0

        def factory(**kwargs: object) -> Counting:
            nonlocal constructed
            constructed += 1
            return Counting(**kwargs)

        runtime = EmbeddingRuntime(paths, store, provider_factory=factory)
        first = runtime.load_active()
        assert digests == 1

        for _ in range(20):
            assert runtime.load_active() is first
        assert (constructed, digests) == (1, 1)
    finally:
        db.close()


def test_runtime_load_active_reuses_provider_instance(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        active = profile("c" * 64)
        assert store.activate(active)
        (paths.models / "model.onnx").write_bytes(b"model")
        calls: list[dict[str, object]] = []

        def factory(**kwargs: object) -> FakeProvider:
            calls.append(kwargs)
            return FakeProvider(**kwargs)

        runtime = EmbeddingRuntime(paths, store, provider_factory=factory)
        first = runtime.load_active(local_only=True)
        second = runtime.load_active(local_only=True)

        assert first is second
        assert len(calls) == 1
        assert calls[0]["local_files_only"] is True
    finally:
        db.close()


def test_runtime_cache_scan_error_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        assert store.activate(profile("c" * 64))

        def denied(_self: Path, _pattern: str):
            raise PermissionError("denied")

        monkeypatch.setattr(type(paths.models), "rglob", denied)
        with pytest.raises(EmbeddingModelCacheMissingError, match="invalid"):
            EmbeddingRuntime(
                paths, store, provider_factory=lambda **_: FakeProvider()
            ).load_active()
    finally:
        db.close()


def test_summary_counts_only_current_chunks_and_vectors(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "db.sqlite3")
    try:
        store = EmbeddingProfileStore(db)
        active = profile("c" * 64)
        assert store.activate(active)
        connection = db.connection
        connection.execute(
            """INSERT INTO items(platform, source_id, content_hash, item_dir,
               published_at, first_seen_at, index_input_hash, access_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("x", "current", "hash", ".", "2026-01-01", "2026-01-01", "current", "available"),
        )
        connection.execute(
            """INSERT INTO items(platform, source_id, content_hash, item_dir,
               published_at, first_seen_at, index_input_hash, access_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("x", "stale", "hash", ".", "2026-01-01", "2026-01-01", "new", "available"),
        )
        connection.execute(
            """INSERT INTO items(platform, source_id, content_hash, item_dir,
               published_at, first_seen_at, index_input_hash, access_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("x", "missing", "hash", ".", "2026-01-01", "2026-01-01", "missing", "missing"),
        )
        connection.execute(
            """INSERT INTO items(platform, source_id, content_hash, item_dir,
               published_at, first_seen_at, index_input_hash, access_status)
               VALUES ('x', 'current-failed', 'hash', '.', '2026-01-01',
                       '2026-01-01', 'failed-input', 'available')"""
        )
        chunk_ids: dict[str, int] = {}
        chunk_inputs = (("current", "current"), ("stale", "old"), ("missing", "missing"))
        for source_id, input_hash in chunk_inputs:
            cursor = connection.execute(
                """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
                   line_start, line_end, heading, text, input_hash, created_at)
                   VALUES (?, ?, 0, 'content.md', 1, 1, NULL, ?, ?, '2026-01-01')""",
                ("x", source_id, source_id, input_hash),
            )
            chunk_ids[source_id] = int(cursor.lastrowid)
        vector = encode_float32((1.0, 0.0), dimensions=2)
        for source_id in chunk_ids:
            connection.execute(
                """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
                   token_start, token_end, vector, created_at)
                   VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
                (chunk_ids[source_id], active.id, vector),
            )
        corrupt_cursor = connection.execute(
            """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
               line_start, line_end, heading, text, input_hash, created_at)
               VALUES ('x', 'current', 1, 'content.md', 2, 2, NULL,
                       'corrupt', 'current', '2026-01-01')"""
        )
        connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (int(corrupt_cursor.lastrowid), active.id, b"bad"),
        )
        connection.execute(
            """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
               line_start, line_end, heading, text, input_hash, created_at)
               VALUES ('x', 'current-failed', 0, 'content.md', 1, 1, NULL,
                       'failed', 'failed-input', '2026-01-01')"""
        )
        current_task_hash = embedding_task_input_hash(active.id, "current")
        tasks = (
            ("pending-task", "current", current_task_hash, "pending", None),
            (
                "failed-task",
                "current-failed",
                embedding_task_input_hash(active.id, "failed-input"),
                "pending",
                "boom",
            ),
            (
                "stale-task",
                "current",
                embedding_task_input_hash(active.id, "old"),
                "failed",
                "stale",
            ),
            (
                "old-profile-task",
                "current",
                embedding_task_input_hash("old-profile", "current"),
                "failed",
                "old",
            ),
        )
        for task_id, source_id, input_hash, status, error in tasks:
            connection.execute(
                """INSERT INTO enrichment_tasks(id, platform, source_id, kind,
                   input_hash, status, attempts, error, created_at, updated_at)
                   VALUES (?, 'x', ?, 'embed_content', ?, ?, 0, ?,
                           '2026-01-01', '2026-01-01')""",
                (task_id, source_id, input_hash, status, error),
            )
        inactive = profile("d" * 64, profile_id="inactive")
        connection.execute(
            """INSERT INTO embedding_profiles(id, provider, provider_version, model,
               dimensions, normalization, max_input_tokens, segment_tokens,
               overlap_tokens, artifact_digest, config_json, is_active, initialized_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 0, '2026-01-01')""",
            (
                inactive.id,
                inactive.provider,
                inactive.provider_version,
                inactive.model,
                inactive.dimensions,
                inactive.normalization,
                inactive.max_input_tokens,
                inactive.segment_tokens,
                inactive.overlap_tokens,
                inactive.artifact_digest,
            ),
        )
        connection.execute(
            """INSERT INTO embedding_build_runs(id, profile_id, status, counts_json,
               error_json, started_at, finished_at)
               VALUES ('build', ?, 'completed', '{"processed":2}', NULL,
                       '2026-01-01', '2026-01-02')""",
            (active.id,),
        )
        connection.execute(
            """INSERT INTO embedding_build_runs(id, profile_id, status, counts_json,
               error_json, started_at, finished_at)
               VALUES ('inactive-build', ?, 'completed', '{"processed":99}', NULL,
                       '2026-02-01', '2026-02-02')""",
            (inactive.id,),
        )
        summary = store.summary()
        assert summary.current_chunks == 3
        assert summary.embedded_chunks == 1
        assert summary.corrupt_vectors == 1
        assert summary.pending_tasks == 2
        assert summary.failed_tasks == 1
        assert summary.last_build_report == {
            "processed": 2,
            "status": "completed",
            "finished_at": "2026-01-02",
        }
    finally:
        db.close()


def test_runtime_failed_probe_leaves_store_without_active_profile(tmp_path: Path) -> None:
    class Broken(FakeProvider):
        def embed_queries(self, texts: tuple[str, ...]):
            return ((float("nan"), 0.0),) * len(texts)

    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        runtime = EmbeddingRuntime(paths, store, provider_factory=lambda **_: Broken())
        with pytest.raises(EmbeddingRuntimeError):
            runtime.initialize()
        assert store.active() is None
    finally:
        db.close()


@pytest.mark.parametrize("failure", ["l2", "manifest"])
def test_runtime_invalid_probe_or_manifest_does_not_return_profile(
    tmp_path: Path, failure: str
) -> None:
    class Broken(FakeProvider):
        def embed_documents(self, texts: tuple[str, ...]):
            if failure == "l2":
                return ((2.0, 0.0),) * len(texts)
            return super().embed_documents(texts)

        def artifact_digest(self) -> str:
            return "invalid" if failure == "manifest" else super().artifact_digest()

    db = Database.open(tmp_path / "db.sqlite3")
    try:
        paths = FavHubPaths.from_root(tmp_path / "root")
        paths.ensure()
        store = EmbeddingProfileStore(db)
        runtime = EmbeddingRuntime(paths, store, provider_factory=lambda **_: Broken())
        with pytest.raises(EmbeddingRuntimeError):
            runtime.initialize()
        assert store.active() is None
    finally:
        db.close()
