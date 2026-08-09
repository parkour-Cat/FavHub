"""End-to-end M3 coverage: fake passive capture -> parsers -> MCP -> retrieval.

The fake capture serves synthetic Bookmarks GraphQL pages shaped like the
frozen fixtures. The driver emulates the favhub-x-sync Skill: cursor
pagination honoring the platform frontier, per-image OCR descriptions,
bounded idempotent batches, and platform-level finish — against a real
Application stack (SQLite, item files, indexing, retrieval, MCP status).
"""

import io
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from favhub.application import Application
from favhub.mcp_server import PROTOCOL_VERSION, run_stdio
from favhub.retrieval import SearchRequest
from favhub.sync_gateway import SyncGateway
from favhub.x_mapping import map_captured_item
from favhub.x_models import XTombstone, XTweet
from favhub.x_parsers import parse_bookmarks_page

OBSERVED_AT = datetime(2026, 7, 26, 12, tzinfo=UTC)


def _created(year: int, month: int, day: int) -> str:
    return datetime(year, month, day, tzinfo=UTC).strftime("%a %b %d %H:%M:%S %z %Y")


def _tweet_entry(
    tweet_id: str,
    text: str,
    *,
    name: str = "示例名称10",
    handle: str = "example",
    created_at: str | None = None,
    photos: int = 0,
    quoted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    legacy: dict[str, Any] = {
        "full_text": text,
        "created_at": created_at or _created(2026, 7, 20),
    }
    if photos:
        legacy["extended_entities"] = {
            "media": [
                {
                    "type": "photo",
                    "media_url_https": f"https://pbs.twimg.com/media/{tweet_id}-{n}.jpg",
                }
                for n in range(photos)
            ]
        }
    result: dict[str, Any] = {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {"user_results": {"result": {"core": {"name": name, "screen_name": handle}}}},
        "legacy": legacy,
    }
    if quoted is not None:
        result["quoted_status_result"] = {"result": quoted}
    return {
        "entryId": f"tweet-{tweet_id}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {"tweet_results": {"result": result}},
        },
    }


def _quoted_result(tweet_id: str, text: str, name: str, handle: str) -> dict[str, Any]:
    return {
        "__typename": "Tweet",
        "rest_id": tweet_id,
        "core": {"user_results": {"result": {"core": {"name": name, "screen_name": handle}}}},
        "legacy": {"full_text": text, "created_at": _created(2026, 7, 18)},
    }


def _tombstone_entry(tweet_id: str, reason: str) -> dict[str, Any]:
    return {
        "entryId": f"tweet-{tweet_id}",
        "content": {
            "entryType": "TimelineTimelineItem",
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "__typename": "TweetTombstone",
                        "tombstone": {"text": {"text": reason}},
                    }
                }
            },
        },
    }


def _page(items: list[dict[str, Any]], cursor: str) -> dict[str, Any]:
    entries = [
        *items,
        {
            "entryId": f"cursor-bottom-{cursor}",
            "content": {
                "entryType": "TimelineTimelineCursor",
                "cursorType": "Bottom",
                "value": cursor,
            },
        },
    ]
    return {
        "data": {
            "bookmark_timeline_v2": {
                "timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": entries}]}
            }
        }
    }


