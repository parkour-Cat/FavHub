from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import favhub.retrieval as retrieval_module
from favhub.application import Application
from favhub.database import Database
from favhub.domain import CapturedItem
from favhub.embedding import EmbeddingProfile, encode_float32
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.embedding_runtime import EmbeddingModelCacheMissingError
from favhub.enrichment_queue import EnrichmentQueue
from favhub.hybrid_search import candidate_pool_size
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.retrieval import (
    GetItemRequest,
    ReindexRequest,
    RetrievalMode,
    RetrievalService,
    RetrievalStatus,
    SearchRequest,
    SupportingChunk,
)


class _FakeProvider:
    name = "fake"
    version = "1"
    dimensions = 2

    def __init__(self, query=(1.0, 0.0)):
        self.query = query

    def embed_queries(self, _texts):
        return [self.query]


class _FakeRuntime:
    def __init__(self, provider):
        self.provider = provider

    def load_active(self, *, local_only=True):
        assert local_only
        return self.provider


def captured(
    source_id: str,
    body: str,
    published_at: datetime | None = None,
    *,
    platform: str = "x",
) -> CapturedItem:
    return CapturedItem(
        platform=platform,
        source_id=source_id,
        canonical_url=f"https://example.com/{source_id}",
        title=f"Title {source_id}",
        author="Author",
        published_at=published_at or datetime(2026, 1, 1, tzinfo=UTC),
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
    indexer = ContentIndexer(database, store, queue)
    try:
        yield database, store, queue, indexer, RetrievalService(database, store, indexer)
    finally:
        database.close()


def register(
    database: Database,
    store: ItemStore,
    item: CapturedItem,
    content_type: str = "text",
) -> None:
    stored = store.write(item)
    index_input_hash = store.index_fingerprint(item.platform, item.source_id)
    database.connection.execute(
        """INSERT INTO items(platform,source_id,content_hash,item_dir,published_at,
                              first_seen_at,content_type,index_input_hash)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            item.platform,
            item.source_id,
            item.content_hash,
            str(stored.directory),
            item.published_at.isoformat().replace("+00:00", "Z"),
            "2026-07-18T00:00:00Z",
            content_type,
            index_input_hash,
        ),
    )


def index_item(
    queue: EnrichmentQueue,
    store: ItemStore,
    indexer: ContentIndexer,
    item: CapturedItem,
) -> None:
    fingerprint = store.index_fingerprint(item.platform, item.source_id)
    queue.enqueue(item.platform, item.source_id, "index_content", fingerprint)
    assert indexer.index_next() is not None


def test_search_request_is_frozen():
    request = SearchRequest(query="hello")
    assert request.limit == 10
    with pytest.raises(FrozenInstanceError):
        request.limit = 11  # type: ignore[misc]


def test_supporting_chunk_is_frozen_and_publicly_exported():
    chunk = SupportingChunk("favhub:x/42#chunk-0", "excerpt", "items/x/42/content.md", 1, 2)

    assert "SupportingChunk" in retrieval_module.__all__
    assert not hasattr(chunk, "__dict__")
    with pytest.raises(FrozenInstanceError):
        chunk.excerpt = "replacement"  # type: ignore[misc]


def test_search_skips_unsafe_chunk_path_and_uses_the_next_safe_chunk(components):
    database, store, queue, indexer, retrieval = components
    item = captured(
        "unsafe-chunk",
        "\n\n".join(
            (
                "needle " * 20 + "passage zero " + "detail " * 220,
                "needle passage one " + "detail " * 220,
            )
        ),
    )
    register(database, store, item)
    index_item(queue, store, indexer, item)
    unmodified = retrieval.search(SearchRequest("needle"))
    unsafe_ordinal = int(unmodified.hits[0].citation_id.rsplit("-", maxsplit=1)[1])
    database.connection.execute(
        "UPDATE content_chunks SET relative_path=? WHERE platform=? AND source_id=? AND ordinal=?",
        ("../../outside.md", "x", "unsafe-chunk", unsafe_ordinal),
    )

    result = retrieval.search(SearchRequest("needle"))

    assert result.found
    assert result.hits[0].citation_id != unmodified.hits[0].citation_id


def test_retrieval_status_keeps_four_position_construction_and_reports_index_state() -> None:
    status = RetrievalStatus(1, 2, 3, 4)

    assert status.index_state == "available"
    assert status.as_dict() == {
        "indexed_items": 1,
        "indexed_chunks": 2,
        "pending_index_tasks": 3,
        "failed_index_tasks": 4,
        "index_state": "available",
        # Nothing is known to be gone until something counts it, and the
        # four-position construction this test pins must keep meaning that.
        "unavailable_items": 0,
    }


def test_search_returns_excerpt_metadata_and_stable_citation(components):
    database, store, queue, indexer, retrieval = components
    x_item = captured("42", "Hello searchable world. 中文检索内容。")
    bilibili_item = captured("BV1", "A second searchable result.", platform="bilibili")
    for item in (x_item, bilibili_item):
        register(database, store, item)
        index_item(queue, store, indexer, item)

    english = retrieval.search(SearchRequest("Hello"))
    chinese = retrieval.search(SearchRequest("中文检索内容"))
    second_platform = retrieval.search(SearchRequest("second"))

    assert english.found and english.total_returned == len(english.hits) == 1
    hit = english.hits[0]
    assert (hit.title, hit.author, hit.canonical_url) == (
        "Title 42",
        "Author",
        "https://example.com/42",
    )
    assert "Hello" in hit.excerpt
    assert hit.citation_id.startswith("favhub:x/42#chunk-")
    assert hit.local_path == "items/x/42/content.md"
    assert not Path(hit.local_path).is_absolute()
    assert chinese.found
    assert second_platform.hits[0].platform == "bilibili"
    assert english.index_summary["indexed_items"] == 2


def test_search_returns_one_hit_per_item_with_supporting_chunks(components):
    database, store, queue, indexer, retrieval = components
    item = captured(
        "multi-chunk",
        "\n\n".join(f"needle passage {ordinal} " + "detail " * 220 for ordinal in range(4)),
    )
    register(database, store, item)
    index_item(queue, store, indexer, item)

    result = retrieval.search(SearchRequest("needle", limit=10))

    assert result.found
    assert len(result.hits) == result.total_returned == 1
    hit = result.hits[0]
    assert len(hit.supporting_chunks) <= 3
    citations = (hit.citation_id, *(chunk.citation_id for chunk in hit.supporting_chunks))
    assert len(citations) == len(set(citations))


def test_search_classifies_multi_chunk_item_once(components, monkeypatch):
    database, store, queue, indexer, retrieval = components
    item = captured(
        "multi-chunk-evidence",
        "\n\n".join(f"needle passage {ordinal} " + "detail " * 220 for ordinal in range(4)),
    )
    register(database, store, item)
    index_item(queue, store, indexer, item)
    original = retrieval_module.classify_evidence
    calls = 0

    def count_classifications(source):
        nonlocal calls
        calls += 1
        return original(source)

    monkeypatch.setattr(retrieval_module, "classify_evidence", count_classifications)

    result = retrieval.search(SearchRequest("needle", limit=1))

    assert result.found
    assert calls == 1


def test_title_only_hit_is_marked_and_exact_title_remains_findable(components):
    database, store, queue, indexer, retrieval = components
    item = replace(captured("title-only", ""), title="Seedance exact workflow")
    register(database, store, item)
    index_item(queue, store, indexer, item)

    result = retrieval.search(SearchRequest("Seedance exact workflow"))

    assert result.found
    hit = result.hits[0]
    assert hit.evidence_level == "title_only"
    assert hit.evidence_warning


def test_search_expands_chunk_candidates_before_applying_item_limit(components):
    database, store, queue, indexer, retrieval = components
    dominant = captured(
        "dominant",
        "\n\n".join(f"needle passage {ordinal} " + "detail " * 220 for ordinal in range(4)),
    )
    second = captured("second", "A short body that contains needle.")
    for item in (dominant, second):
        register(database, store, item)
        index_item(queue, store, indexer, item)

    result = retrieval.search(SearchRequest("needle", limit=2))

    assert result.total_returned == 2
    assert {hit.source_id for hit in result.hits} == {"dominant", "second"}


def test_search_progressively_expands_lexical_pool_to_fill_item_limit(components, monkeypatch):
    database, store, queue, indexer, retrieval = components
    dominant = captured(
        "dominant",
        "\n\n".join(
            ("needle " * 10) + f"passage {ordinal} " + "detail " * 220 for ordinal in range(3)
        ),
    )
    second = captured("second", "needle " + "context " * 2_000)
    for item in (dominant, second):
        register(database, store, item)
        index_item(queue, store, indexer, item)
    monkeypatch.setattr(retrieval_module, "candidate_pool_size", lambda _limit: 2)
    traced_sql: list[str] = []
    database.connection.set_trace_callback(traced_sql.append)

    try:
        result = retrieval.search(SearchRequest("needle", limit=2))
    finally:
        database.connection.set_trace_callback(None)

    assert result.total_returned == 2
    assert {hit.source_id for hit in result.hits} == {"dominant", "second"}
    normalized = [" ".join(statement.split()) for statement in traced_sql]
    assert any("LIMIT 2 OFFSET 2" in statement for statement in normalized)


def test_chinese_substring_queries_match_via_fts(components):
    database, store, queue, indexer, retrieval = components
    item = captured("cjk-1", "深度学习框架对比评测，覆盖训练与推理两个阶段。")
    register(database, store, item)
    index_item(queue, store, indexer, item)

    for query in ("学习框架", "框架", "对比评测", "训练", "golang 框架"):
        result = retrieval.search(SearchRequest(query))
        assert result.found is True, query
        top = result.hits[0]
        assert top.source_id == "cjk-1", query
        assert "fts" in top.match_sources, query


def test_mixed_ascii_cjk_token_matches_via_fts(components):
    database, store, queue, indexer, retrieval = components
    item = captured("cjk-2", "推荐几个golang练手项目，从爬虫到贪吃蛇。")
    register(database, store, item)
    index_item(queue, store, indexer, item)

    result = retrieval.search(SearchRequest("golang练手"))
    assert result.found is True
    assert result.hits[0].source_id == "cjk-2"
    assert "fts" in result.hits[0].match_sources


def test_reopening_backfills_fts_text_for_existing_chunks(tmp_path: Path):
    database = Database.open(tmp_path / "db.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    indexer = ContentIndexer(database, store, queue)
    item = captured("cjk-3", "分布式系统一致性协议详解")
    register(database, store, item)
    fingerprint = store.index_fingerprint(item.platform, item.source_id)
    queue.enqueue(item.platform, item.source_id, "index_content", fingerprint)
    assert indexer.index_next() is not None
    # Simulate rows written before the bigram column existed.
    database.connection.execute("UPDATE content_chunks SET fts_text = NULL")
    database.close()

    reopened = Database.open(tmp_path / "db.sqlite3")
    try:
        retrieval = RetrievalService(reopened, store, ContentIndexer(reopened, store, queue))
        result = retrieval.search(SearchRequest("一致性协议"))
        assert result.found is True
        assert "fts" in result.hits[0].match_sources
    finally:
        reopened.close()


def test_search_validates_query_limit_and_date_strings(components):
    *_, retrieval = components
    with pytest.raises(ValueError, match="query"):
        retrieval.search(SearchRequest("  "))
    for limit in (0, 51, True):
        with pytest.raises(ValueError, match="limit"):
            retrieval.search(SearchRequest("hello", limit=limit))
    for value in ("not-a-date", "2026-01-01T00:00:00"):
        with pytest.raises(ValueError, match="date filters"):
            retrieval.search(SearchRequest("hello", published_since=value))
    with pytest.raises(ValueError, match="published_since"):
        retrieval.search(
            SearchRequest(
                "hello",
                published_since="2026-02-01T00:00:00Z",
                published_until="2026-01-01T00:00:00Z",
            )
        )
    with pytest.raises(ValueError, match="published_since"):
        retrieval.search(
            SearchRequest(
                "hello",
                published_since="2026-01-01T00:00:00.500Z",
                published_until="2026-01-01T00:00:00Z",
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("platforms", ()),
        ("platforms", ["x"]),
        ("platforms", ("",)),
        ("platforms", (None,)),
        ("content_types", ()),
        ("content_types", "text"),
        ("content_types", ("",)),
        ("content_types", (1,)),
    ],
)
def test_search_rejects_empty_or_wrongly_typed_filters(components, field, value):
    *_, retrieval = components
    with pytest.raises(ValueError, match=field):
        retrieval.search(SearchRequest("hello", **{field: value}))


def test_special_fts_input_is_safe_and_punctuation_has_explainable_result(components):
    database, store, queue, indexer, retrieval = components
    item = captured("42", "hello")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    assert retrieval.search(SearchRequest("hello\" OR *') --")).found
    result = retrieval.search(SearchRequest("*** \"' --"))
    assert not result.found and "no searchable tokens" in (result.reason or "")


def test_search_filters_platform_content_type_and_dates(components):
    database, store, queue, indexer, retrieval = components
    video = captured("42", "needle alpha", datetime(2026, 2, 1, tzinfo=UTC))
    later_video = captured("43", "needle beta", datetime(2026, 2, 2, tzinfo=UTC))
    text = captured("BV1", "needle gamma", datetime(2026, 2, 1, tzinfo=UTC), platform="bilibili")
    for item, content_type in ((video, "video"), (later_video, "video"), (text, "text")):
        register(database, store, item, content_type=content_type)
        index_item(queue, store, indexer, item)

    filtered = retrieval.search(
        SearchRequest(
            "needle",
            platforms=("x",),
            content_types=("video",),
            published_since="2026-02-01T08:00:00+08:00",
            published_until="2026-03-01T00:00:00+00:00",
            limit=1,
        )
    )
    assert filtered.found and filtered.total_returned == len(filtered.hits) == 1
    assert filtered.hits[0].platform == "x"
    assert filtered.hits[0].content_type == "video"
    assert not retrieval.search(SearchRequest("needle", content_types=("article",))).found
    assert retrieval.search(SearchRequest("needle", platforms=("bilibili",))).found


def test_no_result_reports_pending_and_failed_index_work(components):
    database, store, queue, _, retrieval = components
    first = captured("pending", "one")
    second = captured("failed", "two")
    register(database, store, first)
    register(database, store, second)
    failed_id = queue.enqueue(
        "x", "failed", "index_content", store.index_fingerprint("x", "failed")
    )
    failed_task = queue.claim_next(kind="index_content")
    assert failed_task is not None and failed_task.id == failed_id
    queue.fail(failed_id, "boom")
    queue.enqueue("x", "pending", "index_content", store.index_fingerprint("x", "pending"))

    result = retrieval.search(SearchRequest("absent"))

    assert not result.found and "pending or failed" in (result.reason or "")
    assert result.not_found_reason == result.reason
    assert result.total_returned == 0
    assert result.index_summary["pending_index_tasks"] == 2
    assert result.index_summary["failed_index_tasks"] == 1


def test_status_counts_current_completed_empty_item_but_not_stale_task(components):
    database, store, queue, _, retrieval = components
    current = captured("empty", "removed")
    stale = captured("stale", "current content")
    register(database, store, current)
    register(database, store, stale)
    (store.items_root / "x" / "empty" / "content.md").unlink()
    empty_hash = store.index_fingerprint("x", "empty")
    database.connection.execute(
        "UPDATE items SET index_input_hash=? WHERE platform='x' AND source_id='empty'",
        (empty_hash,),
    )
    empty_id = queue.enqueue("x", "empty", "index_content", empty_hash)
    stale_id = queue.enqueue("x", "stale", "index_content", "old-fingerprint")
    database.connection.execute(
        "UPDATE enrichment_tasks SET status='completed' WHERE id IN (?,?)", (empty_id, stale_id)
    )

    status = retrieval.status()

    assert status.indexed_items == 1
    assert status.indexed_chunks == 0
    assert status.pending_index_tasks == 0


def test_status_reports_durable_counts_when_fts_is_unavailable(components):
    database, store, queue, indexer, retrieval = components
    item = captured("42", "durable searchable content")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    expected_chunks = database.connection.execute("SELECT COUNT(*) FROM content_chunks").fetchone()[
        0
    ]
    database.connection.execute("DROP TABLE content_chunks_fts")

    status = retrieval.status()

    assert status.indexed_items == 1
    assert status.indexed_chunks == expected_chunks
    assert status.pending_index_tasks == 0
    assert status.failed_index_tasks == 0
    assert status.index_state == "index_unavailable"


def test_search_fails_stably_before_querying_unavailable_fts(components):
    database, *_, retrieval = components
    database.connection.execute("DROP TABLE content_chunks_fts")

    with pytest.raises(RuntimeError, match="^index_unavailable$"):
        retrieval.search(SearchRequest("anything"))


def test_search_and_get_item_reject_registered_directory_outside_store(components, tmp_path):
    database, store, queue, indexer, retrieval = components
    item = captured("42", "unsafe directory needle")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    database.connection.execute(
        "UPDATE items SET item_dir=? WHERE platform=? AND source_id=?",
        (str(tmp_path.parent / "outside"), "x", "42"),
    )

    result = retrieval.search(SearchRequest("needle"))

    assert not result.found
    with pytest.raises(ValueError, match="registered item directory"):
        retrieval.get_item(GetItemRequest("x", "42"))


def test_search_ignores_stale_chunks_until_current_index_completes(components):
    database, store, queue, indexer, retrieval = components
    item = captured("42", "original needle")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    assert retrieval.search(SearchRequest("original")).found

    content_path = store.items_root / "x" / "42" / "content.md"
    content_path.write_text("replacement token\n", encoding="utf-8")
    assert indexer.reindex_missing() == 1

    assert not retrieval.search(SearchRequest("original")).found
    assert not retrieval.search(SearchRequest("replacement")).found
    assert indexer.index_next() is not None
    assert retrieval.search(SearchRequest("replacement")).found


def test_search_filters_stale_rows_before_applying_limit(components):
    database, store, queue, indexer, retrieval = components
    stale = captured("stale", "needle stale")
    current = captured("current", "needle current")
    for item in (stale, current):
        register(database, store, item)
        index_item(queue, store, indexer, item)
    content_path = store.items_root / "x" / "stale" / "content.md"
    content_path.write_text("changed content\n", encoding="utf-8")
    assert indexer.reindex_missing() == 1
    traced_sql: list[str] = []
    database.connection.set_trace_callback(traced_sql.append)

    try:
        result = retrieval.search(SearchRequest("needle", limit=1))
    finally:
        database.connection.set_trace_callback(None)

    assert result.found and result.total_returned == 1
    assert result.hits[0].source_id == "current"
    assert any("LIMIT 1" in " ".join(statement.split()) for statement in traced_sql)


def test_search_fetches_candidate_pool_past_broken_top_result_to_fill_item_limit(components):
    database, store, queue, indexer, retrieval = components
    broken = captured(
        "bad",
        "needle",
        datetime(2026, 2, 2, tzinfo=UTC),
    )
    valid = captured(
        "good",
        "needle valid fallback with additional context",
        datetime(2026, 2, 1, tzinfo=UTC),
    )
    for item in (broken, valid):
        register(database, store, item)
        index_item(queue, store, indexer, item)
    (store.items_root / "x" / "bad" / "source.json").unlink()
    traced_sql: list[str] = []
    database.connection.set_trace_callback(traced_sql.append)

    try:
        result = retrieval.search(SearchRequest("needle", limit=1))
    finally:
        database.connection.set_trace_callback(None)

    assert result.found and result.total_returned == 1
    assert result.hits[0].source_id == "good"
    normalized = [" ".join(statement.split()) for statement in traced_sql]
    assert any(f"LIMIT {candidate_pool_size(1)} OFFSET 0" in statement for statement in normalized)


def test_get_item_returns_safe_file_manifest_and_only_system_content(components):
    database, store, _, indexer, retrieval = components
    item = captured("42", "safe body")
    register(database, store, item)
    directory = store.items_root / "x" / "42"
    (directory / "cookies.txt").write_text("secret", encoding="utf-8")

    response = retrieval.get_item(GetItemRequest("x", "42"))

    assert response.source["source_id"] == "42"
    assert {"source.json", "content.md", "notes.md"} <= set(response.files)
    assert "cookies.txt" not in response.files
    assert "content.md" in response.system_content
    assert "notes.md" not in response.system_content
    assert response.access_status == "available"
    assert response.content_type == "text"
    database.connection.execute(
        "UPDATE items SET access_status='restricted' WHERE platform='x' AND source_id='42'"
    )
    restricted = retrieval.get_item(GetItemRequest("x", "42"))
    assert restricted.access_status == "restricted"
    without_content = retrieval.get_item(GetItemRequest("x", "42", include_content=False))
    assert without_content.files == response.files
    assert without_content.system_content == {}
    with pytest.raises(KeyError, match="not found"):
        retrieval.get_item(GetItemRequest("x", "missing"))
    with pytest.raises(ValueError, match="include_content"):
        retrieval.get_item(GetItemRequest("x", "42", include_content=1))


def test_reindex_delegates_force_to_indexer(components, monkeypatch):
    *_, indexer, retrieval = components
    calls: list[bool] = []
    monkeypatch.setattr(indexer, "reindex_missing", lambda force=False: calls.append(force) or 3)
    response = retrieval.reindex(ReindexRequest(force=True))
    assert response.enqueued == 3 and calls == [True]


def test_reindex_repairs_unavailable_fts_from_existing_chunks(components):
    database, store, queue, indexer, retrieval = components
    item = captured("42", "recoverable indexed text")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    database.connection.execute("DROP TABLE content_chunks_fts")

    response = retrieval.reindex(ReindexRequest())

    assert response.enqueued == 0
    assert retrieval.status().index_state == "available"
    assert retrieval.search(SearchRequest("recoverable")).found


def test_application_open_wires_indexer_and_retrieval(tmp_path: Path):
    with Application.open(tmp_path / "data") as application:
        assert application.indexer is not None
        assert application.retrieval is not None
        assert application.indexer.database is application.database
        assert application.indexer.store is application.store
        assert application.indexer.queue is application.queue
        assert application.retrieval.database is application.database
        assert application.retrieval.store is application.store
        assert application.retrieval.indexer is application.indexer


def test_hybrid_vector_candidates_share_fts_metadata_filters(components):
    database, store, queue, indexer, _ = components
    first = captured("first", "lexical one")
    second = captured("second", "lexical two", platform="bilibili")
    for item in (first, second):
        register(database, store, item)
        index_item(queue, store, indexer, item)
    profiles = EmbeddingProfileStore(database)
    profile = EmbeddingProfile(
        "f" * 64,
        "fake",
        "1",
        "fake-model",
        2,
        "l2",
        32,
        16,
        2,
        "a" * 64,
    )
    profiles.activate(profile)
    for item, vector in ((first, (1.0, 0.0)), (second, (0.0, 1.0))):
        chunk_id = database.connection.execute(
            "SELECT id FROM content_chunks WHERE platform=? AND source_id=?",
            (item.platform, item.source_id),
        ).fetchone()[0]
        input_hash = store.index_fingerprint(item.platform, item.source_id)
        task_id = queue.enqueue(
            item.platform,
            item.source_id,
            "embed_content",
            embedding_task_input_hash(profile.id, input_hash),
        )
        database.connection.execute(
            "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (task_id,)
        )
        database.connection.execute(
            "INSERT INTO chunk_embeddings("
            "chunk_id,profile_id,segment_ordinal,token_start,token_end,vector,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                chunk_id,
                profile.id,
                0,
                0,
                2,
                encode_float32(vector, dimensions=2),
                "2026-07-18T00:00:00Z",
            ),
        )
    retrieval = RetrievalService(
        database,
        store,
        indexer,
        embedding_profiles=profiles,
        embedding_runtime=_FakeRuntime(_FakeProvider()),
    )
    result = retrieval.search(SearchRequest("semantic", platforms=("x",)))
    assert result.found and result.retrieval_mode == "hybrid"
    assert [hit.platform for hit in result.hits] == ["x"]
    assert result.hits[0].match_sources == ("vector",)


def test_hybrid_query_provider_failure_falls_back_to_fts(components):
    database, store, queue, indexer, _ = components
    item = captured("one", "query fallback")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    profiles = EmbeddingProfileStore(database)
    profiles.activate(
        EmbeddingProfile(
            "e" * 64,
            "fake",
            "1",
            "fake-model",
            2,
            "l2",
            32,
            16,
            2,
            "b" * 64,
        )
    )

    class BrokenProvider(_FakeProvider):
        def embed_queries(self, _texts):
            raise RuntimeError("boom")

    retrieval = RetrievalService(
        database,
        store,
        indexer,
        embedding_profiles=profiles,
        embedding_runtime=_FakeRuntime(BrokenProvider()),
    )
    result = retrieval.search(SearchRequest("query"))
    assert result.found and result.retrieval_mode == "fts"
    assert result.vector_warning == "query_embedding_failed"

    with pytest.raises(RuntimeError, match="^hybrid retrieval is unavailable$"):
        retrieval.search(SearchRequest("query"), mode=RetrievalMode.HYBRID)


def test_fts_mode_never_loads_embedding_provider(components):
    database, store, queue, indexer, _ = components
    item = captured("fts", "lexical only retrieval")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    profiles = EmbeddingProfileStore(database)
    profiles.activate(
        EmbeddingProfile(
            "a" * 64,
            "fake",
            "1",
            "fake-model",
            2,
            "l2",
            32,
            16,
            2,
            "b" * 64,
        )
    )

    class ExplodingRuntime:
        def load_active(self, *, local_only=True):
            raise AssertionError("FTS mode must not load an embedding provider")

    retrieval = RetrievalService(
        database,
        store,
        indexer,
        embedding_profiles=profiles,
        embedding_runtime=ExplodingRuntime(),
    )

    with pytest.raises(AssertionError, match="^FTS mode must not load an embedding provider$"):
        retrieval.search(SearchRequest("lexical"), mode=RetrievalMode.HYBRID)

    result = retrieval.search(SearchRequest("lexical"), mode=RetrievalMode.FTS)

    assert result.found
    assert result.retrieval_mode == "fts"


def test_strict_hybrid_mode_rejects_missing_profile(components):
    *_, retrieval = components

    with pytest.raises(RuntimeError, match="^hybrid retrieval is unavailable$"):
        retrieval.search(SearchRequest("anything"), mode=RetrievalMode.HYBRID)


def test_search_rejects_invalid_retrieval_mode(components):
    *_, retrieval = components

    with pytest.raises(ValueError, match="^retrieval mode must be auto, fts, or hybrid$"):
        retrieval.search(SearchRequest("anything"), mode="semantic")


def test_hybrid_semantic_pool_collapses_segments_before_pool_limit(components):
    database, store, queue, indexer, _ = components
    first = captured("first", "segment alpha")
    second = captured("second", "segment beta")
    for item in (first, second):
        register(database, store, item)
        index_item(queue, store, indexer, item)
    profiles = EmbeddingProfileStore(database)
    profile = EmbeddingProfile("a" * 64, "fake", "1", "fake-model", 2, "l2", 32, 16, 2, "c" * 64)
    profiles.activate(profile)
    for item, segment_count in ((first, 50), (second, 1)):
        chunk_id = database.connection.execute(
            "SELECT id FROM content_chunks WHERE platform=? AND source_id=?",
            (item.platform, item.source_id),
        ).fetchone()[0]
        input_hash = store.index_fingerprint(item.platform, item.source_id)
        task_id = queue.enqueue(
            item.platform,
            item.source_id,
            "embed_content",
            embedding_task_input_hash(profile.id, input_hash),
        )
        database.connection.execute(
            "UPDATE enrichment_tasks SET status='completed' WHERE id=?", (task_id,)
        )
        for ordinal in range(segment_count):
            score_vector = (1.0, 0.0) if item is first else (0.99, 0.1410673598)
            database.connection.execute(
                "INSERT INTO chunk_embeddings("
                "chunk_id,profile_id,segment_ordinal,token_start,token_end,vector,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    chunk_id,
                    profile.id,
                    ordinal,
                    ordinal,
                    ordinal + 1,
                    encode_float32(score_vector, dimensions=2),
                    "2026-07-18T00:00:00Z",
                ),
            )
    retrieval = RetrievalService(
        database,
        store,
        indexer,
        embedding_profiles=profiles,
        embedding_runtime=_FakeRuntime(_FakeProvider()),
    )
    result = retrieval.search(SearchRequest("semantic", limit=1))
    assert result.found and result.hits[0].source_id == "first"
    # The second chunk remains in the 50-chunk semantic pool even though its
    # segment row is encountered after the first chunk's 50 segments.
    expanded = retrieval.search(SearchRequest("semantic", limit=2))
    assert {hit.source_id for hit in expanded.hits} == {"first", "second"}


def test_hybrid_cache_missing_reports_unavailable_summary(components):
    database, store, queue, indexer, _ = components
    item = captured("cache", "cache fallback")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    profiles = EmbeddingProfileStore(database)
    profiles.activate(
        EmbeddingProfile("d" * 64, "fake", "1", "fake-model", 2, "l2", 32, 16, 2, "e" * 64)
    )

    class MissingRuntime:
        def load_active(self, *, local_only=True):
            raise EmbeddingModelCacheMissingError("missing")

    retrieval = RetrievalService(
        database,
        store,
        indexer,
        embedding_profiles=profiles,
        embedding_runtime=MissingRuntime(),
    )
    result = retrieval.search(SearchRequest("cache"))
    assert result.found and result.vector_warning == "embedding_unavailable"
    assert result.embedding_summary["state"] == "unavailable"
    punctuation = retrieval.search(SearchRequest("***"))
    assert "embedding_unavailable" in (punctuation.reason or "")
    with pytest.raises(RuntimeError, match="^hybrid retrieval is unavailable$"):
        retrieval.search(SearchRequest("cache"), mode=RetrievalMode.HYBRID)


def test_search_filters_by_favorited_window_and_excludes_null(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    library = LibraryModule(database, store, queue)
    indexer = ContentIndexer(database, store, queue)
    retrieval = RetrievalService(database, store, indexer)
    timestamp = "2026-07-01T00:00:00Z"
    database.connection.execute(
        """INSERT INTO sync_jobs(id, mode, status, options_json, created_at, updated_at)
           VALUES ('fav-job', 'full', 'running', '{}', ?, ?)""",
        (timestamp, timestamp),
    )

    def make(source_id: str, favorited_at: str | None) -> CapturedItem:
        metadata = {"favorited_at": favorited_at} if favorited_at else None
        return CapturedItem(
            platform="bilibili",
            source_id=source_id,
            canonical_url=f"https://www.bilibili.com/video/{source_id}",
            title=f"收藏窗口测试 {source_id}",
            author=None,
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
            body="收藏窗口测试正文",
            collections=(),
            extractor_version="v1",
            platform_metadata=metadata,
        )

    library.ingest_batch(
        "fav-job",
        "bilibili",
        "b1",
        [
            make("BV1FAVWINA01", "2026-03-01T00:00:00Z"),
            make("BV1FAVWINB01", "2026-06-15T00:00:00Z"),
            make("BV1FAVWINC01", None),
        ],
        True,
    )
    while indexer.index_next() is not None:
        pass

    everything = retrieval.search(SearchRequest("收藏窗口测试", limit=10))
    assert {hit.source_id for hit in everything.hits} == {
        "BV1FAVWINA01",
        "BV1FAVWINB01",
        "BV1FAVWINC01",
    }

    windowed = retrieval.search(
        SearchRequest(
            "收藏窗口测试",
            favorited_since=datetime(2026, 6, 1, tzinfo=UTC),
            favorited_until=datetime(2026, 6, 30, tzinfo=UTC),
            limit=10,
        )
    )
    assert {hit.source_id for hit in windowed.hits} == {"BV1FAVWINB01"}

    since_only = retrieval.search(
        SearchRequest("收藏窗口测试", favorited_since=datetime(2026, 1, 1, tzinfo=UTC), limit=10)
    )
    # NULL favorited_at is excluded whenever the filter is active.
    assert {hit.source_id for hit in since_only.hits} == {"BV1FAVWINA01", "BV1FAVWINB01"}

    with pytest.raises(ValueError, match="favorited_since"):
        retrieval.search(
            SearchRequest(
                "收藏窗口测试",
                favorited_since=datetime(2026, 7, 1, tzinfo=UTC),
                favorited_until=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
    database.close()


def _place_in_collection(database: Database, item: CapturedItem, *names: str) -> None:
    database.connection.executemany(
        "INSERT INTO item_collections(platform, source_id, name) VALUES (?,?,?)",
        [(item.platform, item.source_id, name) for name in names],
    )


def test_search_can_be_restricted_to_the_users_own_folders(components):
    database, store, queue, indexer, retrieval = components
    deliberate = captured("in-nlp", "needle about retrieval")
    one_click = captured("in-default", "needle about retrieval")
    for item in (deliberate, one_click):
        register(database, store, item)
        index_item(queue, store, indexer, item)
    _place_in_collection(database, deliberate, "NLP")
    _place_in_collection(database, one_click, "默认收藏夹")

    scoped = retrieval.search(SearchRequest("needle", collections=("NLP",)))

    assert [hit.source_id for hit in scoped.hits] == ["in-nlp"]
    # Without the filter both are equally good matches; the folder is the only
    # thing that separates a deliberate save from a reflex.
    assert len(retrieval.search(SearchRequest("needle")).hits) == 2


def test_an_item_in_several_folders_matches_any_of_them(components):
    database, store, queue, indexer, retrieval = components
    item = captured("multi", "needle about retrieval")
    register(database, store, item)
    index_item(queue, store, indexer, item)
    _place_in_collection(database, item, "NLP", "考研")

    for folder in ("NLP", "考研"):
        assert [
            hit.source_id
            for hit in retrieval.search(SearchRequest("needle", collections=(folder,))).hits
        ] == ["multi"]
    assert retrieval.search(SearchRequest("needle", collections=("钢琴",))).hits == ()


def test_the_folder_map_counts_live_items_largest_first(components):
    database, store, _queue, _indexer, retrieval = components
    for source_id in ("a", "b", "c"):
        item = captured(source_id, "body", platform="bilibili")
        register(database, store, item)
        _place_in_collection(database, item, "默认收藏夹")
    piano = captured("piano", "body", platform="bilibili")
    register(database, store, piano)
    _place_in_collection(database, piano, "钢琴")

    folders = retrieval.collections().collections

    assert [(folder.name, folder.items) for folder in folders] == [("默认收藏夹", 3), ("钢琴", 1)]


def test_a_folder_stops_counting_an_item_the_platform_dropped(components):
    database, store, _queue, _indexer, retrieval = components
    live = captured("live", "body", platform="bilibili")
    gone = captured("gone", "body", platform="bilibili")
    for item in (live, gone):
        register(database, store, item)
        _place_in_collection(database, item, "钢琴")
    database.connection.execute(
        "UPDATE items SET access_status='unavailable' WHERE source_id='gone'"
    )

    # The folder still lists it upstream, but a count that includes items
    # nobody can open would overstate what is actually readable here.
    assert [(folder.name, folder.items) for folder in retrieval.collections().collections] == [
        ("钢琴", 1)
    ]


def test_the_map_names_the_platforms_no_folder_describes(components):
    database, store, _queue, _indexer, retrieval = components
    filed = captured("filed", "body", platform="bilibili")
    register(database, store, filed)
    _place_in_collection(database, filed, "钢琴")
    for source_id in ("repo-1", "repo-2"):
        register(database, store, captured(source_id, "body", platform="github"))

    coverage = {row.platform: row for row in retrieval.collections().platforms}

    # Stars are a flat list: no folder name will ever hint at what they hold,
    # so reading the folders as the whole library would hide them exactly when
    # they matter most.
    assert (coverage["github"].items, coverage["github"].unfiled) == (2, 2)
    assert (coverage["bilibili"].items, coverage["bilibili"].unfiled) == (1, 0)


def test_a_blank_collection_filter_is_refused(components):
    _database, _store, _queue, _indexer, retrieval = components
    with pytest.raises(ValueError):
        retrieval.search(SearchRequest("needle", collections=()))
    with pytest.raises(ValueError):
        retrieval.search(SearchRequest("needle", collections=("  ",)))
