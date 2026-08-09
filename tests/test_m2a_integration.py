import io
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from favhub.application import Application
from favhub.cli import main as cli_main
from favhub.mcp_server import PROTOCOL_VERSION
from favhub.mcp_server import main as mcp_main
from favhub.retrieval import ReindexRequest, SearchHit, SearchRequest, SearchResponse

FIXTURE = Path(__file__).parent / "fixtures" / "m2a-captured-items.json"
QUERIES = ("captured", "知识库")
EXPECTED_IDENTITIES = {
    "captured": (
        "x",
        "m2a-x-1",
        "https://x.com/example/status/m2a-x-1",
    ),
    "知识库": (
        "bilibili",
        "BV1M2AFixture",
        "https://www.bilibili.com/video/BV1M2AFixture",
    ),
}


@dataclass(frozen=True, slots=True)
class AcceptanceCase:
    query: str
    expected_identity: tuple[str, str]
    expected_file_suffix: str | None = None


ACCEPTANCE_CASES = (
    AcceptanceCase("captured", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("durable", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("citation", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("python", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("rebuild", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("bookmarks", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("canonical", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("deduplicate", ("x", "m2a-x-1"), "content.md"),
    AcceptanceCase("知识库", ("bilibili", "BV1M2AFixture"), "content.md"),
    AcceptanceCase("本地检索", ("bilibili", "BV1M2AFixture"), "content.md"),
    AcceptanceCase("收藏整理", ("bilibili", "BV1M2AFixture"), "content.md"),
    AcceptanceCase("关键词", ("bilibili", "BV1M2AFixture"), "content.md"),
    AcceptanceCase("视频字幕", ("bilibili", "BV1M2AFixture"), "transcript.md"),
    AcceptanceCase("增量同步", ("bilibili", "BV1M2AFixture"), "transcript.md"),
    AcceptanceCase("任务队列", ("bilibili", "BV1M2AFixture"), "transcript.md"),
    AcceptanceCase("离线问答", ("bilibili", "BV1M2AFixture"), "transcript.md"),
    AcceptanceCase("OCR", ("x", "m2a-x-1"), "ocr.md"),
    AcceptanceCase("image", ("x", "m2a-x-1"), "ocr.md"),
    AcceptanceCase("screenshot", ("x", "m2a-x-1"), "ocr.md"),
    AcceptanceCase("visual", ("x", "m2a-x-1"), "ocr.md"),
)

IDENTITY_URLS = {
    (platform, source_id): canonical_url
    for platform, source_id, canonical_url in EXPECTED_IDENTITIES.values()
}


def _cli_json(capsys: pytest.CaptureFixture[str], root: Path, *arguments: str) -> dict[str, Any]:
    assert cli_main(["--root", str(root), *arguments]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _drain_index(root: Path) -> tuple[int, dict[str, int]]:
    processed = 0
    with Application.open(root) as application:
        assert application.indexer is not None
        assert application.retrieval is not None
        while application.indexer.index_next() is not None:
            processed += 1
        status = application.retrieval.status().as_dict()
    return processed, status


def _item_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted((root / "items").rglob("*"))
        if path.is_file()
    }


def _write_fixture_enrichment(root: Path) -> None:
    generated_files = {
        root / "items" / "bilibili" / "BV1M2AFixture" / "transcript.md": (
            "# Fixture Transcript\n\n"
            "[00:00] 视频字幕 增量同步 全量同步 任务队列\n\n"
            "[00:10] 片段引用 离线问答 内容索引\n"
        ),
        root / "items" / "x" / "m2a-x-1" / "ocr.md": (
            "# Fixture OCR Placeholder\n\nOCR image screenshot visual tutorial\n"
        ),
    }
    for path, content in generated_files.items():
        assert path.parent.is_dir()
        path.write_bytes(content.encode("utf-8"))


def _acceptance_searches(root: Path) -> dict[str, SearchResponse]:
    assert len(ACCEPTANCE_CASES) == len({case.query for case in ACCEPTANCE_CASES}) == 20
    with Application.open(root) as application:
        assert application.retrieval is not None
        return {
            case.query: application.retrieval.search(SearchRequest(case.query, limit=5))
            for case in ACCEPTANCE_CASES
        }


def _is_traceable_acceptance_hit(root: Path, case: AcceptanceCase, hit: SearchHit) -> bool:
    platform, source_id = case.expected_identity
    if (hit.platform, hit.source_id) != case.expected_identity:
        return False
    if hit.canonical_url != IDENTITY_URLS[case.expected_identity]:
        return False

    relative_path = PurePosixPath(hit.local_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return False
    if relative_path.parts[:3] != ("items", platform, source_id):
        return False
    if case.expected_file_suffix is not None and relative_path.parts[3:] != (
        case.expected_file_suffix,
    ):
        return False
    try:
        local_file = root.joinpath(*relative_path.parts).resolve()
        local_file.relative_to(root.resolve())
        lines = local_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError, ValueError):
        return False
    if not 1 <= hit.line_start <= hit.line_end <= len(lines):
        return False
    cited_text = "\n".join(lines[hit.line_start - 1 : hit.line_end])
    citation = re.fullmatch(
        rf"favhub:{re.escape(platform)}/{re.escape(source_id)}#chunk-(\d+)",
        hit.citation_id,
    )
    return citation is not None and case.query.casefold() in cited_text.casefold()


def _assert_acceptance_matrix(
    root: Path, results: dict[str, SearchResponse]
) -> dict[str, SearchHit]:
    useful_hits: dict[str, SearchHit] = {}
    for case in ACCEPTANCE_CASES:
        result = results[case.query]
        assert result.total_returned == len(result.hits)
        if result.found is not True:
            continue
        useful_hit = next(
            (hit for hit in result.hits[:5] if _is_traceable_acceptance_hit(root, case, hit)),
            None,
        )
        if useful_hit is not None:
            useful_hits[case.query] = useful_hit

    useful_count = len(useful_hits)
    assert useful_count >= 16, f"only {useful_count} of 20 acceptance queries were useful"
    assert useful_hits["deduplicate"].local_path.endswith("/content.md")
    assert useful_hits["视频字幕"].local_path.endswith("/transcript.md")
    assert useful_hits["OCR"].local_path.endswith("/ocr.md")
    return useful_hits


def _mcp_searches(root: Path) -> dict[str, dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "m2a-acceptance", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    messages.extend(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "name": "favhub.search",
                "arguments": {"query": query},
            },
        }
        for request_id, query in enumerate(QUERIES, start=2)
    )
    stdin = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
    stdout = io.StringIO()
    stderr = io.StringIO()

    assert mcp_main(["--root", str(root)], stdin=stdin, stdout=stdout, stderr=stderr) == 0
    assert stderr.getvalue() == ""
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[0]["result"]["protocolVersion"] == PROTOCOL_VERSION
    return {
        query: response["result"]["structuredContent"]
        for query, response in zip(QUERIES, responses[1:], strict=True)
    }


def _assert_useful_hits(root: Path, query: str, result: dict[str, Any]) -> None:
    assert result["found"] is True
    assert result["total_returned"] == len(result["hits"]) >= 1
    expected_platform, expected_source_id, expected_url = EXPECTED_IDENTITIES[query]
    assert {
        (hit["platform"], hit["source_id"], hit["canonical_url"]) for hit in result["hits"]
    } == {(expected_platform, expected_source_id, expected_url)}
    for hit in result["hits"]:
        assert query.casefold() in hit["excerpt"].casefold()

        relative_path = PurePosixPath(hit["local_path"])
        assert not relative_path.is_absolute()
        assert ".." not in relative_path.parts
        assert relative_path.parts[0] == "items"
        assert relative_path.parts[1:3] == (expected_platform, expected_source_id)
        local_file = root.joinpath(*relative_path.parts).resolve()
        local_file.relative_to(root.resolve())
        assert local_file.is_file()

        assert 1 <= hit["line_start"] <= hit["line_end"]
        citation = re.fullmatch(
            rf"favhub:{re.escape(expected_platform)}/"
            rf"{re.escape(expected_source_id)}#chunk-(\d+)",
            hit["citation_id"],
        )
        assert citation is not None


def test_m2a_cli_and_mcp_retrieval_survives_derived_index_rebuild(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "favhub"
    imported = _cli_json(capsys, root, "import-fixture", str(FIXTURE), "--mode", "full")
    assert imported["capture_status"] == "completed"
    assert {entry["platform"] for entry in imported["platforms"]} == {"bilibili", "x"}
    assert sum(entry["counts"]["added"] for entry in imported["platforms"]) == 2
    # Fixture import is a legacy platform-level job: no per-folder scopes, and
    # it remains distinct from the M2C browser-driven Bilibili connector.
    assert all(entry["scopes"] == [] for entry in imported["platforms"])

    with Application.open(root) as application:
        assert application.retrieval is not None
        assert application.retrieval.status().pending_index_tasks == 2
        _write_fixture_enrichment(root)

        reindex = application.retrieval.reindex(ReindexRequest())
        assert reindex.enqueued == 2
        assert application.retrieval.status().pending_index_tasks == 4
        for platform, source_id in IDENTITY_URLS:
            row = application.database.connection.execute(
                """SELECT index_input_hash FROM items
                   WHERE platform = ? AND source_id = ?""",
                (platform, source_id),
            ).fetchone()
            assert row is not None
            assert row["index_input_hash"] == application.store.index_fingerprint(
                platform, source_id
            )

    processed, indexed_status = _drain_index(root)
    assert processed == 4
    assert indexed_status["indexed_items"] == 2
    assert indexed_status["indexed_chunks"] >= 2
    assert indexed_status["pending_index_tasks"] == 0
    assert indexed_status["failed_index_tasks"] == 0

    cli_before = {query: _cli_json(capsys, root, "search", query) for query in QUERIES}
    mcp_before = _mcp_searches(root)
    assert cli_before == mcp_before
    for query, result in cli_before.items():
        _assert_useful_hits(root, query, result)

    acceptance_before = _acceptance_searches(root)
    _assert_acceptance_matrix(root, acceptance_before)

    with Application.open(root) as application:
        assert application.retrieval is not None
        connection = application.database.connection
        item_count = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        item_files = _item_files(root)
        with application.database.transaction():
            connection.execute("DELETE FROM content_chunks")
        assert connection.execute("SELECT COUNT(*) FROM content_chunks").fetchone()[0] == 0
        for query in dict.fromkeys((*QUERIES, *(case.query for case in ACCEPTANCE_CASES))):
            quoted_token = '"' + query.replace('"', '""') + '"'
            assert (
                connection.execute(
                    """SELECT rowid FROM content_chunks_fts
                   WHERE content_chunks_fts MATCH ?""",
                    (quoted_token,),
                ).fetchall()
                == []
            )
        assert connection.execute("SELECT COUNT(*) FROM items").fetchone()[0] == item_count == 2
        assert _item_files(root) == item_files

        reindex = application.retrieval.reindex(ReindexRequest())
        assert reindex.enqueued == 2
        pending_status = application.retrieval.status().as_dict()
        assert pending_status["pending_index_tasks"] == 2
        assert pending_status["failed_index_tasks"] == 0

    rebuilt, rebuilt_status = _drain_index(root)
    assert rebuilt == 2
    assert rebuilt_status == indexed_status
    assert _item_files(root) == item_files

    cli_after = {query: _cli_json(capsys, root, "search", query) for query in QUERIES}
    mcp_after = _mcp_searches(root)
    acceptance_after = _acceptance_searches(root)
    assert cli_after == cli_before
    assert mcp_after == mcp_before
    assert cli_after == mcp_after
    assert acceptance_after == acceptance_before
    _assert_acceptance_matrix(root, acceptance_after)
