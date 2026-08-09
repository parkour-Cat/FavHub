from datetime import UTC, datetime
from pathlib import Path

import pytest

from favhub.database import Database
from favhub.domain import CapturedItem
from favhub.embedding import EmbeddingProfile, encode_float32
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.enrichment_queue import EnrichmentQueue
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.retrieval import RetrievalService, SearchRequest


def captured(body: str, source_id: str = "42") -> CapturedItem:
    return CapturedItem(
        platform="x",
        source_id=source_id,
        canonical_url=f"https://example.com/{source_id}",
        title="Title",
        author="Author",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body=body,
        collections=("Research",),
        extractor_version="v1",
    )


@pytest.fixture
def components(tmp_path: Path):
    database = Database.open(tmp_path / "db.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    try:
        yield database, store, queue
    finally:
        database.close()


def register(database, store, item):
    stored = store.write(item)
    index_input_hash = store.index_fingerprint(item.platform, item.source_id)
    database.connection.execute(
        """INSERT INTO items(
               platform,source_id,content_hash,item_dir,published_at,
               first_seen_at,index_input_hash
           ) VALUES (?,?,?,?,?,?,?)""",
        (
            item.platform,
            item.source_id,
            item.content_hash,
            str(stored.directory),
            "2026-01-01T00:00:00Z",
            "2026-07-18T00:00:00Z",
            index_input_hash,
        ),
    )


def test_index_next_completes_task_in_same_transaction_as_chunks(components, monkeypatch):
    database, store, queue = components
    item = captured("# Hello\n\nBody text")
    register(database, store, item)
    task_id = queue.enqueue("x", "42", "index_content", store.index_fingerprint("x", "42"))
    transaction_states: list[bool] = []
    original_complete = queue.complete

    def record_transaction_state(completed_task_id: str) -> None:
        transaction_states.append(database.connection.in_transaction)
        original_complete(completed_task_id)

    monkeypatch.setattr(queue, "complete", record_transaction_state)

    result = ContentIndexer(database, store, queue).index_next()

    assert result is not None and result.task.id == task_id
    rows = database.connection.execute(
        "SELECT text, input_hash FROM content_chunks WHERE platform='x' AND source_id='42'"
    ).fetchall()
    assert any("Body text" in row["text"] for row in rows)
    assert {row["input_hash"] for row in rows} == {store.index_fingerprint("x", "42")}
    assert (
        database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        == "completed"
    )
    assert (
        database.connection.execute(
            """SELECT COUNT(*) FROM content_chunks_fts
           WHERE content_chunks_fts MATCH 'Body'"""
        ).fetchone()[0]
        == 1
    )
    assert transaction_states == [True]


def test_index_completion_enqueues_deterministic_embedding_task_atomically(components, monkeypatch):
    database, store, queue = components
    item = captured("semantic body")
    register(database, store, item)
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
    profiles = EmbeddingProfileStore(database)
    profiles.activate(profile)
    fingerprint = store.index_fingerprint("x", "42")
    queue.enqueue("x", "42", "index_content", fingerprint)
    transaction_states: list[bool] = []
    original_enqueue = queue.enqueue

    def record_enqueue(*args):
        if args[2] == "embed_content":
            transaction_states.append(database.connection.in_transaction)
        return original_enqueue(*args)

    monkeypatch.setattr(queue, "enqueue", record_enqueue)
    ContentIndexer(database, store, queue, profiles).index_next()

    row = database.connection.execute(
        """SELECT input_hash, status FROM enrichment_tasks
           WHERE platform='x' AND source_id='42' AND kind='embed_content'"""
    ).fetchone()
    assert row is not None
    assert row["input_hash"] == embedding_task_input_hash(profile.id, fingerprint)
    assert row["input_hash"] == embedding_task_input_hash("profile-1", fingerprint)
    assert row["status"] == "pending"
    assert transaction_states == [True]


def test_forced_same_fingerprint_reindex_requeues_completed_embedding_task(components):
    database, store, queue = components
    item = captured("same fingerprint semantic body")
    register(database, store, item)
    profile = EmbeddingProfile(
        id="profile-force",
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
    profiles = EmbeddingProfileStore(database)
    profiles.activate(profile)
    fingerprint = store.index_fingerprint("x", "42")
    indexer = ContentIndexer(database, store, queue, profiles)
    queue.enqueue("x", "42", "index_content", fingerprint)
    assert indexer.index_next() is not None
    embed_hash = embedding_task_input_hash(profile.id, fingerprint)
    embed_task = queue.claim_next(kind="embed_content")
    assert embed_task is not None and embed_task.input_hash == embed_hash
    chunk_id = database.connection.execute(
        "SELECT id FROM content_chunks WHERE platform='x' AND source_id='42' LIMIT 1"
    ).fetchone()[0]
    database.connection.execute(
        """INSERT INTO chunk_embeddings(chunk_id, profile_id, segment_ordinal,
           token_start, token_end, vector, created_at)
           VALUES (?, ?, 0, 0, 1, ?, '2026-01-01')""",
        (chunk_id, profile.id, encode_float32((1.0, 0.0), dimensions=2)),
    )
    queue.complete(embed_task.id)

    assert indexer.reindex_missing(force=True) == 1
    assert indexer.index_next() is not None

    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE profile_id=?", (profile.id,)
        ).fetchone()[0]
        == 0
    )
    task_row = database.connection.execute(
        "SELECT status, error FROM enrichment_tasks WHERE id=?", (embed_task.id,)
    ).fetchone()
    assert tuple(task_row) == ("pending", None)
    claimed = queue.claim_next(kind="embed_content")
    assert claimed is not None and claimed.id == embed_task.id


