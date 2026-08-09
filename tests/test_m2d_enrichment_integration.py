"""End-to-end M2D coverage: fake Agent -> MCP enrichment -> retrieval.

A fake Agent drives the pull -> generate -> submit loop against a real
Application stack, including a JSON-RPC leg, a skip-and-retry, and a stale
race, then verifies summaries and tags are searchable with citations and the
contentTypes filter honours the classified type.
"""

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from favhub.application import Application
from favhub.domain import CapturedItem
from favhub.enrich_gateway import EnrichGateway
from favhub.mcp_server import PROTOCOL_VERSION, run_stdio
from favhub.retrieval import SearchRequest
from favhub.sync_gateway import SyncGateway

OBSERVED = datetime(2026, 7, 26, tzinfo=UTC)


def _item(platform: str, source_id: str, title: str, body: str) -> CapturedItem:
    url = (
        f"https://www.bilibili.com/video/{source_id}"
        if platform == "bilibili"
        else f"https://x.com/example/status/{source_id}"
    )
    return CapturedItem(
        platform=platform,
        source_id=source_id,
        canonical_url=url,
        title=title,
        author="作者",
        published_at=datetime(2026, 5, 1, tzinfo=UTC),
        observed_at=OBSERVED,
        body=body,
        collections=(),
        extractor_version="fixture-v1",
    )


def _ingest(application: Application, job_id: str, items: list[CapturedItem]) -> None:
    timestamp = "2026-07-26T00:00:00Z"
    application.database.connection.execute(
        """INSERT INTO sync_jobs(id, mode, status, options_json, created_at, updated_at)
           VALUES (?, 'full', 'running', '{}', ?, ?)""",
        (job_id, timestamp, timestamp),
    )
    by_platform: dict[str, list[CapturedItem]] = {}
    for item in items:
        by_platform.setdefault(item.platform, []).append(item)
    for platform, platform_items in by_platform.items():
        application.library.ingest_batch(
            job_id, platform, f"{job_id}-{platform}", platform_items, True
        )


def _gateway(application: Application) -> EnrichGateway:
    return EnrichGateway(
        application.database, application.queue, application.library, application.store
    )


def _mcp_call(
    application: Application, gateway: EnrichGateway, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "m2d-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ]
    stdout = io.StringIO()
    assert application.retrieval is not None
    run_stdio(
        application.retrieval,
        io.StringIO("".join(json.dumps(m) + "\n" for m in messages)),
        stdout,
        io.StringIO(),
        sync=SyncGateway(application.sync),
        enrich=gateway,
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    result = responses[1]["result"]
    assert "isError" not in result, result
    return dict(result["structuredContent"])


def _fields(summary: str, tags: list[str], content_type: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "tags": tags,
        "contentType": content_type,
        "provider": "agent",
        "model": "fake-agent-1",
    }


def test_fake_agent_enrichment_loop_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with Application.open(root) as application:
        gateway = _gateway(application)
        _ingest(
            application,
            "job-1",
            [
                _item("x", "1001", "检索笔记", "关于 hybrid retrieval 的收藏正文。"),
                _item("bilibili", "BV1M2DVIDEO1", "视频教程", "视频内容 工作流 演示。"),
            ],
        )
        notes = root / "items" / "x" / "1001" / "notes.md"
        notes_before = notes.read_bytes()

        # Leg 1 over real JSON-RPC: claim and submit the first task.
        claimed = _mcp_call(application, gateway, "favhub.enrich_next", {})
        task1 = claimed["task"]
        assert task1 is not None
        submitted = _mcp_call(
            application,
            gateway,
            "favhub.enrich_submit",
            {
                "taskId": task1["task_id"],
                **_fields(
                    "梳理 hybrid retrieval 工作流精读笔记 的核心要点。",
                    ["混合检索流程", "Retrieval"],
                    "text",
                ),
            },
        )
        assert submitted["outcome"] == "applied"

        # Leg 2: skip once, reclaim, then submit as the video type.
        task2 = gateway.next({})["task"]
        assert task2 is not None
        gateway.skip(
            {"taskId": task2["task_id"], "code": "generation_failed", "message": "首次生成失败"}
        )
        retry = gateway.next({})["task"]
        assert retry is not None and retry["attempts"] == 2
        assert (
            gateway.submit(
                {
                    "taskId": retry["task_id"],
                    **_fields("讲解视频工作流的教程摘要。", ["视频教程", "工作流"], "video"),
                }
            )["outcome"]
            == "applied"
        )

        # Stale race: content changes after the claim; submit is superseded.
        _ingest(application, "job-2", [_item("x", "1002", "第三条", "初版正文")])
        task3 = gateway.next({})["task"]
        assert task3 is not None
        _ingest(application, "job-3", [_item("x", "1002", "第三条", "重写后的正文")])
        stale = gateway.submit(
            {"taskId": task3["task_id"], **_fields("旧摘要", ["旧标签"], "text")}
        )
        assert stale["outcome"] == "stale"
        task4 = gateway.next({})["task"]
        assert task4 is not None and task4["source_id"] == "1002"
        assert (
            gateway.submit(
                {"taskId": task4["task_id"], **_fields("重写后的摘要。", ["重写"], "text")}
            )["outcome"]
            == "applied"
        )
        assert gateway.next({}) == {"task": None}

        # Enriched snapshots landed without touching user notes.
        assert notes.read_bytes() == notes_before
        snapshot = application.store.read_source("x", "1002")
        assert snapshot is not None
        assert snapshot["enrichment"]["summary"] == "重写后的摘要。"

        # Index and retrieve: summaries and tags are searchable with citations.
        assert application.indexer is not None
        while application.indexer.index_next() is not None:
            pass
        assert application.retrieval is not None
        summary_hits = application.retrieval.search(SearchRequest("工作流精读笔记", limit=5))
        assert summary_hits.found is True
        top = summary_hits.hits[0]
        assert top.platform == "x" and top.source_id == "1001"
        assert top.local_path.endswith("content.md")
        assert top.citation_id.startswith("favhub:x/1001#chunk-")

        tag_hits = application.retrieval.search(SearchRequest("混合检索流程", limit=5))
        assert tag_hits.found is True
        assert tag_hits.hits[0].source_id == "1001"

        video_only = application.retrieval.search(
            SearchRequest("摘要", content_types=("video",), limit=5)
        )
        assert video_only.found is True
        assert {hit.source_id for hit in video_only.hits} == {"BV1M2DVIDEO1"}

        # Unchanged re-ingest does not reopen the queue.
        _ingest(
            application,
            "job-4",
            [_item("x", "1001", "检索笔记", "关于 hybrid retrieval 的收藏正文。")],
        )
        assert gateway.next({}) == {"task": None}
