from collections.abc import Sequence
from pathlib import Path

import pytest

from favhub.database import Database
from favhub.embedding import EmbeddingProfile, encode_float32
from favhub.embedding_indexing import EmbeddingIndexer
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.enrichment_queue import EnrichmentQueue
from favhub.semantic_chunking import SemanticSegment


def embedding_profile(profile_id: str = "profile-1") -> EmbeddingProfile:
    return EmbeddingProfile(
        id=profile_id,
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


class FakeProvider:
    name = "fake"
    version = "1"
    dimensions = 2

    def __init__(self) -> None:
        self.inputs: list[tuple[str, ...]] = []

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, float], ...]:
        self.inputs.append(tuple(texts))
        return tuple((1.0, 0.0) for _ in texts)

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, float], ...]:
        return self.embed_documents(texts)

    def tokenize(self, text: str) -> tuple[int, ...]:
        return tuple(range(len(text.split())))

    def decode_tokens(self, tokens: Sequence[int]) -> str:
        return " ".join(f"token-{token}" for token in tokens)


def add_indexed_item(database: Database, queue: EnrichmentQueue, *, input_hash: str = "input"):
    database.connection.execute(
        """INSERT INTO items(platform, source_id, content_hash, item_dir,
           published_at, first_seen_at, index_input_hash)
           VALUES ('x', '42', 'content', '.', '2026-01-01', '2026-01-01', ?)""",
        (input_hash,),
    )
    cursor = database.connection.execute(
        """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
           line_start, line_end, heading, text, input_hash, created_at)
           VALUES ('x', '42', 0, 'content.md', 1, 1, NULL,
                   'one two three', ?, '2026-01-01')""",
        (input_hash,),
    )
    index_id = queue.enqueue("x", "42", "index_content", input_hash)
    database.connection.execute(
        "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (index_id,)
    )
    return int(cursor.lastrowid)


def add_named_indexed_item(
    database: Database,
    queue: EnrichmentQueue,
    source_id: str,
    *,
    access_status: str = "available",
    input_hash: str = "input",
) -> int:
    database.connection.execute(
        """INSERT INTO items(platform, source_id, content_hash, item_dir,
           published_at, first_seen_at, index_input_hash, access_status)
           VALUES ('x', ?, 'content', '.', '2026-01-01', '2026-01-01', ?, ?)""",
        (source_id, input_hash, access_status),
    )
    cursor = database.connection.execute(
        """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
           line_start, line_end, heading, text, input_hash, created_at)
           VALUES ('x', ?, 0, 'content.md', 1, 1, NULL,
                   'one two three', ?, '2026-01-01')""",
        (source_id, input_hash),
    )
    index_id = queue.enqueue("x", source_id, "index_content", input_hash)
    database.connection.execute(
        "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (index_id,)
    )
    return int(cursor.lastrowid)