def test_reindex_replaces_chunks_and_failure_preserves_previous(components, monkeypatch):
    database, store, queue = components
    first = captured("first")
    register(database, store, first)
    queue.enqueue("x", "42", "index_content", store.index_fingerprint("x", "42"))
    indexer = ContentIndexer(database, store, queue)
    indexer.index_next()
    second = captured("second")
    store.write(second)
    second_index_hash = store.index_fingerprint("x", "42")
    database.connection.execute(
        """UPDATE items SET content_hash=?, index_input_hash=?
           WHERE platform='x' AND source_id='42'""",
        (second.content_hash, second_index_hash),
    )
    queue.enqueue("x", "42", "index_content", second_index_hash)
    indexer.index_next()
    chunk_snapshot = [
        tuple(row)
        for row in database.connection.execute(
            """SELECT ordinal,relative_path,line_start,line_end,heading,text,input_hash
               FROM content_chunks WHERE platform='x' AND source_id='42'
               ORDER BY ordinal"""
        ).fetchall()
    ]
    fts_snapshot = [
        tuple(row)
        for row in database.connection.execute(
            """SELECT rowid FROM content_chunks_fts
               WHERE content_chunks_fts MATCH 'second' ORDER BY rowid"""
        ).fetchall()
    ]
    assert any("second" in row[5] for row in chunk_snapshot)
    assert all("first" not in row[5] for row in chunk_snapshot)
    assert fts_snapshot

    queue.enqueue("x", "42", "index_content", "broken")
    monkeypatch.setattr(
        store,
        "_safe_read_index_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError, match="boom"):
        indexer.index_next()
    row = database.connection.execute(
        "SELECT status,error FROM enrichment_tasks WHERE input_hash='broken'"
    ).fetchone()
    assert row[0] == "pending" and "boom" in row[1]
    assert [
        tuple(row)
        for row in database.connection.execute(
            """SELECT ordinal,relative_path,line_start,line_end,heading,text,input_hash
               FROM content_chunks WHERE platform='x' AND source_id='42'
               ORDER BY ordinal"""
        ).fetchall()
    ] == chunk_snapshot
    assert [
        tuple(row)
        for row in database.connection.execute(
            """SELECT rowid FROM content_chunks_fts
               WHERE content_chunks_fts MATCH 'second' ORDER BY rowid"""
        ).fetchall()
    ] == fts_snapshot


def test_claim_next_kind_filter_does_not_skip_other_tasks(components):
    _, _, queue = components
    queue.enqueue("x", "1", "enrich_item", "h1")
    index = queue.enqueue("x", "2", "index_content", "h2")
    task = queue.claim_next(kind="index_content")
    assert task is not None and task.id == index
    assert queue.claim_next() is not None and queue.claim_next() is None


def test_notes_are_not_indexed_and_force_requeues_completed_task(components):
    database, store, queue = components
    item = captured("body text")
    register(database, store, item)
    (store.items_root / "x" / "42" / "notes.md").write_text("private secret", encoding="utf-8")
    queue.enqueue("x", "42", "index_content", store.index_fingerprint("x", "42"))
    indexer = ContentIndexer(database, store, queue)
    indexer.index_next()
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks WHERE text LIKE '%private secret%'"
        ).fetchone()[0]
        == 0
    )
    assert indexer.reindex_missing() == 0
    assert indexer.reindex_missing(force=True) == 1
    assert queue.claim_next(kind="index_content") is not None


