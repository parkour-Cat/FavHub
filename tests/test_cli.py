import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from favhub.application import Application
from favhub.cli import main
from favhub.embedding import EmbeddingProfile
from favhub.embedding_profiles import EmbeddingSummary
from favhub.embedding_service import EmbeddingBuildProgress
from favhub.retrieval import (
    GetItemRequest,
    ItemResponse,
    ReindexRequest,
    ReindexResponse,
    RetrievalMode,
    RetrievalStatus,
    SearchRequest,
    SearchResponse,
)
from favhub.root_lock import DataRootBusyError
from favhub.sync_module import SyncModule

FIXTURE = Path(__file__).parent / "fixtures" / "captured-items.json"


@dataclass
class _RetrievalRecorder:
    search_request: SearchRequest | None = None
    search_calls: list[tuple[SearchRequest, RetrievalMode]] = field(default_factory=list)
    item_request: GetItemRequest | None = None
    reindex_request: ReindexRequest | None = None

    def status(self) -> RetrievalStatus:
        return RetrievalStatus(2, 3, 4, 5)

    def search(
        self,
        request: SearchRequest,
        *,
        mode: RetrievalMode | str = RetrievalMode.AUTO,
    ) -> SearchResponse:
        self.search_request = request
        self.search_calls.append((request, RetrievalMode(mode)))
        return SearchResponse(found=False, reason="empty")

    def get_item(self, request: GetItemRequest) -> ItemResponse:
        self.item_request = request
        return ItemResponse(
            platform=request.platform,
            source_id=request.source_id,
            source={"title": "测试条目"},
            files=("source.json",),
        )

    def reindex(self, request: ReindexRequest) -> ReindexResponse:
        self.reindex_request = request
        return ReindexResponse(enqueued=7)


class _ApplicationWithRetrieval:
    def __init__(self, retrieval: Any) -> None:
        self.retrieval = retrieval

    def __enter__(self) -> "_ApplicationWithRetrieval":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        return None


@dataclass(frozen=True)
class _BuildReport:
    run_id: str = "run-1"
    profile_id: str = "profile-1"
    attempted: int = 2
    processed: int = 1
    skipped: int = 0
    failed: int = 1
    remaining: int = 3
    elapsed_seconds: float = 0.25
    errors: tuple[object, ...] = ()


class _EmbeddingRecorder:
    def __init__(self) -> None:
        self.build_arguments: tuple[int | None, bool] | None = None
        self.progress: object | None = None

    def initialize(self) -> EmbeddingProfile:
        return EmbeddingProfile(
            id="profile-1",
            provider="fastembed",
            provider_version="0.8",
            model="intfloat/multilingual-e5-small",
            dimensions=384,
            normalization="l2",
            max_input_tokens=512,
            segment_tokens=480,
            overlap_tokens=32,
            artifact_digest="a" * 64,
        )

    def build(self, *, max_items: int | None, force: bool, progress: Any = None) -> _BuildReport:
        self.build_arguments = (max_items, force)
        self.progress = progress
        if progress is not None:
            progress(
                EmbeddingBuildProgress(
                    phase="embed", done=3, remaining=9, vectors=41, elapsed_seconds=6.0
                )
            )
        return _BuildReport()

    def status(self) -> EmbeddingSummary:
        return EmbeddingSummary(state="disabled")


class _ApplicationWithEmbeddings(_ApplicationWithRetrieval):
    def __init__(self, retrieval: Any, embeddings: _EmbeddingRecorder, root: Path) -> None:
        super().__init__(retrieval)
        self.embedding_service = embeddings
        self.paths = type("Paths", (), {"models": root / "models"})()