class FakeXCapture:
    """Ordered Bookmarks pages as the passive interception would record them."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages


def _to_mcp_item(item: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sourceId": item.source_id,
        "canonicalUrl": item.canonical_url,
        "title": item.title,
        "author": item.author,
        "publishedAt": item.published_at.isoformat().replace("+00:00", "Z"),
        "observedAt": item.observed_at.isoformat().replace("+00:00", "Z"),
        "body": item.body,
        "collections": list(item.collections),
        "extractorVersion": item.extractor_version,
    }
    if item.platform_metadata:
        payload["platformMetadata"] = item.platform_metadata
    if item.assets:
        payload["assets"] = [
            {
                "relativePath": asset.relative_path,
                "mediaType": asset.media_type,
                "text": asset.text,
                "sha256": asset.sha256,
            }
            for asset in item.assets
        ]
    return payload


def run_x_collection(
    gateway: SyncGateway,
    capture: FakeXCapture,
    *,
    mode: str = "full",
    published_since: str | None = None,
    descriptions: dict[str, list[str | None]] | None = None,
    batch_size: int = 20,
    job: dict[str, Any] | None = None,
    stop_after_first_batch: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if job is None:
        arguments: dict[str, Any] = {"platform": "x", "mode": mode}
        if published_since is not None:
            arguments["publishedSince"] = published_since
        job = gateway.start(arguments)
    job_id = str(job["job_id"])
    frontier = set(job["frontiers"].get("x", []))

    collected: list[XTweet | XTombstone] = []
    for raw_page in capture.pages:
        page = parse_bookmarks_page(raw_page)
        hit_frontier = False
        for tweet in page.tweets:
            if tweet.tweet_id in frontier:
                hit_frontier = True
                break
            collected.append(tweet)
        if hit_frontier or not page.has_more:
            break

    items = [
        map_captured_item(
            tweet,
            image_descriptions=(descriptions or {}).get(tweet.tweet_id)
            if isinstance(tweet, XTweet)
            else None,
            observed_at=OBSERVED_AT,
        )
        for tweet in collected
    ]

    receipts: list[dict[str, Any]] = []
    for start in range(0, len(items), batch_size):
        receipts.append(
            gateway.submit_batch(
                {
                    "jobId": job_id,
                    "platform": "x",
                    "batchId": f"b-{start // batch_size:04d}",
                    "items": [_to_mcp_item(item) for item in items[start : start + batch_size]],
                }
            )
        )
        if stop_after_first_batch:
            return job, receipts

    gateway.finish(
        {
            "jobId": job_id,
            "platform": "x",
            "observedEnd": True,
            "maxScanReached": False,
            "frontierIds": [tweet.tweet_id for tweet in collected][:20],
        }
    )
    return job, receipts


def _default_capture() -> FakeXCapture:
    quoted = _quoted_result(
        "3000000000000000004", "被引用的推文 工作流 更新说明", "被引作者", "quoted_author"
    )
    page1 = _page(
        [
            _tweet_entry(
                "3000000000000000001",
                "带图片的推文 学习笔记",
                photos=1,
                created_at=_created(2026, 7, 21),
            ),
            _tweet_entry(
                "3000000000000000002",
                "引用推文的正文 转发点评",
                quoted=quoted,
                created_at=_created(2026, 7, 20),
            ),
            _tombstone_entry("3000000000000000003", "该推文已被删除 unavailable"),
        ],
        "CURSOR-1",
    )
    page2 = _page(
        [
            _tweet_entry(
                "2999999999999999999", "普通推文 纯文本内容", created_at=_created(2026, 7, 15)
            )
        ],
        "CURSOR-2",
    )
    terminal = _page([], "CURSOR-END")
    return FakeXCapture([page1, page2, terminal])


DESCRIPTIONS = {
    "3000000000000000001": ["图片OCR文本 白板架构图 数据流向标注"],
}


def _mcp_sync_status(application: Application, gateway: SyncGateway, job_id: str) -> dict[str, Any]:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "m3-test", "version": "1"},
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
    assert application.retrieval is not None
    run_stdio(
        application.retrieval,
        io.StringIO("".join(json.dumps(m) + "\n" for m in messages)),
        stdout,
        io.StringIO(),
        sync=gateway,
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    result = responses[1]["result"]
    assert "isError" not in result
    return dict(result["structuredContent"])


def test_full_collection_ocr_quote_tombstone_and_retrieval(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with Application.open(root) as application:
        gateway = SyncGateway(application.sync)
        job, receipts = run_x_collection(gateway, _default_capture(), descriptions=DESCRIPTIONS)
        job_id = str(job["job_id"])
        assert sum(receipt["added"] for receipt in receipts) == 4

        image_dir = root / "items" / "x" / "3000000000000000001"
        assert "图片OCR文本" in (image_dir / "ocr" / "0001.md").read_text("utf-8")
        image_source = json.loads((image_dir / "source.json").read_text("utf-8"))
        assert image_source["platform_metadata"]["media"][0]["ocr_status"] == "available"

        quote_content = (root / "items" / "x" / "3000000000000000002" / "content.md").read_text(
            "utf-8"
        )
        assert "## 引用推文" in quote_content
        assert "被引用的推文 工作流 更新说明" in quote_content

        tombstone_source = json.loads(
            (root / "items" / "x" / "3000000000000000003" / "source.json").read_text("utf-8")
        )
        assert tombstone_source["platform_metadata"]["source_status"] == "source_unavailable"

        status = _mcp_sync_status(application, gateway, job_id)
        platform = status["platforms"][0]
        assert platform["platform"] == "x"
        assert platform["status"] == "completed"
        assert platform["scopes"] == []
        assert platform["counts"]["added"] == 4

        assert application.indexer is not None
        while application.indexer.index_next() is not None:
            pass
        assert application.retrieval is not None
        ocr_hits = application.retrieval.search(SearchRequest("图片OCR文本", limit=5))
        assert ocr_hits.found is True
        assert any(hit.local_path.endswith("ocr/0001.md") for hit in ocr_hits.hits)
        for hit in ocr_hits.hits:
            assert re.fullmatch(r"favhub:x/3000000000000000001#chunk-\d+", hit.citation_id)

        resumed = gateway.start({"platform": "x", "mode": "incremental"})
        assert resumed["frontiers"]["x"][0] == "3000000000000000001"


def test_publication_filter_does_not_stop_pagination_early(tmp_path: Path) -> None:
    capture = FakeXCapture(
        [
            _page(
                [
                    _tweet_entry("4000000000000000001", "新推文", created_at=_created(2026, 6, 1)),
                    _tweet_entry("4000000000000000002", "旧推文", created_at=_created(2024, 1, 1)),
                ],
                "C1",
            ),
            _page(
                [_tweet_entry("4000000000000000003", "较新推文", created_at=_created(2026, 5, 1))],
                "C2",
            ),
            _page([], "C-END"),
        ]
    )
    with Application.open(tmp_path / "root") as application:
        gateway = SyncGateway(application.sync)
        job, receipts = run_x_collection(gateway, capture, published_since="2026-01-01T00:00:00Z")
        status = gateway.status({"jobId": str(job["job_id"])})
        counts = status["platforms"][0]["counts"]
        assert counts["scanned"] == 3
        assert counts["added"] == 2
        assert counts["out_of_range"] == 1
        assert not (tmp_path / "root" / "items" / "x" / "4000000000000000002").exists()


def test_incremental_discovers_new_bookmark_and_stops_at_frontier(tmp_path: Path) -> None:
    capture = _default_capture()
    with Application.open(tmp_path / "root") as application:
        gateway = SyncGateway(application.sync)
        run_x_collection(gateway, capture, descriptions=DESCRIPTIONS)

        fresh = _tweet_entry(
            "3000000000000000009", "新书签的推文 增量发现", created_at=_created(2026, 7, 25)
        )
        first_entries = capture.pages[0]["data"]["bookmark_timeline_v2"]["timeline"][
            "instructions"
        ][0]["entries"]
        first_entries.insert(0, fresh)

        job, receipts = run_x_collection(gateway, capture, mode="incremental")
        assert job["frontiers"]["x"][0] == "3000000000000000001"
        assert sum(receipt["added"] for receipt in receipts) == 1
        assert sum(receipt["duplicates"] for receipt in receipts) == 0

        third = gateway.start({"platform": "x", "mode": "incremental"})
        assert third["frontiers"]["x"][0] == "3000000000000000009"


def test_pause_resume_replays_batches(tmp_path: Path) -> None:
    capture = _default_capture()
    with Application.open(tmp_path / "root") as application:
        gateway = SyncGateway(application.sync)
        job, first_receipts = run_x_collection(
            gateway,
            capture,
            descriptions=DESCRIPTIONS,
            batch_size=2,
            stop_after_first_batch=True,
        )
        job_id = str(job["job_id"])
        assert len(first_receipts) == 1

        paused = gateway.pause(
            {
                "jobId": job_id,
                "platform": "x",
                "code": "rate_limited",
                "message": "限流，稍后重试",
            }
        )
        assert paused["status"] == "paused"

        _, resumed = run_x_collection(
            gateway, capture, descriptions=DESCRIPTIONS, batch_size=2, job=job
        )
        assert resumed[0] == first_receipts[0]
        assert sum(receipt["added"] for receipt in resumed) == 4
        status = gateway.status({"jobId": job_id})
        assert status["capture_status"] == "completed"
        total = application.database.connection.execute(
            "SELECT COUNT(*) FROM items WHERE platform = 'x'"
        ).fetchone()[0]
        assert total == 4
