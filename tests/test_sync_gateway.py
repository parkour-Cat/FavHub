import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from favhub.database import Database
from favhub.domain import sha256_text
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.sync_gateway import PAUSE_CODES, SyncArgumentError, SyncGateway
from favhub.sync_module import SyncModule


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[SyncGateway, Database, ItemStore]]:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    gateway = SyncGateway(SyncModule(database, library))
    try:
        yield gateway, database, store
    finally:
        database.close()


def start_arguments(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "platform": "bilibili",
        "mode": "incremental",
        "scopes": [
            {"scopeId": "100001", "scopeName": "默认收藏夹"},
            {"scopeId": "100002", "scopeName": "技术分享"},
        ],
    }
    arguments.update(overrides)
    return arguments


TRANSCRIPT = "# Transcript\n\n[00:00] 大家好\n"
SUBTITLE_RAW = '{"body": [{"from": 0.0, "to": 1.0, "content": "大家好"}]}'


def item_payload(source_id: str = "BV1aa411c7mD", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sourceId": source_id,
        "canonicalUrl": f"https://www.bilibili.com/video/{source_id}",
        "title": "本地知识库设计漫谈",
        "author": "示例UP主",
        "publishedAt": "2026-01-02T00:00:00Z",
        "observedAt": "2026-07-26T00:00:00Z",
        "body": "简介\n\n[00:00] 大家好",
        "collections": ["技术分享"],
        "extractorVersion": "bilibili-browser-v1",
        "platformMetadata": {"subtitle_status": "available"},
        "assets": [
            {
                "relativePath": "transcript/0001.md",
                "mediaType": "text/markdown",
                "text": TRANSCRIPT,
                "sha256": sha256_text(TRANSCRIPT),
            },
            {
                "relativePath": "assets/subtitles/zh-CN.json",
                "mediaType": "application/json",
                "text": SUBTITLE_RAW,
                "sha256": sha256_text(SUBTITLE_RAW),
            },
        ],
    }
    payload.update(overrides)
    return payload


def submit_arguments(job_id: str, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "jobId": job_id,
        "platform": "bilibili",
        "batchId": "batch-1",
        "items": [item_payload()],
        "scopeScans": {"100002": ["BV1aa411c7mD"]},
    }
    arguments.update(overrides)
    return arguments