def test_search_maps_filters_to_retrieval_request_and_serializes_response(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _RetrievalRecorder()
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(lambda _cls, _root: _ApplicationWithRetrieval(retrieval)),
    )

    assert (
        main(
            [
                "--root",
                "test-root",
                "search",
                "量子 计算",
                "--platform",
                "x",
                "--platform",
                "bilibili",
                "--content-type",
                "video",
                "--content-type",
                "text",
                "--published-since",
                "2026-01-01T00:00:00Z",
                "--published-until",
                "2026-01-02T00:00:00+00:00",
                "--limit",
                "3",
                "--retrieval-mode",
                "fts",
            ]
        )
        == 0
    )

    expected_request = SearchRequest(
        query="量子 计算",
        platforms=("x", "bilibili"),
        content_types=("video", "text"),
        published_since=datetime(2026, 1, 1, tzinfo=UTC),
        published_until=datetime(2026, 1, 2, tzinfo=UTC),
        limit=3,
    )
    assert retrieval.search_request == expected_request
    assert retrieval.search_calls == [(expected_request, RetrievalMode.FTS)]
    assert json.loads(capsys.readouterr().out) == {
        "found": False,
        "hits": [],
        "index_summary": {},
        "reason": "empty",
        "total_returned": 0,
        "retrieval_mode": "fts",
        "vector_warning": None,
        "embedding_summary": {},
    }