def test_segmenter_and_provider_loader_are_public_injection_seams(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_indexed_item(database, queue)
        task_hash = embedding_task_input_hash(profile.id, "input")
        queue.enqueue("x", "42", "embed_content", task_hash)
        provider = FakeProvider()
        transaction_states: list[bool] = []

        def segmenter(
            text: str, *, segment_tokens: int, overlap_tokens: int
        ) -> tuple[SemanticSegment, ...]:
            transaction_states.append(database.connection.in_transaction)
            assert text == "one two three"
            assert (segment_tokens, overlap_tokens) == (2, 1)
            return (SemanticSegment(0, 0, 3, "custom segment"),)

        indexer = EmbeddingIndexer(
            database,
            queue,
            profiles,
            provider_loader=lambda: provider,
            segmenter=segmenter,
        )
        result = indexer.index_next()

        assert result is not None and result.vector_count == 1
        assert provider.inputs == [("custom segment",)]
        assert transaction_states == [False]
    finally:
        database.close()


def test_index_tasks_embeds_segments_from_multiple_items_in_one_call(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_named_indexed_item(database, queue, "first")
        add_named_indexed_item(database, queue, "second")
        for source_id in ("first", "second"):
            queue.enqueue(
                "x", source_id, "embed_content", embedding_task_input_hash(profile.id, "input")
            )
        tasks = (queue.claim_next(kind="embed_content"), queue.claim_next(kind="embed_content"))
        assert all(task is not None for task in tasks)

        provider = FakeProvider()
        results = EmbeddingIndexer(database, queue, profiles, provider=provider).index_tasks(
            tuple(task for task in tasks if task is not None)
        )

        assert [result.vector_count for result in results] == [2, 2]
        assert provider.inputs == [("token-0 token-1", "token-1 token-2") * 2]
        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 4
        )
    finally:
        database.close()


def test_embedding_task_hash_has_stable_embed_v1_identity() -> None:
    assert embedding_task_input_hash("profile-1", "input") == (
        "d4c3c003cc9eb3f854e69850b236c120726c5185f7d4a9f8585b8b4eba5cef45"
    )


def test_reindex_missing_is_disabled_without_active_profile(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        add_indexed_item(database, queue)
        indexer = EmbeddingIndexer(database, queue, EmbeddingProfileStore(database))
        assert indexer.reindex_missing() == 0
        assert queue.claim_next(kind="embed_content") is None
    finally:
        database.close()


def test_item_cas_failure_requeues_without_replacing_vectors(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        chunk_id = add_indexed_item(database, queue)
        database.connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (chunk_id, profile.id, encode_float32((0.0, 1.0), dimensions=2)),
        )
        task_id = queue.enqueue(
            "x", "42", "embed_content", embedding_task_input_hash(profile.id, "input")
        )

        class RacingProvider(FakeProvider):
            def embed_documents(self, texts: Sequence[str]):
                database.connection.execute(
                    "UPDATE items SET index_input_hash='changed' WHERE source_id='42'"
                )
                return super().embed_documents(texts)

        with pytest.raises(ValueError, match="item index input changed"):
            EmbeddingIndexer(
                database, queue, profiles, provider_loader=lambda: RacingProvider()
            ).index_next()
        assert database.connection.execute(
            "SELECT vector FROM chunk_embeddings WHERE chunk_id=?", (chunk_id,)
        ).fetchone()[0] == encode_float32((0.0, 1.0), dimensions=2)
        assert (
            database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            == "pending"
        )
    finally:
        database.close()


def test_profile_cas_failure_requeues_when_active_profile_changes(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        first = embedding_profile()
        second = embedding_profile("profile-2")
        profiles.activate(first)
        add_indexed_item(database, queue)
        queue.enqueue("x", "42", "embed_content", embedding_task_input_hash(first.id, "input"))

        class RacingProvider(FakeProvider):
            def embed_documents(self, texts: Sequence[str]):
                profiles.activate(second)
                return super().embed_documents(texts)

        with pytest.raises(ValueError, match="active embedding profile changed"):
            EmbeddingIndexer(
                database, queue, profiles, provider_loader=lambda: RacingProvider()
            ).index_next()
        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
        )
        assert profiles.active() == second
    finally:
        database.close()


def test_inference_failure_preserves_old_vectors_and_requeues_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        chunk_id = add_indexed_item(database, queue)
        old = encode_float32((0.0, 1.0), dimensions=2)
        database.connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (chunk_id, profile.id, old),
        )
        task_id = queue.enqueue(
            "x", "42", "embed_content", embedding_task_input_hash(profile.id, "input")
        )

        class BrokenProvider(FakeProvider):
            def embed_documents(self, texts: Sequence[str]):
                raise RuntimeError("inference failed")

        indexer = EmbeddingIndexer(
            database, queue, profiles, provider_loader=lambda: BrokenProvider()
        )
        with pytest.raises(RuntimeError, match="inference failed"):
            indexer.index_next()
        assert (
            database.connection.execute(
                "SELECT vector FROM chunk_embeddings WHERE chunk_id=?", (chunk_id,)
            ).fetchone()[0]
            == old
        )
        assert tuple(
            database.connection.execute(
                "SELECT status, error FROM enrichment_tasks WHERE id=?", (task_id,)
            ).fetchone()
        ) == ("pending", "inference failed")
    finally:
        database.close()


def test_force_invalidation_requeues_completed_task_and_deletes_active_vectors(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_indexed_item(database, queue)
        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())
        assert indexer.reindex_missing() == 1
        assert indexer.index_next() is not None
        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 2
        )

        assert indexer.reindex_missing(force=True) == 1

        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
        )
        task = queue.claim_next(kind="embed_content")
        assert task is not None
    finally:
        database.close()