def test_force_reindex_immediately_invalidates_derived_index_without_writing_facts(components):
    database, store, queue = components
    item = captured("force invalidation marker")
    register(database, store, item)
    indexer = ContentIndexer(database, store, queue)
    fingerprint = store.index_fingerprint("x", "42")
    queue.enqueue("x", "42", "index_content", fingerprint)
    assert indexer.index_next() is not None
    retrieval = RetrievalService(database, store, indexer)
    assert retrieval.search(SearchRequest("invalidation")).found
    item_directory = store.items_root / "x" / "42"
    facts_before = {
        path.relative_to(item_directory).as_posix(): path.read_bytes()
        for path in item_directory.rglob("*")
        if path.is_file()
    }

    assert indexer.reindex_missing(force=True) == 1

    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks WHERE platform='x' AND source_id='42'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks_fts WHERE content_chunks_fts MATCH 'invalidation'"
        ).fetchone()[0]
        == 0
    )
    task = database.connection.execute(
        """SELECT status FROM enrichment_tasks
           WHERE platform='x' AND source_id='42' AND kind='index_content'
             AND input_hash=?""",
        (fingerprint,),
    ).fetchone()
    assert task["status"] == "pending"
    assert {
        path.relative_to(item_directory).as_posix(): path.read_bytes()
        for path in item_directory.rglob("*")
        if path.is_file()
    } == facts_before
    assert not retrieval.search(SearchRequest("invalidation")).found

    assert indexer.index_next() is not None
    assert retrieval.search(SearchRequest("invalidation")).found


def test_running_index_task_can_be_reset_and_retried(components):
    database, store, queue = components
    item = captured("retry me")
    register(database, store, item)
    queue.enqueue("x", "42", "index_content", store.index_fingerprint("x", "42"))
    task = queue.claim_next(kind="index_content")
    assert task is not None
    assert queue.reset_running() == 1
    result = ContentIndexer(database, store, queue).index_next()
    assert result is not None


def test_direct_wrong_kind_is_rejected_without_transition(components):
    database, store, queue = components
    item = captured("body")
    register(database, store, item)
    queue.enqueue("x", "42", "enrich_item", "manual")
    task = queue.claim_next(kind="enrich_item")
    assert task is not None
    with pytest.raises(ValueError, match="unsupported task kind"):
        ContentIndexer(database, store, queue).index_task(task)
    row = database.connection.execute(
        "SELECT status,error FROM enrichment_tasks WHERE id=?", (task.id,)
    ).fetchone()
    assert tuple(row) == ("running", None)


def test_reindex_missing_enqueues_nonempty_item_without_task(components):
    database, store, queue = components
    item = captured("needs indexing")
    register(database, store, item)
    indexer = ContentIndexer(database, store, queue)
    assert indexer.reindex_missing() == 1
    task = queue.claim_next(kind="index_content")
    assert task is not None and task.input_hash == store.index_fingerprint("x", "42")


def test_reindex_missing_does_not_repeat_completed_empty_snapshot(components):
    database, store, queue = components
    item = captured("removed generated content")
    register(database, store, item)
    (store.items_root / "x" / "42" / "content.md").unlink()
    fingerprint = store.index_fingerprint("x", "42")
    task_id = queue.enqueue("x", "42", "index_content", fingerprint)
    database.connection.execute(
        "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (task_id,)
    )
    assert ContentIndexer(database, store, queue).reindex_missing() == 0


@pytest.mark.parametrize("task_status", ["pending", "running"])
def test_force_reindex_invalidates_chunks_without_preempting_queued_task(components, task_status):
    database, store, queue = components
    item = captured("preserved running content")
    register(database, store, item)
    indexer = ContentIndexer(database, store, queue)
    original_fingerprint = store.index_fingerprint("x", "42")
    queue.enqueue("x", "42", "index_content", original_fingerprint)
    assert indexer.index_next() is not None
    (store.items_root / "x" / "42" / "content.md").write_text(
        "replacement running content", encoding="utf-8"
    )
    assert indexer.reindex_missing() == 1
    fingerprint = store.index_fingerprint("x", "42")
    task_id = database.connection.execute(
        "SELECT id FROM enrichment_tasks WHERE input_hash=?", (fingerprint,)
    ).fetchone()[0]
    if task_status == "running":
        running = queue.claim_next(kind="index_content")
        assert running is not None and running.id == task_id
    item_directory = store.items_root / "x" / "42"
    facts_before = {
        path.relative_to(item_directory).as_posix(): path.read_bytes()
        for path in item_directory.rglob("*")
        if path.is_file()
    }

    assert indexer.reindex_missing(force=True) == 0

    assert (
        database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        == task_status
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks WHERE platform='x' AND source_id='42'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks_fts WHERE content_chunks_fts MATCH 'preserved'"
        ).fetchone()[0]
        == 0
    )
    assert {
        path.relative_to(item_directory).as_posix(): path.read_bytes()
        for path in item_directory.rglob("*")
        if path.is_file()
    } == facts_before


