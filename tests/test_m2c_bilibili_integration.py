"""End-to-end M2C coverage: fake browser -> parsers -> MCP sync -> retrieval.

The fake browser serves synthetic same-origin response payloads shaped like
the frozen fixtures. The driver below emulates the favhub-bilibili-sync Skill
workflow (enumerate, per-folder pagination honoring frontiers, dedup, detail
and subtitle parsing, bounded idempotent batches, finish with per-folder
frontiers) against a real Application stack: SQLite, item files, indexing,
and MCP status.
"""

import io
import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from favhub.application import Application
from favhub.bilibili_mapping import deduplicate, map_captured_item
from favhub.bilibili_models import BilibiliCaptureError
from favhub.bilibili_parsers import (
    parse_folders,
    parse_resource_page,
    parse_subtitle,
    parse_video_detail,
)
from favhub.domain import CapturedAsset, CapturedItem, isoformat
from favhub.mcp_server import PROTOCOL_VERSION, run_stdio
from favhub.retrieval import SearchRequest
from favhub.sync_gateway import SyncGateway

OBSERVED_AT = datetime(2026, 7, 26, tzinfo=UTC)


def _epoch(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


def _media(bvid: str, title: str, pubtime: int, intro: str = "") -> dict[str, Any]:
    return {
        "id": zlib.crc32(bvid.encode("utf-8")),
        "bvid": bvid,
        "title": title,
        "intro": intro,
        "cover": f"https://i0.hdslb.com/bfs/archive/{bvid}.jpg",
        "upper": {"mid": 80000002, "name": "示例UP主"},
        "pubtime": pubtime,
        "fav_time": pubtime + 1000,
    }


def _detail(bvid: str, title: str, pubdate: int, desc: str) -> dict[str, Any]:
    return {
        "code": 0,
        "message": "0",
        "data": {
            "bvid": bvid,
            "aid": zlib.crc32(bvid.encode("utf-8")),
            "title": title,
            "desc": desc,
            "pic": f"https://i0.hdslb.com/bfs/archive/{bvid}.jpg",
            "pubdate": pubdate,
            "duration": 600,
            "owner": {"mid": 80000002, "name": "示例UP主"},
            "cid": 220000001,
        },
    }


def _subtitle(*cues: tuple[float, float, str]) -> dict[str, Any]:
    return {
        "lan": "zh-CN",
        "body": [{"from": start, "to": end, "content": content} for start, end, content in cues],
    }


@dataclass
class FakeBilibiliBrowser:
    """Synthetic same-origin structured responses, keyed like the fixtures."""

    folders: dict[str, tuple[str, list[list[dict[str, Any]]]]]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    subtitles: dict[str, dict[str, Any]] = field(default_factory=dict)

    def folder_list(self) -> dict[str, Any]:
        return {
            "code": 0,
            "message": "0",
            "data": {
                "count": len(self.folders),
                "list": [
                    {"id": int(scope_id), "title": name, "media_count": sum(map(len, pages))}
                    for scope_id, (name, pages) in self.folders.items()
                ],
            },
        }

    def resource_page(self, scope_id: str, page_index: int) -> dict[str, Any]:
        _, pages = self.folders[scope_id]
        return {
            "code": 0,
            "message": "0",
            "data": {
                "medias": pages[page_index],
                "has_more": page_index + 1 < len(pages),
            },
        }

    def video_detail(self, bvid: str) -> dict[str, Any]:
        return self.details.get(bvid, {"code": -404, "message": "啥都木有", "data": None})

    def subtitle(self, bvid: str) -> dict[str, Any] | None:
        return self.subtitles.get(bvid)


def _to_mcp_item(item: CapturedItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sourceId": item.source_id,
        "canonicalUrl": item.canonical_url,
        "title": item.title,
        "author": item.author,
        "publishedAt": isoformat(item.published_at),
        "observedAt": isoformat(item.observed_at),
        "body": item.body,
        "collections": list(item.collections),
        "extractorVersion": item.extractor_version,
    }
    if item.platform_metadata:
        payload["platformMetadata"] = item.platform_metadata
    if item.assets:
        payload["assets"] = [_to_mcp_asset(asset) for asset in item.assets]
    return payload


def _to_mcp_asset(asset: CapturedAsset) -> dict[str, Any]:
    return {
        "relativePath": asset.relative_path,
        "mediaType": asset.media_type,
        "text": asset.text,
        "sha256": asset.sha256,
    }


def run_collection(
    gateway: SyncGateway,
    browser: FakeBilibiliBrowser,
    *,
    mode: str = "full",
    published_since: str | None = None,
    published_until: str | None = None,
    batch_size: int = 20,
    job: dict[str, Any] | None = None,
    stop_after_first_batch: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Emulate the Skill workflow; returns the start payload and receipts."""
    folders = parse_folders(browser.folder_list())
    if job is None:
        arguments: dict[str, Any] = {
            "platform": "bilibili",
            "mode": mode,
            "scopes": [
                {"scopeId": folder.scope_id, "scopeName": folder.title} for folder in folders
            ],
        }
        if published_since is not None:
            arguments["publishedSince"] = published_since
        if published_until is not None:
            arguments["publishedUntil"] = published_until
        job = gateway.start(arguments)
    job_id = str(job["job_id"])
    frontiers = {scope: set(ids) for scope, ids in dict(job["scoped_frontiers"]).items()}

    pages_by_scope: dict[str, list[Any]] = {}
    scanned_by_scope: dict[str, list[str]] = {}
    for folder in folders:
        frontier = frontiers.get(folder.scope_id, set())
        entries: list[Any] = []
        page_index = 0
        while True:
            page = parse_resource_page(browser.resource_page(folder.scope_id, page_index))
            hit_frontier = False
            for entry in page.entries:
                if entry.bvid in frontier:
                    hit_frontier = True
                    break
                # Publication-range filtering happens after normalization in
                # SyncModule; pagination never stops early on dates.
                entries.append(entry)
            if hit_frontier or not page.has_more:
                break
            page_index += 1
        pages_by_scope[folder.scope_id] = entries
        scanned_by_scope[folder.scope_id] = [entry.bvid for entry in entries]

    observations = deduplicate(
        pages_by_scope, {folder.scope_id: folder.title for folder in folders}
    )
    items: list[CapturedItem] = []
    for observation in observations.values():
        try:
            detail: Any = parse_video_detail(browser.video_detail(observation.bvid))
        except BilibiliCaptureError as error:
            detail = error
        subtitle: Any = None
        subtitle_raw: str | None = None
        raw_payload = browser.subtitle(observation.bvid)
        if raw_payload is not None:
            try:
                subtitle = parse_subtitle(raw_payload)
                subtitle_raw = json.dumps(raw_payload, ensure_ascii=False)
            except BilibiliCaptureError as error:
                subtitle = error
        items.append(
            map_captured_item(
                observation,
                detail=detail,
                subtitle=subtitle,
                subtitle_raw=subtitle_raw,
                observed_at=OBSERVED_AT,
            )
        )

    receipts: list[dict[str, Any]] = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        arguments = {
            "jobId": job_id,
            "platform": "bilibili",
            "batchId": f"b-{start // batch_size:04d}",
            "items": [_to_mcp_item(item) for item in batch],
        }
        if start == 0:
            arguments["scopeScans"] = {scope: ids for scope, ids in scanned_by_scope.items()}
        receipts.append(gateway.submit_batch(arguments))
        if stop_after_first_batch:
            return job, receipts

    gateway.finish(
        {
            "jobId": job_id,
            "platform": "bilibili",
            "observedEnd": True,
            "maxScanReached": False,
            "visibleTotal": sum(len(ids) for ids in scanned_by_scope.values()),
            "frontierScopes": {scope: ids[:20] for scope, ids in scanned_by_scope.items()},
            "scopeResults": {
                folder.scope_id: {
                    "maxScanReached": False,
                    "visibleTotal": folder.media_count,
                }
                for folder in folders
            },
        }
    )
    return job, receipts


def _default_browser() -> FakeBilibiliBrowser:
    shared = _media("BV1SHARED001", "两个收藏夹共有的视频", _epoch(2026, 3, 1))
    return FakeBilibiliBrowser(
        folders={
            "100001": (
                "默认收藏夹",
                [[shared, _media("BV1PLAIN0001", "普通视频", _epoch(2026, 2, 1))]],
            ),
            "100002": (
                "技术分享",
                [
                    [shared, _media("BV1SUBBED001", "带字幕的视频", _epoch(2026, 4, 1))],
                    [_media("BV1BROKEN001", "已失效视频", _epoch(2026, 1, 15))],
                ],
            ),
        },
        details={
            "BV1SHARED001": _detail(
                "BV1SHARED001", "两个收藏夹共有的视频", _epoch(2026, 3, 1), "共有视频 简介"
            ),
            "BV1PLAIN0001": _detail(
                "BV1PLAIN0001", "普通视频", _epoch(2026, 2, 1), "普通视频 简介"
            ),
            "BV1SUBBED001": _detail(
                "BV1SUBBED001", "带字幕的视频", _epoch(2026, 4, 1), "字幕视频 简介"
            ),
        },
        subtitles={
            "BV1SUBBED001": _subtitle(
                (0.0, 2.5, "大家好 欢迎收看"),
                (2.5, 6.0, "增量同步 边界条件 字幕检索"),
            ),
        },
    )


def _mcp_status(application: Application, gateway: SyncGateway, job_id: str) -> dict[str, Any]:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "m2c-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "favhub.sync_status", "arguments": {"jobId": job_id}},
        },
    ]
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert application.retrieval is not None
    run_stdio(
        application.retrieval,
        io.StringIO("".join(json.dumps(message) + "\n" for message in messages)),
        stdout,
        stderr,
        sync=gateway,
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    result = responses[1]["result"]
    assert "isError" not in result
    return dict(result["structuredContent"])


def test_full_collection_dedup_assets_status_and_retrieval(tmp_path: Path) -> None:
    browser = _default_browser()
    root = tmp_path / "root"
    with Application.open(root) as application:
        gateway = SyncGateway(application.sync)
        job, receipts = run_collection(gateway, browser, mode="full", batch_size=20)
        job_id = str(job["job_id"])

        assert sum(receipt["added"] for receipt in receipts) == 4

        # Re-running the whole collection replays every confirmed batch and
        # returns the original durable receipts without duplicate writes.
        _, replay_receipts = run_collection(gateway, browser, mode="full", job=job)
        assert replay_receipts == receipts

        # Cross-folder dedup: the shared video exists once, with both names.
        shared_source = json.loads(
            (root / "items" / "bilibili" / "BV1SHARED001" / "source.json").read_text("utf-8")
        )
        assert shared_source["collections"] == ["技术分享", "默认收藏夹"]

        # Subtitle raw asset and transcript are persisted for the subbed video.
        subbed_dir = root / "items" / "bilibili" / "BV1SUBBED001"
        raw = json.loads((subbed_dir / "assets" / "subtitles" / "zh-CN.json").read_text("utf-8"))
        assert raw["body"][1]["content"] == "增量同步 边界条件 字幕检索"
        transcript = (subbed_dir / "transcript" / "0001.md").read_text("utf-8")
        assert "字幕检索" in transcript

        # Detail failure keeps list metadata with a stable error code.
        broken_source = json.loads(
            (root / "items" / "bilibili" / "BV1BROKEN001" / "source.json").read_text("utf-8")
        )
        assert broken_source["platform_metadata"]["source_status"] == "source_unavailable"
        assert broken_source["title"] == "已失效视频"

        # MCP status reports per-folder enumeration and progress.
        status = _mcp_status(application, gateway, job_id)
        scopes = {scope["scope_id"]: scope for scope in status["platforms"][0]["scopes"]}
        assert scopes["100001"]["scope_name"] == "默认收藏夹"
        assert scopes["100002"]["scope_name"] == "技术分享"
        assert scopes["100001"]["counts"]["scanned"] == 2
        assert scopes["100002"]["counts"]["scanned"] == 3
        assert scopes["100001"]["visible_total"] == 2
        assert scopes["100002"]["visible_total"] == 3
        assert all(scope["status"] == "completed" for scope in scopes.values())
        assert all(scope["max_scan_reached"] is False for scope in scopes.values())

        # Index and retrieve: hits must cite content.md or transcript files.
        assert application.indexer is not None
        while application.indexer.index_next() is not None:
            pass
        indexed_paths = {
            str(row["relative_path"])
            for row in application.database.connection.execute(
                """SELECT DISTINCT relative_path FROM content_chunks
                   WHERE platform = 'bilibili' AND source_id = 'BV1SUBBED001'"""
            )
        }
        assert indexed_paths == {"content.md", "transcript/0001.md"}

        assert application.retrieval is not None
        result = application.retrieval.search(SearchRequest("字幕检索", limit=5))
        assert result.found is True
        for hit in result.hits:
            assert hit.platform == "bilibili"
            assert hit.local_path.startswith("items/bilibili/BV1SUBBED001/")
            assert hit.local_path.endswith(("content.md", "transcript/0001.md"))
            assert re.fullmatch(r"favhub:bilibili/BV1SUBBED001#chunk-\d+", hit.citation_id)


def test_publication_range_filters_without_early_pagination_stop(tmp_path: Path) -> None:
    browser = FakeBilibiliBrowser(
        folders={
            "100003": (
                "混合时间",
                [
                    [
                        _media("BV1NEWEST001", "新视频", _epoch(2026, 6, 1)),
                        _media("BV1OLDEST001", "旧视频", _epoch(2024, 1, 1)),
                    ],
                    [_media("BV1MIDDLE001", "较新视频", _epoch(2026, 5, 1))],
                ],
            ),
        },
        details={
            "BV1NEWEST001": _detail("BV1NEWEST001", "新视频", _epoch(2026, 6, 1), "新"),
            "BV1OLDEST001": _detail("BV1OLDEST001", "旧视频", _epoch(2024, 1, 1), "旧"),
            "BV1MIDDLE001": _detail("BV1MIDDLE001", "较新视频", _epoch(2026, 5, 1), "较新"),
        },
    )
    with Application.open(tmp_path / "root") as application:
        gateway = SyncGateway(application.sync)
        job, receipts = run_collection(
            gateway, browser, mode="full", published_since="2026-01-01T00:00:00Z"
        )
        status = gateway.status({"jobId": str(job["job_id"])})
        platform = status["platforms"][0]
        # The old item sits between two new ones: full pagination is required.
        assert platform["counts"]["scanned"] == 3
        assert platform["counts"]["added"] == 2
        assert platform["counts"]["out_of_range"] == 1
        assert platform["scopes"][0]["counts"]["scanned"] == 3
        assert not (tmp_path / "root" / "items" / "bilibili" / "BV1OLDEST001").exists()
        assert sum(receipt["out_of_range"] for receipt in receipts) == 1


def test_incremental_frontiers_advance_independently(tmp_path: Path) -> None:
    browser = _default_browser()
    with Application.open(tmp_path / "root") as application:
        gateway = SyncGateway(application.sync)
        run_collection(gateway, browser, mode="full")

        # A new favorite lands only in the default folder.
        name, pages = browser.folders["100001"]
        new_video = _media("BV1FRESH0001", "新收藏的视频", _epoch(2026, 7, 1))
        browser.folders["100001"] = (name, [[new_video, *pages[0]], *pages[1:]])
        browser.details["BV1FRESH0001"] = _detail(
            "BV1FRESH0001", "新收藏的视频", _epoch(2026, 7, 1), "新收藏"
        )

        job, receipts = run_collection(gateway, browser, mode="incremental")
        assert job["scoped_frontiers"]["100001"] == ["BV1SHARED001", "BV1PLAIN0001"]
        assert job["scoped_frontiers"]["100002"] == [
            "BV1SHARED001",
            "BV1SUBBED001",
            "BV1BROKEN001",
        ]
        assert sum(receipt["added"] for receipt in receipts) == 1
        assert sum(receipt["duplicates"] for receipt in receipts) == 0

        status = gateway.status({"jobId": str(job["job_id"])})
        scanned = {
            scope["scope_id"]: scope["counts"]["scanned"]
            for scope in status["platforms"][0]["scopes"]
        }
        # Folder A scanned only the new video; folder B stopped at its frontier.
        assert scanned == {"100001": 1, "100002": 0}

        third = gateway.start(
            {
                "platform": "bilibili",
                "mode": "incremental",
                "scopes": [{"scopeId": "100001"}, {"scopeId": "100002"}],
            }
        )
        assert third["scoped_frontiers"]["100001"][0] == "BV1FRESH0001"
        assert third["scoped_frontiers"]["100002"][0] == "BV1SHARED001"


def test_pause_resume_replays_batches_and_commits_frontier_once(tmp_path: Path) -> None:
    browser = _default_browser()
    root = tmp_path / "root"
    with Application.open(root) as application:
        gateway = SyncGateway(application.sync)

        job, first_receipts = run_collection(
            gateway, browser, mode="full", batch_size=2, stop_after_first_batch=True
        )
        job_id = str(job["job_id"])
        assert len(first_receipts) == 1

        paused = gateway.pause(
            {
                "jobId": job_id,
                "platform": "bilibili",
                "code": "rate_limited",
                "message": "限流，稍后重试",
            }
        )
        assert paused["status"] == "paused"

        # Interrupted before finish: no per-folder frontier was committed.
        frontier_rows = application.database.connection.execute(
            "SELECT COUNT(*) FROM sync_frontier_scopes"
        ).fetchone()[0]
        assert frontier_rows == 0
        assert gateway.status({"jobId": job_id})["capture_status"] == "paused"

        # Resume with the same job: batch b-0000 replays, the rest completes.
        _, resumed_receipts = run_collection(gateway, browser, mode="full", batch_size=2, job=job)
        assert resumed_receipts[0] == first_receipts[0]
        assert sum(receipt["added"] for receipt in resumed_receipts) == 4

        status = gateway.status({"jobId": job_id})
        assert status["capture_status"] == "completed"
        scopes = status["platforms"][0]["scopes"]
        assert all(scope["status"] == "completed" for scope in scopes)
        scanned = {scope["scope_id"]: scope["counts"]["scanned"] for scope in scopes}
        # Scope scans were attached to the replayed batch and not re-applied,
        # then applied once by the resumed run's fresh receipt.
        assert scanned == {"100001": 2, "100002": 3}
        assert (
            application.database.connection.execute(
                "SELECT COUNT(*) FROM sync_frontier_scopes"
            ).fetchone()[0]
            == 2
        )
        item_count = application.database.connection.execute(
            "SELECT COUNT(*) FROM items WHERE platform = 'bilibili'"
        ).fetchone()[0]
        assert item_count == 4