def test_search_batch_reuses_one_application_and_preserves_query_order(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _RetrievalRecorder()
    applications: list[_ApplicationWithRetrieval] = []

    def open_application(_cls: type[Application], _root: Path) -> _ApplicationWithRetrieval:
        application = _ApplicationWithRetrieval(retrieval)
        applications.append(application)
        return application

    monkeypatch.setattr(Application, "open", classmethod(open_application))

    assert (
        main(
            [
                "--root",
                "test-root",
                "search-batch",
                "--query",
                "AI 视频",
                "--query",
                "n8n 工作流",
                "--retrieval-mode",
                "hybrid",
            ]
        )
        == 0
    )

    assert len(applications) == 1
    assert [request.query for request, _mode in retrieval.search_calls] == [
        "AI 视频",
        "n8n 工作流",
    ]
    assert [mode for _request, mode in retrieval.search_calls] == [
        RetrievalMode.HYBRID,
        RetrievalMode.HYBRID,
    ]
    assert [entry["query"] for entry in json.loads(capsys.readouterr().out)["results"]] == [
        "AI 视频",
        "n8n 工作流",
    ]


def test_get_item_and_reindex_map_flags_and_serialize_dataclasses(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _RetrievalRecorder()
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(lambda _cls, _root: _ApplicationWithRetrieval(retrieval)),
    )

    assert (
        main(
            [
                "--root",
                "test-root",
                "get-item",
                "x",
                "item-1",
                "--include-content",
            ]
        )
        == 0
    )
    assert retrieval.item_request == GetItemRequest("x", "item-1", include_content=True)
    assert json.loads(capsys.readouterr().out)["source"] == {"title": "测试条目"}

    assert main(["--root", "test-root", "reindex", "--force"]) == 0
    assert retrieval.reindex_request == ReindexRequest(force=True)
    assert json.loads(capsys.readouterr().out) == {"enqueued": 7}


def test_embeddings_init_and_build_nested_commands_emit_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval = _RetrievalRecorder()
    embeddings = _EmbeddingRecorder()
    application = _ApplicationWithEmbeddings(retrieval, embeddings, tmp_path)
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(lambda _cls, _root: application),
    )

    assert main(["--root", "test-root", "embeddings", "init"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "artifact_digest": "a" * 64,
        "cache_path": str(tmp_path / "models"),
        "license": "MIT",
        "model": "intfloat/multilingual-e5-small",
        "profile_id": "profile-1",
    }

    assert (
        main(
            [
                "--root",
                "test-root",
                "embeddings",
                "build",
                "--max-items",
                "7",
                "--force",
            ]
        )
        == 0
    )
    assert embeddings.build_arguments == (7, True)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["run_id"] == "run-1"
    # Heartbeats go to stderr so stdout stays one parseable JSON report.
    assert "[embed] 3 done, 9 left, 41 vectors, 6s elapsed, ~18s left" in captured.err


def test_embeddings_build_can_be_silenced(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embeddings = _EmbeddingRecorder()
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(
            lambda _cls, _root: _ApplicationWithEmbeddings(
                _RetrievalRecorder(), embeddings, tmp_path
            )
        ),
    )

    assert main(["--root", "test-root", "embeddings", "build", "--quiet"]) == 0

    assert embeddings.progress is None
    assert capsys.readouterr().err == ""


def test_status_includes_embedding_summary_without_changing_sync_job_status(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval = _RetrievalRecorder()
    application = _ApplicationWithEmbeddings(retrieval, _EmbeddingRecorder(), tmp_path)
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(lambda _cls, _root: application),
    )

    assert main(["--root", "test-root", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["embedding_summary"] == {
        "active_profile": None,
        "corrupt_vectors": 0,
        "current_chunks": 0,
        "embedded_chunks": 0,
        "failed_tasks": 0,
        "last_build_report": None,
        "pending_tasks": 0,
        "state": "disabled",
    }


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_embeddings_build_rejects_non_positive_max_items(
    value: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(_cls: type[Application], _root: Path) -> Application:
        raise AssertionError("Application.open must not be called for invalid max-items")

    monkeypatch.setattr(Application, "open", classmethod(fail_open))
    with pytest.raises(SystemExit):
        main(
            [
                "--root",
                "test-root",
                "embeddings",
                "build",
                "--max-items",
                value,
            ]
        )
    assert "max-items must be a positive integer" in capsys.readouterr().err


def test_status_without_job_id_reports_retrieval_status(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _RetrievalRecorder()
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(lambda _cls, _root: _ApplicationWithRetrieval(retrieval)),
    )

    assert main(["--root", "test-root", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "failed_index_tasks": 5,
        "index_state": "available",
        "indexed_chunks": 3,
        "indexed_items": 2,
        "pending_index_tasks": 4,
        "unavailable_items": 0,
    }


@pytest.mark.parametrize(
    "argv, message",
    [
        (["search", "query", "--published-since", "bad-date"], "published_since"),
        (
            [
                "search",
                "query",
                "--published-since",
                "2026-01-02T00:00:00Z",
                "--published-until",
                "2026-01-01T00:00:00Z",
            ],
            "published_since must not be later than published_until",
        ),
        (["search", "query", "--limit", "0"], "limit must be between 1 and 50"),
        (["search", "   "], "query must not be blank"),
        (["search-batch"], "the following arguments are required: --query"),
        (["search-batch", "--query", "   "], "query must not be blank"),
        (
            [
                "search-batch",
                "--query",
                "query",
                "--favorited-since",
                "2026-01-02T00:00:00Z",
                "--favorited-until",
                "2026-01-01T00:00:00Z",
            ],
            "favorited_since must not be later than favorited_until",
        ),
    ],
)
def test_retrieval_validation_errors_are_reported_by_parser(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    message: str,
) -> None:
    def fail_open(_cls: type[Application], _root: Path) -> Application:
        raise AssertionError("Application.open must not be called for invalid search input")

    monkeypatch.setattr(Application, "open", classmethod(fail_open))

    with pytest.raises(SystemExit):
        main(["--root", "test-root", *argv])
    assert message in capsys.readouterr().err


def test_import_fixture_completes_capture_and_status_reports_pending_enrichment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "import-fixture",
                str(FIXTURE),
                "--mode",
                "full",
            ]
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)

    assert imported["capture_status"] == "completed"
    assert imported["platforms"][0]["counts"]["added"] == 1
    job_id = imported["job_id"]

    assert main(["--root", str(tmp_path), "status", job_id]) == 0
    status = json.loads(capsys.readouterr().out)
    # One index_content task plus one summarize task per imported item.
    assert status["enrichment_pending"] == 2
    assert isinstance(status["platforms"], list)


def test_import_fixture_groups_mixed_platforms_and_finishes_each_platform(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "mixed.json"
    values = json.loads(FIXTURE.read_text(encoding="utf-8"))
    values.append(
        {
            **values[0],
            "platform": "bilibili",
            "source_id": "bv-fixture-2",
            "canonical_url": "https://www.bilibili.com/video/BV-fixture-2",
        }
    )
    fixture.write_text(json.dumps(values), encoding="utf-8")

    assert (
        main(
            [
                "--root",
                str(tmp_path / "root"),
                "import-fixture",
                str(fixture),
                "--mode",
                "full",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["capture_status"] == "completed"
    assert [platform["platform"] for platform in result["platforms"]] == ["bilibili", "x"]
    assert all(platform["status"] == "completed" for platform in result["platforms"])


def test_import_fixture_delegates_max_scan_enforcement_to_sync_module(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "limited.json"
    values = json.loads(FIXTURE.read_text(encoding="utf-8"))
    values.append(
        {
            **values[0],
            "source_id": "fixture-2",
            "canonical_url": "https://example.com/x/fixture-2",
        }
    )
    fixture.write_text(json.dumps(values), encoding="utf-8")
    root = tmp_path / "root"

    assert (
        main(
            [
                "--root",
                str(root),
                "import-fixture",
                str(fixture),
                "--mode",
                "full",
                "--max-scan-items",
                "1",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["capture_status"] == "partial"
    assert result["platforms"][0]["counts"]["scanned"] == 1
    assert result["platforms"][0]["max_scan_reached"] is True
    assert result["platforms"][0]["observed_end"] is False

    with Application.open(root) as application:
        payload = json.loads(
            application.database.connection.execute(
                "SELECT receipt_json FROM sync_batches WHERE job_id = ?",
                (result["job_id"],),
            ).fetchone()[0]
        )
        frontier = application.database.connection.execute(
            "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
        ).fetchone()
    assert payload["_sync"]["truncated"] is True
    assert frontier is None


def test_incremental_fixture_stops_at_known_frontier_and_includes_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "history.json"
    template = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]
    history = [
        {
            **template,
            "source_id": source_id,
            "canonical_url": f"https://x.com/example/status/{source_id}",
            "published_at": f"2026-07-{day:02d}T00:00:00Z",
        }
        for source_id, day in (("newest", 3), ("middle", 2), ("oldest", 1))
    ]
    fixture.write_text(json.dumps(history), encoding="utf-8")
    root = tmp_path / "root"

    assert main(["--root", str(root), "import-fixture", str(fixture), "--mode", "full"]) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["platforms"][0]["counts"]["scanned"] == 3

    assert main(["--root", str(root), "import-fixture", str(fixture), "--mode", "incremental"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["capture_status"] == "completed"
    assert repeated["platforms"][0]["counts"]["scanned"] == 1
    assert repeated["platforms"][0]["counts"]["duplicates"] == 1

    with_new_item = [
        {
            **template,
            "source_id": "brand-new",
            "canonical_url": "https://x.com/example/status/brand-new",
            "published_at": "2026-07-04T00:00:00Z",
        },
        *history,
    ]
    fixture.write_text(json.dumps(with_new_item), encoding="utf-8")
    assert main(["--root", str(root), "import-fixture", str(fixture), "--mode", "incremental"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["platforms"][0]["counts"]["scanned"] == 2
    assert updated["platforms"][0]["counts"]["added"] == 1
    assert updated["platforms"][0]["counts"]["duplicates"] == 1


def test_partial_incremental_does_not_advance_frontier_past_unscanned_items(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "continuation.json"
    template = json.loads(FIXTURE.read_text(encoding="utf-8"))[0]

    def value(source_id: str, day: int) -> dict[str, object]:
        return {
            **template,
            "source_id": source_id,
            "canonical_url": f"https://x.com/example/status/{source_id}",
            "published_at": f"2026-07-{day:02d}T00:00:00Z",
        }

    initial = [value("boundary", 2), value("older", 1)]
    fixture.write_text(json.dumps(initial), encoding="utf-8")
    root = tmp_path / "root"
    assert main(["--root", str(root), "import-fixture", str(fixture), "--mode", "full"]) == 0
    capsys.readouterr()

    with_new = [value("newest", 4), value("newer", 3), *initial]
    fixture.write_text(json.dumps(with_new), encoding="utf-8")
    assert (
        main(
            [
                "--root",
                str(root),
                "import-fixture",
                str(fixture),
                "--mode",
                "incremental",
                "--max-scan-items",
                "1",
            ]
        )
        == 0
    )
    partial = json.loads(capsys.readouterr().out)
    assert partial["capture_status"] == "partial"

    assert main(["--root", str(root), "import-fixture", str(fixture), "--mode", "incremental"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["capture_status"] == "completed"
    assert resumed["platforms"][0]["counts"]["scanned"] == 3
    assert resumed["platforms"][0]["counts"]["added"] == 1
    assert resumed["platforms"][0]["counts"]["duplicates"] == 2


def test_import_fixture_marks_started_job_failed_and_reports_runtime_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"

    def fail_submit(
        _self: SyncModule,
        _job_id: str,
        _platform: str,
        _idempotency_key: str,
        _items: object,
    ) -> object:
        raise RuntimeError("injected fixture failure")

    monkeypatch.setattr(SyncModule, "submit_batch", fail_submit)

    with pytest.raises(SystemExit):
        main(
            [
                "--root",
                str(root),
                "import-fixture",
                str(FIXTURE),
                "--mode",
                "full",
            ]
        )
    assert "injected fixture failure" in capsys.readouterr().err

    with Application.open(root) as application:
        job_id = application.database.connection.execute(
            "SELECT id FROM sync_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        status = application.sync.get_status(job_id)
    assert status["capture_status"] == "failed"
    assert status["capture_finished_at"] is not None
    assert status["platforms"][0]["error"] == {
        "code": "fixture_import_failed",
        "message": "injected fixture failure",
    }


def test_import_fixture_failure_marks_every_unfinished_platform_failed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "mixed.json"
    values = json.loads(FIXTURE.read_text(encoding="utf-8"))
    values.append(
        {
            **values[0],
            "platform": "bilibili",
            "source_id": "bilibili-fixture",
            "canonical_url": "https://www.bilibili.com/video/bilibili-fixture",
        }
    )
    fixture.write_text(json.dumps(values), encoding="utf-8")
    root = tmp_path / "root"
    original_submit = SyncModule.submit_batch

    def fail_first_platform(
        self: SyncModule,
        job_id: str,
        platform: str,
        idempotency_key: str,
        items: object,
    ) -> object:
        if platform == "bilibili":
            raise RuntimeError("first platform failed")
        return original_submit(  # type: ignore[arg-type]
            self, job_id, platform, idempotency_key, items
        )

    monkeypatch.setattr(SyncModule, "submit_batch", fail_first_platform)

    with pytest.raises(SystemExit):
        main(
            [
                "--root",
                str(root),
                "import-fixture",
                str(fixture),
                "--mode",
                "full",
            ]
        )
    assert "first platform failed" in capsys.readouterr().err

    with Application.open(root) as application:
        job_id = application.database.connection.execute(
            "SELECT id FROM sync_jobs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        status = application.sync.get_status(job_id)
    assert status["capture_status"] == "failed"
    assert status["capture_finished_at"] is not None
    assert [platform["status"] for platform in status["platforms"]] == ["failed", "failed"]


@pytest.mark.parametrize(
    "payload",
    ["{not-json", "{}", "[]"],
)
def test_import_fixture_rejects_invalid_or_empty_fixture(
    tmp_path: Path, payload: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_text(payload, encoding="utf-8")

    with pytest.raises((ValueError, SystemExit)):
        main(
            [
                "--root",
                str(tmp_path / "root"),
                "import-fixture",
                str(fixture),
                "--mode",
                "full",
            ]
        )


def test_application_closes_database_when_startup_maintenance_fails(tmp_path: Path) -> None:
    paths = tmp_path / "root" / "items" / "x" / "broken"
    paths.mkdir(parents=True)
    (paths / "source.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError):
        Application.open(tmp_path / "root")

    # A closed connection releases SQLite's file lock and allows reopening.
    (paths / "source.json").unlink()
    app = Application.open(tmp_path / "root")
    app.close()


def test_enrich_backfill_enqueues_missing_summarize_tasks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "captured-items.json"
    assert main(["--root", str(tmp_path), "import-fixture", str(fixture), "--mode", "full"]) == 0
    capsys.readouterr()

    # Import already enqueued summarize tasks: backfill finds nothing new.
    assert main(["--root", str(tmp_path), "enrich-backfill"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["enqueued"] == 0
    assert first["already_current"] >= 1

    # Wipe the queue to simulate a pre-M2D root, then backfill restores it.
    with Application.open(tmp_path) as application:
        with application.database.transaction():
            application.database.connection.execute(
                "DELETE FROM enrichment_tasks WHERE kind = 'summarize'"
            )
        item_count = application.database.connection.execute(
            "SELECT COUNT(*) FROM items"
        ).fetchone()[0]

    assert main(["--root", str(tmp_path), "enrich-backfill"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["enqueued"] == item_count
    assert second["already_current"] == 0

    assert main(["--root", str(tmp_path), "enrich-backfill"]) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["enqueued"] == 0

    # An items/-only restore: the enrichment block itself counts as coverage.
    with Application.open(tmp_path) as application:
        row = application.database.connection.execute(
            "SELECT platform, source_id, content_hash FROM items LIMIT 1"
        ).fetchone()
        application.store.apply_enrichment(
            str(row["platform"]),
            str(row["source_id"]),
            {
                "summary": "已有摘要。",
                "tags": ["已有"],
                "content_type": "text",
                "provider": "agent",
                "model": "m",
                "generated_at": "2026-07-26T12:00:00Z",
                "input_hash": str(row["content_hash"]),
            },
        )
        with application.database.transaction():
            application.database.connection.execute(
                "DELETE FROM enrichment_tasks WHERE kind = 'summarize'"
            )

    assert main(["--root", str(tmp_path), "enrich-backfill"]) == 0
    fourth = json.loads(capsys.readouterr().out)
    assert fourth["enqueued"] == item_count - 1
    assert fourth["already_current"] == 1


def test_favtime_backfill_command_reports_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "captured-items.json"
    assert main(["--root", str(tmp_path), "import-fixture", str(fixture), "--mode", "full"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tmp_path), "favtime-backfill"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"updated", "unchanged"}


def test_search_maps_favorited_window_and_rejects_inverted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    retrieval = _RetrievalRecorder()
    monkeypatch.setattr(
        Application,
        "open",
        classmethod(lambda _cls, _root: _ApplicationWithRetrieval(retrieval)),
    )

    assert (
        main(
            [
                "--root",
                "r",
                "search",
                "q",
                "--favorited-since",
                "2026-06-01T00:00:00Z",
                "--favorited-until",
                "2026-07-01T00:00:00Z",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert retrieval.search_request.favorited_since == datetime(2026, 6, 1, tzinfo=UTC)
    assert retrieval.search_request.favorited_until == datetime(2026, 7, 1, tzinfo=UTC)

    with pytest.raises(SystemExit):
        main(
            [
                "--root",
                "r",
                "search",
                "q",
                "--favorited-since",
                "2026-07-01T00:00:00Z",
                "--favorited-until",
                "2026-06-01T00:00:00Z",
            ]
        )
    capsys.readouterr()


def test_a_busy_data_root_names_the_process_holding_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One FavHub per root is the design; being told only a lock path is not.

    Indexing, embedding and every backfill run through this door, and the
    holder is the Agent window the reader has open right now — which the bare
    message never said, so the way forward had to be guessed at every time.
    """
    root = tmp_path / "root"
    (root / "state").mkdir(parents=True)
    (root / "state" / "browser-pipe.json").write_text(
        json.dumps({"pid": os.getpid(), "pipe": r"\.\pipe\favhub"}), encoding="utf-8"
    )

    def refuse(_root: Any) -> Any:
        raise DataRootBusyError(f"FavHub data root is already in use (lock: {root})")

    monkeypatch.setattr(Application, "open", staticmethod(refuse))

    with pytest.raises(SystemExit):
        main(["--root", str(root), "reindex"])

    message = capsys.readouterr().err
    assert "already in use" in message
    assert f"pid {os.getpid()}" in message
    assert "Close it" in message
