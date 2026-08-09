"""M2B retrieval diagnostics exposed through the MCP adapter."""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from favhub.application import Application
from favhub.cli import main as cli_main
from favhub.config import FavHubPaths
from favhub.database import Database
from favhub.domain import CapturedItem
from favhub.embedding import EmbeddingProfile
from favhub.embedding_indexing import EmbeddingIndexer
from favhub.embedding_profiles import EmbeddingProfileStore, embedding_task_input_hash
from favhub.enrichment_queue import EnrichmentQueue
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.mcp_server import run_stdio
from favhub.retrieval import (
    RetrievalService,
    RetrievalStatus,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SupportingChunk,
)


class _HybridStub:
    embedding_profiles: Any = None

    def search(self, _request: Any) -> SearchResponse:
        return SearchResponse(
            found=True,
            hits=(
                SearchHit(
                    platform="x",
                    source_id="semantic",
                    title="Semantic result",
                    author=None,
                    published_at="2026-01-01T00:00:00Z",
                    content_type="text",
                    excerpt="semantic",
                    canonical_url="https://example.com/semantic",
                    local_path="items/x/semantic/content.md",
                    line_start=1,
                    line_end=1,
                    citation_id="favhub:x/semantic#chunk-0",
                    match_sources=("vector",),
                    cosine_similarity=0.9,
                    rrf_score=1 / 61,
                    evidence_level="transcript",
                    supporting_chunks=(
                        SupportingChunk(
                            citation_id="favhub:x/semantic#chunk-1",
                            excerpt="supporting passage",
                            local_path="items/x/semantic/content.md",
                            line_start=2,
                            line_end=4,
                        ),
                    ),
                ),
            ),
            total_returned=1,
            retrieval_mode="hybrid",
            vector_warning=None,
            embedding_summary={"state": "ready", "embedded_chunks": 1},
        )

    def get_item(self, request: Any) -> Any:
        raise KeyError(request.source_id)

    def status(self) -> RetrievalStatus:
        return RetrievalStatus(1, 1, 0, 0)


class _StatusProfilesStub:
    def summary(self) -> SimpleNamespace:
        return SimpleNamespace(
            state="ready",
            active_profile_metadata={"id": "profile-1"},
            current_chunks=1,
            embedded_chunks=1,
            pending_tasks=0,
            failed_tasks=0,
            corrupt_vectors=0,
            last_build_report={"run_id": "run-1", "status": "completed"},
        )


class _StatusStub(_HybridStub):
    embedding_profiles = _StatusProfilesStub()


class _CliApplicationStub:
    def __init__(self, retrieval: RetrievalService) -> None:
        self.retrieval = retrieval

    def __enter__(self) -> _CliApplicationStub:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_mcp_search_preserves_hybrid_diagnostics_and_tool_surface() -> None:
    requests = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "favhub.search",
                        "arguments": {"query": "semantic"},
                    },
                }
            ),
        ]
    )
    output = io.StringIO()
    run_stdio(_HybridStub(), io.StringIO(requests), output, io.StringIO())
    response = json.loads(output.getvalue().splitlines()[-1])
    structured = response["result"]["structuredContent"]
    assert structured["retrieval_mode"] == "hybrid"
    assert structured["embedding_summary"]["state"] == "ready"
    assert structured["hits"][0]["match_sources"] == ["vector"]
    assert structured["hits"][0]["cosine_similarity"] == 0.9
    assert structured["hits"][0]["evidence_level"] == "transcript"
    assert structured["hits"][0]["evidence_warning"] is None
    assert structured["hits"][0]["supporting_chunks"] == [
        {
            "citation_id": "favhub:x/semantic#chunk-1",
            "excerpt": "supporting passage",
            "local_path": "items/x/semantic/content.md",
            "line_start": 2,
            "line_end": 4,
            "timestamp": None,
        }
    ]


def test_mcp_status_preserves_last_embedding_build_report() -> None:
    requests = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {},
                    },
                }
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "favhub.status", "arguments": {}},
                }
            ),
        ]
    )
    output = io.StringIO()
    run_stdio(_StatusStub(), io.StringIO(requests), output, io.StringIO())

    structured = json.loads(output.getvalue().splitlines()[-1])["result"]["structuredContent"]
    assert structured["embedding_summary"]["last_build_report"] == {
        "run_id": "run-1",
        "status": "completed",
    }


class _IntegrationProvider:
    name = "fake"
    version = "1"
    dimensions = 2

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, float], ...]:
        return tuple((1.0, 0.0) if "中文" in text else (0.0, 1.0) for text in texts)

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, float], ...]:
        return self.embed_documents(texts)

    def embed_queries(self, texts: Sequence[str]) -> tuple[tuple[float, float], ...]:
        return self.embed_documents(texts)

    def tokenize(self, text: str) -> tuple[int, ...]:
        return tuple(range(len(text.split())))

    def decode_tokens(self, tokens: Sequence[int]) -> str:
        return " ".join(f"token-{token}" for token in tokens)


class _IntegrationRuntime:
    def __init__(self, provider: _IntegrationProvider) -> None:
        self.provider = provider

    def load_active(self, *, local_only: bool = True) -> _IntegrationProvider:
        assert local_only
        return self.provider