def test_allowed_generated_markdown_has_global_ordinals_and_draft_is_excluded(components):
    database, store, queue = components
    item = captured("main body")
    register(database, store, item)
    directory = store.items_root / "x" / "42"
    (directory / "transcript").mkdir()
    (directory / "transcript" / "part.md").write_text("spoken phrase", encoding="utf-8")
    (directory / "draft.md").write_text("draft secret", encoding="utf-8")
    (directory / "assets").rmdir()
    (directory / "Assets").mkdir()
    (directory / "Assets" / "leak.md").write_text("asset secret", encoding="utf-8")
    indexer = ContentIndexer(database, store, queue)
    assert indexer.reindex_missing() == 1
    indexer.index_next()
    rows = database.connection.execute(
        "SELECT ordinal,text FROM content_chunks ORDER BY ordinal"
    ).fetchall()
    assert [row["ordinal"] for row in rows] == list(range(len(rows)))
    aggregate = "\n".join(str(row["text"]) for row in rows)
    assert "spoken phrase" in aggregate
    assert "draft secret" not in aggregate and "asset secret" not in aggregate
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks_fts WHERE content_chunks_fts MATCH 'spoken'"
        ).fetchone()[0]
        == 1
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks_fts WHERE content_chunks_fts MATCH 'secret'"
        ).fetchone()[0]
        == 0
    )


def test_transcript_change_changes_fingerprint_and_requires_reindex(components):
    database, store, queue = components
    item = captured("main body")
    register(database, store, item)
    transcript = store.items_root / "x" / "42" / "transcript.md"
    transcript.write_text("version one", encoding="utf-8")
    first = store.index_fingerprint("x", "42")
    indexer = ContentIndexer(database, store, queue)
    assert indexer.reindex_missing() == 1
    indexer.index_next()
    transcript.write_text("version two", encoding="utf-8")
    second = store.index_fingerprint("x", "42")
    assert second != first
    assert indexer.reindex_missing() == 1


def test_safe_index_read_rejects_identity_change(components, monkeypatch):
    database, store, _ = components
    item = captured("identity")
    register(database, store, item)
    original = store._file_identity
    calls = 0

    def changing_identity(metadata):
        nonlocal calls
        calls += 1
        value = original(metadata)
        return value if calls == 1 else (*value[:-1], value[-1] + 1)

    monkeypatch.setattr(store, "_file_identity", changing_identity)
    with pytest.raises(OSError, match="changed while opening"):
        store.iter_index_markdown("x", "42")


def test_reindex_completed_task_cas_does_not_preempt_concurrent_claim(components, monkeypatch):
    database, store, queue = components
    item = captured("preserved cas content")
    register(database, store, item)
    fingerprint = store.index_fingerprint("x", "42")
    task_id = queue.enqueue("x", "42", "index_content", fingerprint)
    assert ContentIndexer(database, store, queue).index_next() is not None
    original_enqueue = queue.enqueue

    def claim_during_enqueue(*args):
        claimed_id = original_enqueue(*args)
        database.connection.execute(
            "UPDATE enrichment_tasks SET status='running' WHERE id=?", (claimed_id,)
        )
        return claimed_id

    monkeypatch.setattr(queue, "enqueue", claim_during_enqueue)
    item_directory = store.items_root / "x" / "42"
    facts_before = {
        path.relative_to(item_directory).as_posix(): path.read_bytes()
        for path in item_directory.rglob("*")
        if path.is_file()
    }

    assert ContentIndexer(database, store, queue).reindex_missing(force=True) == 0

    assert (
        database.connection.execute(
            "SELECT status FROM enrichment_tasks WHERE id=?", (task_id,)
        ).fetchone()[0]
        == "running"
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks WHERE platform='x' AND source_id='42'"
        ).fetchone()[0]
        == 0
    )
    assert (
        database.connection.execute(
            "SELECT COUNT(*) FROM content_chunks_fts WHERE content_chunks_fts MATCH 'preserved'"
        ).fetchone()[0]
        == 0
    )
    assert {
        path.relative_to(item_directory).as_posix(): path.read_bytes()
        for path in item_directory.rglob("*")
        if path.is_file()
    } == facts_before
    queue.complete(task_id)