def test_force_invalidation_clears_all_active_vectors_and_only_requeues_eligible_items(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        inactive = embedding_profile("profile-inactive")
        active = embedding_profile("profile-active")
        profiles.activate(inactive)
        profiles.activate(active)
        chunks = {
            source_id: add_named_indexed_item(
                database, queue, source_id, access_status=access_status
            )
            for source_id, access_status in (
                ("eligible", "available"),
                ("missing", "missing"),
                ("restricted", "restricted"),
            )
        }
        completed_task = queue.enqueue(
            "x",
            "eligible",
            "embed_content",
            embedding_task_input_hash(active.id, "input"),
        )
        database.connection.execute(
            "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (completed_task,)
        )
        vector = encode_float32((1.0, 0.0), dimensions=2)
        database.connection.executemany(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            [
                (chunk_id, profile_id, vector)
                for chunk_id in chunks.values()
                for profile_id in (active.id, inactive.id)
            ],
        )

        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())
        assert indexer.reindex_missing(force=True) == 1

        active_count = database.connection.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id=?", (active.id,)
        ).fetchone()[0]
        inactive_count = database.connection.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id=?", (inactive.id,)
        ).fetchone()[0]
        assert active_count == 0
        assert inactive_count == 3
        assert (
            database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id=?", (completed_task,)
            ).fetchone()[0]
            == "pending"
        )
        noneligible = database.connection.execute(
            """SELECT COUNT(*) FROM enrichment_tasks
               WHERE kind='embed_content' AND source_id IN ('missing', 'restricted')"""
        ).fetchone()[0]
        assert noneligible == 0
    finally:
        database.close()