def test_start_creates_job_with_scoped_frontiers(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    result = gateway.start(start_arguments())
    assert isinstance(result["job_id"], str) and result["job_id"]
    assert result["frontiers"] == {"bilibili": []}
    assert result["scoped_frontiers"] == {"100001": [], "100002": []}


@pytest.mark.parametrize(
    "overrides",
    [
        {"platform": "x"},
        {"platform": ""},
        {"mode": "resync"},
        {"publishedSince": "2026-01-01T00:00:00"},
        {"publishedSince": "not-a-date"},
        {"publishedSince": "2026-02-01T00:00:00Z", "publishedUntil": "2026-01-01T00:00:00Z"},
        {"maxScanItems": 0},
        {"maxScanItems": True},
        {"maxScanItems": "5"},
        {"scopes": "not-a-list"},
        {"scopes": []},
        {"scopes": [{"scopeId": ""}]},
        {"scopes": [{"scopeName": "缺少ID"}]},
        {"scopes": [{"scopeId": "1", "cookie": "x"}]},
        {"scopes": [{"scopeId": "1"}, {"scopeId": "1"}]},
    ],
)
def test_start_rejects_invalid_arguments(
    stack: tuple[SyncGateway, Database, ItemStore], overrides: dict[str, Any]
) -> None:
    gateway, _, _ = stack
    with pytest.raises(SyncArgumentError):
        gateway.start(start_arguments(**overrides))


def test_submit_writes_items_and_assets_and_counts_scopes(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, store = stack
    job_id = gateway.start(start_arguments())["job_id"]

    receipt = gateway.submit_batch(submit_arguments(job_id))

    assert receipt["added"] == 1
    assert receipt["out_of_range"] == 0
    directory = store.items_root / "bilibili" / "BV1aa411c7mD"
    assert (directory / "transcript" / "0001.md").read_text("utf-8") == TRANSCRIPT
    assert json.loads((directory / "assets" / "subtitles" / "zh-CN.json").read_text("utf-8"))
    status = gateway.status({"jobId": job_id})
    scopes = {scope["scope_id"]: scope for scope in status["platforms"][0]["scopes"]}
    assert scopes["100002"]["counts"]["scanned"] == 1
    assert scopes["100001"]["counts"]["scanned"] == 0


def test_submit_replay_returns_original_receipt(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    first = gateway.submit_batch(submit_arguments(job_id))
    replay = gateway.submit_batch(submit_arguments(job_id))
    assert replay == first


def test_submit_rejects_duplicate_source_ids_in_batch(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    arguments = submit_arguments(job_id, items=[item_payload(), item_payload()])
    with pytest.raises(ValueError, match="duplicate"):
        gateway.submit_batch(arguments)


def test_submit_rejects_unknown_scope(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    arguments = submit_arguments(job_id, scopeScans={"999999": ["BV1aa411c7mD"]})
    with pytest.raises(KeyError, match="unknown scope"):
        gateway.submit_batch(arguments)


@pytest.mark.parametrize(
    "scope_scans",
    [
        "not-a-mapping",
        {"100002": "BV1"},
        {"100002": [1]},
        {"100002": [""]},
        {"": ["BV1"]},
        {"100002": ["BV"] * 201},
    ],
)
def test_submit_rejects_malformed_scope_scans(
    stack: tuple[SyncGateway, Database, ItemStore], scope_scans: Any
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    with pytest.raises(SyncArgumentError):
        gateway.submit_batch(submit_arguments(job_id, scopeScans=scope_scans))


@pytest.mark.parametrize(
    "items",
    ["not-a-list", [item_payload()] * 51],
)
def test_submit_rejects_malformed_items_argument(
    stack: tuple[SyncGateway, Database, ItemStore], items: Any
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    with pytest.raises(SyncArgumentError):
        gateway.submit_batch(submit_arguments(job_id, items=items))


def oversized_asset() -> dict[str, Any]:
    text = "a" * (2 * 1024 * 1024 + 1)
    return {
        "relativePath": "assets/subtitles/big.json",
        "mediaType": "application/json",
        "text": text,
        "sha256": sha256_text(text),
    }


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        (
            "assets",
            [
                {
                    "relativePath": "/etc/passwd",
                    "mediaType": "text/plain",
                    "text": "x",
                    "sha256": sha256_text("x"),
                }
            ],
            "relative",
        ),
        (
            "assets",
            [
                {
                    "relativePath": "assets/../../escape.json",
                    "mediaType": "application/json",
                    "text": "{}",
                    "sha256": sha256_text("{}"),
                }
            ],
            "asset",
        ),
        (
            "assets",
            [
                {
                    "relativePath": "assets/cover.bin",
                    "mediaType": "application/octet-stream",
                    "text": "x",
                    "sha256": sha256_text("x"),
                }
            ],
            "media_type",
        ),
        ("assets", [oversized_asset()], "maximum size"),
        (
            "assets",
            [
                {
                    "relativePath": "assets/subtitles/zh.json",
                    "mediaType": "application/json",
                    "text": "{}",
                    "sha256": "0" * 64,
                }
            ],
            "sha256",
        ),
        (
            "assets",
            [
                {
                    "relativePath": "assets/x.json",
                    "mediaType": "application/json",
                    "text": "{}",
                    "sha256": sha256_text("{}"),
                    "localPath": "C:/x",
                }
            ],
            "localPath",
        ),
        ("cookie", "secret", "cookie"),
        ("body", None, "body"),
        ("publishedAt", "2026-01-02T00:00:00", "timezone"),
        ("collections", "技术分享", "collections"),
        ("author", 7, "author"),
    ],
)
def test_submit_rejects_unsafe_item_content(
    stack: tuple[SyncGateway, Database, ItemStore],
    field: str,
    value: Any,
    match: str,
) -> None:
    gateway, _, store = stack
    job_id = gateway.start(start_arguments())["job_id"]
    arguments = submit_arguments(job_id, items=[item_payload(**{field: value})])
    with pytest.raises((ValueError, TypeError), match=match):
        gateway.submit_batch(arguments)
    assert not (store.items_root / "bilibili" / "BV1aa411c7mD").exists()


def test_pause_sanitizes_message_and_reports_paused(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    noisy = "登录已失效\x00\x1b[31m" + "补" * 300
    result = gateway.pause(
        {"jobId": job_id, "platform": "bilibili", "code": "login_required", "message": noisy}
    )
    assert result["status"] == "paused"
    assert result["error"]["code"] == "login_required"
    message = result["error"]["message"]
    assert "\x00" not in message and "\x1b" not in message
    assert len(message) <= 200
    status = gateway.status({"jobId": job_id})
    assert status["capture_status"] == "paused"
    assert status["platforms"][0]["error"]["code"] == "login_required"


def test_pause_rejects_unknown_code(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    assert "made_up_code" not in PAUSE_CODES
    with pytest.raises(SyncArgumentError):
        gateway.pause(
            {"jobId": job_id, "platform": "bilibili", "code": "made_up_code", "message": "x"}
        )


def test_finish_advances_scope_frontiers_and_reports_platform(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    gateway.submit_batch(submit_arguments(job_id))

    result = gateway.finish(
        {
            "jobId": job_id,
            "platform": "bilibili",
            "observedEnd": False,
            "maxScanReached": False,
            "frontierScopes": {"100002": ["BV1aa411c7mD"]},
        }
    )

    scope_statuses = {scope["scope_id"]: scope["status"] for scope in result["platform"]["scopes"]}
    assert scope_statuses == {"100001": "partial", "100002": "completed"}

    resumed = gateway.start(start_arguments())
    assert resumed["scoped_frontiers"] == {"100001": [], "100002": ["BV1aa411c7mD"]}


@pytest.mark.parametrize(
    "overrides",
    [
        {"observedEnd": "yes"},
        {"maxScanReached": None},
        {"visibleTotal": "10"},
        {"visibleTotal": -1},
        {"frontierIds": "BV1"},
        {"frontierIds": ["BV"] * 101},
        {"frontierScopes": "not-a-mapping"},
        {"frontierScopes": {"100002": ["BV"] * 101}},
    ],
)
def test_finish_rejects_malformed_arguments(
    stack: tuple[SyncGateway, Database, ItemStore], overrides: dict[str, Any]
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    arguments: dict[str, Any] = {
        "jobId": job_id,
        "platform": "bilibili",
        "observedEnd": True,
        "maxScanReached": False,
    }
    arguments.update(overrides)
    with pytest.raises(SyncArgumentError):
        gateway.finish(arguments)


def test_status_unknown_job_raises_key_error(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    with pytest.raises(KeyError):
        gateway.status({"jobId": "no-such-job"})


def test_finish_records_scope_results(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    result = gateway.finish(
        {
            "jobId": job_id,
            "platform": "bilibili",
            "observedEnd": False,
            "maxScanReached": True,
            "frontierScopes": {"100002": ["BV1aa411c7mD"]},
            "scopeResults": {
                "100001": {"maxScanReached": True, "visibleTotal": 40},
                "100002": {"visibleTotal": 3},
            },
        }
    )
    scopes = {scope["scope_id"]: scope for scope in result["platform"]["scopes"]}
    assert scopes["100001"]["max_scan_reached"] is True
    assert scopes["100001"]["visible_total"] == 40
    assert scopes["100001"]["status"] == "partial"
    assert scopes["100002"]["max_scan_reached"] is False
    assert scopes["100002"]["visible_total"] == 3
    assert scopes["100002"]["status"] == "completed"


@pytest.mark.parametrize(
    "scope_results",
    [
        "not-a-mapping",
        {"100001": "not-an-object"},
        {"100001": {"maxScanReached": "yes"}},
        {"100001": {"visibleTotal": -1}},
        {"100001": {"visibleTotal": True}},
        {"100001": {"cookie": "x"}},
        {"": {"maxScanReached": True}},
    ],
)
def test_finish_rejects_malformed_scope_results(
    stack: tuple[SyncGateway, Database, ItemStore], scope_results: Any
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start(start_arguments())["job_id"]
    with pytest.raises(SyncArgumentError):
        gateway.finish(
            {
                "jobId": job_id,
                "platform": "bilibili",
                "observedEnd": True,
                "maxScanReached": False,
                "scopeResults": scope_results,
            }
        )


X_OCR = "# OCR/visual description\n\ncards keywords pricing\n"


def x_item_payload(source_id: str = "1232164438310380159", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sourceId": source_id,
        "canonicalUrl": f"https://x.com/example_six/status/{source_id}",
        "title": "X bookmark with an image",
        "author": "sanjin",
        "publishedAt": "2026-07-20T00:00:00Z",
        "observedAt": "2026-07-26T00:00:00Z",
        "body": "tweet text",
        "collections": [],
        "extractorVersion": "x-browser-v1",
        "platformMetadata": {"source_status": "available", "author_handle": "example_six"},
        "assets": [
            {
                "relativePath": "ocr/0001.md",
                "mediaType": "text/markdown",
                "text": X_OCR,
                "sha256": sha256_text(X_OCR),
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_x_platform_lifecycle_with_platform_frontier(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, store = stack
    started = gateway.start({"platform": "x", "mode": "full"})
    job_id = started["job_id"]
    assert started["frontiers"] == {"x": []}
    assert started["scoped_frontiers"] == {}

    receipt = gateway.submit_batch(
        {
            "jobId": job_id,
            "platform": "x",
            "batchId": "b-0000",
            "items": [x_item_payload()],
        }
    )
    assert receipt["added"] == 1
    ocr_path = store.items_root / "x" / "1232164438310380159" / "ocr" / "0001.md"
    assert ocr_path.read_text("utf-8") == X_OCR

    finished = gateway.finish(
        {
            "jobId": job_id,
            "platform": "x",
            "observedEnd": True,
            "maxScanReached": False,
            "frontierIds": ["1232164438310380159"],
        }
    )
    assert finished["platform"]["status"] == "completed"

    resumed = gateway.start({"platform": "x", "mode": "incremental"})
    assert resumed["frontiers"] == {"x": ["1232164438310380159"]}


def test_x_pause_uses_shared_codes(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    job_id = gateway.start({"platform": "x", "mode": "full"})["job_id"]
    paused = gateway.pause(
        {"jobId": job_id, "platform": "x", "code": "rate_limited", "message": "限流"}
    )
    assert paused["status"] == "paused"


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("start", {"platform": "x", "mode": "full", "scopes": [{"scopeId": "1"}]}),
        (
            "submit_batch",
            {
                "jobId": "job",
                "platform": "x",
                "batchId": "b",
                "items": [],
                "scopeScans": {"1": ["a"]},
            },
        ),
        (
            "finish",
            {
                "jobId": "job",
                "platform": "x",
                "observedEnd": True,
                "maxScanReached": False,
                "frontierScopes": {"1": ["a"]},
            },
        ),
        (
            "finish",
            {
                "jobId": "job",
                "platform": "x",
                "observedEnd": True,
                "maxScanReached": False,
                "scopeResults": {"1": {"maxScanReached": True}},
            },
        ),
    ],
)
def test_x_rejects_bilibili_only_scope_arguments(
    stack: tuple[SyncGateway, Database, ItemStore],
    method: str,
    arguments: dict[str, Any],
) -> None:
    gateway, _, _ = stack
    with pytest.raises(SyncArgumentError, match="bilibili"):
        getattr(gateway, method)(arguments)


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("start", {"platform": "github", "mode": "full", "scopes": [{"scopeId": "1"}]}),
        (
            "submit_batch",
            {
                "jobId": "job",
                "platform": "github",
                "batchId": "b-0000",
                "items": [],
                "scopeScans": {"1": ["a"]},
            },
        ),
        (
            "finish",
            {
                "jobId": "job",
                "platform": "github",
                "observedEnd": True,
                "maxScanReached": False,
                "scopeResults": {"1": {"maxScanReached": True}},
            },
        ),
    ],
)
def test_github_rejects_bilibili_only_scope_arguments(
    stack: tuple[SyncGateway, Database, ItemStore],
    method: str,
    arguments: dict[str, Any],
) -> None:
    gateway, _, _ = stack
    with pytest.raises(SyncArgumentError, match="bilibili"):
        getattr(gateway, method)(arguments)


def test_zhihu_is_a_scoped_platform_end_to_end(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    scopes = [{"scopeId": "210943971", "scopeName": "示例收藏夹"}]
    started = gateway.start({"platform": "zhihu", "mode": "full", "scopes": scopes})
    assert started["scoped_frontiers"] == {"210943971": []}

    receipt = gateway.submit_batch(
        {
            "jobId": started["job_id"],
            "platform": "zhihu",
            "batchId": "z-0000",
            "items": [
                {
                    "sourceId": "answer-3276503723",
                    "canonicalUrl": "https://www.zhihu.com/question/56766597/answer/3276503723",
                    "title": "示例标题22：用于验证解析与索引行为",
                    "author": "示例名称10",
                    "publishedAt": "2023-11-04T07:22:13Z",
                    "observedAt": "2026-07-26T00:00:00Z",
                    "body": "回答正文",
                    "collections": ["示例收藏夹"],
                    "extractorVersion": "zhihu-browser-v1",
                    "platformMetadata": {"favorited_at": "2023-11-04T07:22:13Z"},
                }
            ],
            "scopeScans": {"210943971": ["answer-3276503723"]},
        }
    )
    assert receipt["added"] == 1

    gateway.finish(
        {
            "jobId": started["job_id"],
            "platform": "zhihu",
            "observedEnd": True,
            "maxScanReached": False,
            "frontierScopes": {"210943971": ["answer-3276503723"]},
        }
    )
    resumed = gateway.start({"platform": "zhihu", "mode": "incremental", "scopes": scopes})
    assert resumed["scoped_frontiers"] == {"210943971": ["answer-3276503723"]}


def test_unknown_platform_still_rejected(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    with pytest.raises(SyncArgumentError, match="platform"):
        gateway.start({"platform": "gitlab", "mode": "full"})


# -- Task 2: internal browser-only helpers -----------------------------------


def test_internal_helpers_are_not_public_mcp_operations() -> None:
    """Browser resume/scope registration must not widen the favhub.sync_* surface.

    The public tools take untyped Mapping arguments; these take typed ones and
    are only reachable from in-process browser code.
    """
    from favhub import mcp_server

    exposed = {tool["name"] for tool in mcp_server._TOOLS}
    assert not {name for name in exposed if "resume" in name or "register" in name}


def test_resume_run_lifts_a_pause(stack: tuple[SyncGateway, Database, ItemStore]) -> None:
    gateway, _, _ = stack
    started = gateway.start(start_arguments())
    job_id = str(started["job_id"])
    gateway.pause(
        {"jobId": job_id, "platform": "bilibili", "code": "browser_unavailable", "message": "gone"}
    )
    gateway.resume_run(job_id, "bilibili")
    status = gateway.status({"jobId": job_id})
    assert status["platforms"][0]["status"] == "running"
    assert status["platforms"][0]["error"] is None


def test_resume_run_rejects_an_unknown_platform(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    started = gateway.start(start_arguments())
    with pytest.raises(SyncArgumentError):
        gateway.resume_run(str(started["job_id"]), "reddit")


def test_register_browser_scopes_returns_typed_frontiers(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    started = gateway.start({"platform": "zhihu", "mode": "incremental"})
    frontiers = gateway.register_browser_scopes(
        str(started["job_id"]),
        "zhihu",
        {"42": "默认收藏夹", "99": "技术"},
    )
    assert frontiers == {"42": (), "99": ()}


def test_register_browser_scopes_rejects_unscoped_platforms(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    started = gateway.start({"platform": "x", "mode": "incremental"})
    with pytest.raises(SyncArgumentError):
        gateway.register_browser_scopes(str(started["job_id"]), "x", {"1": "nope"})


def test_register_browser_scopes_bounds_the_scope_count(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    started = gateway.start({"platform": "zhihu", "mode": "incremental"})
    too_many = {str(index): f"folder-{index}" for index in range(200)}
    with pytest.raises(SyncArgumentError):
        gateway.register_browser_scopes(str(started["job_id"]), "zhihu", too_many)


def test_internal_helpers_translate_module_errors(
    stack: tuple[SyncGateway, Database, ItemStore],
) -> None:
    gateway, _, _ = stack
    with pytest.raises(SyncArgumentError):
        gateway.resume_run("missing-job", "bilibili")
    with pytest.raises(SyncArgumentError):
        gateway.register_browser_scopes("missing-job", "zhihu", {"1": "x"})