def test_fake_provider_import_index_embed_mcp_order_partial_and_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = Database.open(tmp_path / "db.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    indexer = ContentIndexer(database, store, queue)
    profiles = EmbeddingProfileStore(database)
    provider = _IntegrationProvider()
    runtime = _IntegrationRuntime(provider)
    items = (
        ("english", "A code example uses Python."),
        ("chinese", "中文检索内容和术语。"),
        ("subtitle", "subtitle: hello world"),
        ("ocr", "OCR placeholder text"),
    )
    try:
        for source_id, body in items:
            item = CapturedItem(
                platform="x",
                source_id=source_id,
                canonical_url=f"https://example.com/{source_id}",
                title=source_id,
                author=None,
                published_at=datetime(2026, 1, 1, tzinfo=UTC),
                observed_at=datetime(2026, 1, 2, tzinfo=UTC),
                body=body,
                collections=(),
                extractor_version="test",
            )
            stored = store.write(item)
            index_hash = store.index_fingerprint("x", source_id)
            database.connection.execute(
                "INSERT INTO items("
                "platform,source_id,content_hash,item_dir,published_at,first_seen_at,"
                "index_input_hash) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    "x",
                    source_id,
                    item.content_hash,
                    str(stored.directory),
                    item.published_at.isoformat(),
                    item.observed_at.isoformat(),
                    index_hash,
                ),
            )
            queue.enqueue("x", source_id, "index_content", index_hash)
            assert indexer.index_next() is not None
        profile = EmbeddingProfile("1" * 64, "fake", "1", "fake", 2, "l2", 32, 16, 2, "a" * 64)
        profiles.activate(profile)
        embedding_indexer = EmbeddingIndexer(
            database, queue, profiles, provider_loader=lambda: provider
        )
        for source_id, _ in items:
            index_hash = store.index_fingerprint("x", source_id)
            queue.enqueue(
                "x", source_id, "embed_content", embedding_task_input_hash(profile.id, index_hash)
            )
            assert embedding_indexer.index_next() is not None
        retrieval = RetrievalService(database, store, indexer, profiles, cast(Any, runtime))
        service_result = retrieval.search(SearchRequest("中文", limit=4))
        assert service_result.found and service_result.retrieval_mode == "hybrid"
        monkeypatch.setattr(
            Application,
            "open",
            classmethod(lambda _cls, _root: _CliApplicationStub(retrieval)),
        )
        assert cli_main(["--root", str(tmp_path), "search", "中文", "--limit", "4"]) == 0
        cli_result = json.loads(capsys.readouterr().out)
        mcp_input = "\n".join(
            [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {},
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "favhub.search",
                            "arguments": {"query": "中文", "limit": 4},
                        },
                    }
                ),
            ]
        )
        mcp_output = io.StringIO()
        run_stdio(retrieval, io.StringIO(mcp_input), mcp_output, io.StringIO())
        mcp_result = json.loads(mcp_output.getvalue().splitlines()[-1])["result"][
            "structuredContent"
        ]
        service_source_ids = [hit.source_id for hit in service_result.hits]
        assert [hit["source_id"] for hit in cli_result["hits"]] == service_source_ids
        assert [hit["source_id"] for hit in mcp_result["hits"]] == service_source_ids
        service_citation_ids = [hit.citation_id for hit in service_result.hits]
        assert all(citation_id.startswith("favhub:x/") for citation_id in service_citation_ids)
        assert [hit["citation_id"] for hit in cli_result["hits"]] == service_citation_ids
        assert [hit["citation_id"] for hit in mcp_result["hits"]] == service_citation_ids
        service_evidence_levels = [hit.evidence_level for hit in service_result.hits]
        assert [hit["evidence_level"] for hit in cli_result["hits"]] == service_evidence_levels
        assert [hit["evidence_level"] for hit in mcp_result["hits"]] == service_evidence_levels
        service_evidence_warnings = [hit.evidence_warning for hit in service_result.hits]
        assert [hit["evidence_warning"] for hit in cli_result["hits"]] == service_evidence_warnings
        assert [hit["evidence_warning"] for hit in mcp_result["hits"]] == service_evidence_warnings
        service_supporting_citations = [
            [chunk.citation_id for chunk in hit.supporting_chunks] for hit in service_result.hits
        ]
        assert [
            [chunk["citation_id"] for chunk in hit["supporting_chunks"]]
            for hit in cli_result["hits"]
        ] == service_supporting_citations
        assert [
            [chunk["citation_id"] for chunk in hit["supporting_chunks"]]
            for hit in mcp_result["hits"]
        ] == service_supporting_citations
        chunk_id = database.connection.execute(
            "SELECT chunk_id FROM chunk_embeddings LIMIT 1"
        ).fetchone()[0]
        database.connection.execute("DELETE FROM chunk_embeddings WHERE chunk_id=?", (chunk_id,))
        assert profiles.summary().state == "partial"
        paths = FavHubPaths.from_root(tmp_path / "runtime")
        paths.ensure()
        cache_file = paths.models / "fake.onnx"
        cache_file.write_bytes(b"fake model")
        cache_file.unlink()
        from favhub.embedding_runtime import EmbeddingRuntime

        missing_runtime = EmbeddingRuntime(
            paths, profiles, provider_factory=cast(Any, lambda **_kwargs: provider)
        )
        fallback = RetrievalService(database, store, indexer, profiles, cast(Any, missing_runtime))
        assert fallback.search(SearchRequest("中文")).vector_warning == "embedding_unavailable"
        assert embedding_indexer.reindex_missing() >= 1
        while embedding_indexer.index_next() is not None:
            pass
        assert profiles.summary().state == "ready"
    finally:
        database.close()