def test_force_invalidation_rolls_back_every_item_when_requeue_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        active = embedding_profile()
        profiles.activate(active)
        vector = encode_float32((1.0, 0.0), dimensions=2)
        task_ids: list[str] = []
        for source_id in ("1", "2"):
            chunk_id = add_named_indexed_item(database, queue, source_id)
            task_id = queue.enqueue(
                "x",
                source_id,
                "embed_content",
                embedding_task_input_hash(active.id, "input"),
            )
            database.connection.execute(
                "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (task_id,)
            )
            task_ids.append(task_id)
            database.connection.execute(
                """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
                   token_start, token_end, vector, created_at)
                   VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
                (chunk_id, active.id, vector),
            )
        original_enqueue = queue.enqueue

        def fail_second(platform: str, source_id: str, kind: str, input_hash: str) -> str:
            assert database.connection.in_transaction
            if source_id == "2":
                raise RuntimeError("injected requeue failure")
            return original_enqueue(platform, source_id, kind, input_hash)

        monkeypatch.setattr(queue, "enqueue", fail_second)
        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())

        with pytest.raises(RuntimeError, match="injected requeue failure"):
            indexer.reindex_missing(force=True)

        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id=?", (active.id,)
            ).fetchone()[0]
            == 2
        )
        statuses = database.connection.execute(
            """SELECT status FROM enrichment_tasks
               WHERE id IN (?, ?) ORDER BY id""",
            tuple(task_ids),
        ).fetchall()
        assert [row["status"] for row in statuses] == ["completed", "completed"]
    finally:
        database.close()


def test_force_invalidation_does_not_reopen_running_embedding_task(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        active = embedding_profile()
        profiles.activate(active)
        chunk_id = add_named_indexed_item(database, queue, "running")
        task_id = queue.enqueue(
            "x",
            "running",
            "embed_content",
            embedding_task_input_hash(active.id, "input"),
        )
        database.connection.execute(
            "UPDATE enrichment_tasks SET status='running' WHERE id=?", (task_id,)
        )
        database.connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (chunk_id, active.id, encode_float32((1.0, 0.0), dimensions=2)),
        )
        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())

        assert indexer.reindex_missing(force=True) == 0

        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id=?", (active.id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            == "running"
        )
    finally:
        database.close()


def test_reindex_missing_requeues_when_one_current_chunk_has_no_vector(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_indexed_item(database, queue)
        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())
        assert indexer.reindex_missing() == 1
        assert indexer.index_next() is not None
        database.connection.execute(
            """INSERT INTO content_chunks(platform, source_id, ordinal, relative_path,
               line_start, line_end, heading, text, input_hash, created_at)
               VALUES ('x', '42', 1, 'content.md', 2, 2, NULL,
                       'missing vector', 'input', '2026-01-01')"""
        )

        assert indexer.reindex_missing() == 1
        task = queue.claim_next(kind="embed_content")
        assert task is not None
    finally:
        database.close()


def test_reindex_missing_detects_one_deleted_segment_from_current_chunk(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_indexed_item(database, queue)
        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())
        assert indexer.reindex_missing() == 1
        assert indexer.index_next() is not None
        rows = database.connection.execute(
            "SELECT segment_ordinal FROM chunk_embeddings ORDER BY segment_ordinal"
        ).fetchall()
        assert [row[0] for row in rows] == [0, 1]
        database.connection.execute("DELETE FROM chunk_embeddings WHERE segment_ordinal=1")

        assert indexer.reindex_missing() == 1
        task = queue.claim_next(kind="embed_content")
        assert task is not None
    finally:
        database.close()


def test_default_segmentation_uses_provider_tokens_for_unspaced_chinese(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = EmbeddingProfile(
            id="profile-chinese",
            provider="fake",
            provider_version="1",
            model="fake-model",
            dimensions=2,
            normalization="l2",
            max_input_tokens=512,
            segment_tokens=480,
            overlap_tokens=32,
            artifact_digest="b" * 64,
        )
        profiles.activate(profile)
        text = "??" * 600
        add_indexed_item(database, queue)
        database.connection.execute(
            "UPDATE content_chunks SET text=? WHERE source_id='42'", (text,)
        )
        states: list[bool] = []

        class ChineseProvider(FakeProvider):
            def tokenize(self, value: str) -> tuple[int, ...]:
                states.append(database.connection.in_transaction)
                return tuple(ord(character) for character in value)

            def decode_tokens(self, tokens: Sequence[int]) -> str:
                return "".join(chr(token) for token in tokens)

            def embed_documents(self, texts: Sequence[str]):
                states.append(database.connection.in_transaction)
                return super().embed_documents(texts)

        indexer = EmbeddingIndexer(database, queue, profiles, provider=ChineseProvider())
        assert indexer.reindex_missing() == 1
        assert indexer.index_next() is not None
        first = [
            tuple(row)
            for row in database.connection.execute(
                """SELECT token_start, token_end FROM chunk_embeddings
                   ORDER BY segment_ordinal"""
            ).fetchall()
        ]
        assert first == [(0, 480), (448, 928), (896, 1200)]
        assert all(end - start <= 480 for start, end in first)
        assert [first[index][1] - first[index + 1][0] for index in range(2)] == [32, 32]
        assert indexer.reindex_missing(force=True) == 1
        assert indexer.index_next() is not None
        second = [
            tuple(row)
            for row in database.connection.execute(
                """SELECT token_start, token_end FROM chunk_embeddings
                   ORDER BY segment_ordinal"""
            ).fetchall()
        ]
        assert second == first
        assert states and not any(states)
    finally:
        database.close()


def test_provider_metadata_must_match_active_profile(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_indexed_item(database, queue)
        task_id = queue.enqueue(
            "x", "42", "embed_content", embedding_task_input_hash(profile.id, "input")
        )

        class WrongProvider(FakeProvider):
            name = "wrong"

        with pytest.raises(ValueError, match="does not match"):
            EmbeddingIndexer(database, queue, profiles, provider=WrongProvider()).index_next()
        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
        )
        assert (
            database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            == "pending"
        )
    finally:
        database.close()


def test_vector_replacement_and_task_completion_share_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        add_indexed_item(database, queue)
        task_id = queue.enqueue(
            "x", "42", "embed_content", embedding_task_input_hash(profile.id, "input")
        )
        states: list[bool] = []
        original_complete = queue.complete

        def record_complete(completed_id: str) -> None:
            states.append(database.connection.in_transaction)
            original_complete(completed_id)

        monkeypatch.setattr(queue, "complete", record_complete)
        result = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider()).index_next()
        assert result is not None and result.vector_count == 2
        assert states == [True]
        assert (
            database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            == "completed"
        )
    finally:
        database.close()


def test_completion_failure_rolls_back_vector_replacement_before_requeue(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    try:
        queue = EnrichmentQueue(database)
        profiles = EmbeddingProfileStore(database)
        profile = embedding_profile()
        profiles.activate(profile)
        chunk_id = add_indexed_item(database, queue)
        old = encode_float32((0.0, 1.0), dimensions=2)
        database.connection.execute(
            """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
               token_start, token_end, vector, created_at)
               VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
            (chunk_id, profile.id, old),
        )
        task_id = queue.enqueue(
            "x", "42", "embed_content", embedding_task_input_hash(profile.id, "input")
        )
        original_complete = queue.complete

        def fail_after_completion(completed_id: str) -> None:
            original_complete(completed_id)
            raise RuntimeError("completion write failed")

        monkeypatch.setattr(queue, "complete", fail_after_completion)
        indexer = EmbeddingIndexer(database, queue, profiles, provider=FakeProvider())
        with pytest.raises(RuntimeError, match="completion write failed"):
            indexer.index_next()

        rows = database.connection.execute(
            """SELECT segment_ordinal, vector FROM chunk_embeddings
               WHERE chunk_id=? ORDER BY segment_ordinal""",
            (chunk_id,),
        ).fetchall()
        assert [tuple(row) for row in rows] == [(0, old)]
        assert (
            database.connection.execute(
                "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
            ).fetchone()[0]
            == "pending"
        )
    finally:
        database.close()
