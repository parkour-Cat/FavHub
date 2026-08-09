import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from favhub.browser_capture import BROWSER_PROTOCOL_VERSION, BrowserCaptureStore
from favhub.browser_ingest import BrowserIngestError, BrowserIngestor
from favhub.database import Database
from favhub.domain import SyncMode
from favhub.enrichment_queue import EnrichmentQueue
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.sync_module import StartSyncRequest, SyncModule

FIXTURES = Path(__file__).parent / "fixtures"
OBSERVED = datetime(2026, 8, 2, tzinfo=UTC)


def fixture(*parts: str) -> Any:
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[BrowserIngestor, SyncModule, BrowserCaptureStore]]:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    library = LibraryModule(database, store, EnrichmentQueue(database))
    sync = SyncModule(database, library)
    sessions = BrowserCaptureStore(database)
    ingestor = BrowserIngestor(sync, sessions, clock=lambda: OBSERVED)
    try:
        yield ingestor, sync, sessions
    finally:
        database.close()


def open_session(
    sync: SyncModule,
    sessions: BrowserCaptureStore,
    platform: str,
    mode: SyncMode = SyncMode.INCREMENTAL,
) -> tuple[str, str]:
    started = sync.start_sync(
        StartSyncRequest(
            platforms=(platform,),
            mode=mode,
            published_since=None,
            published_until=None,
            max_scan_items=None,
        )
    )
    session = sessions.create(started.job_id, platform, BROWSER_PROTOCOL_VERSION)
    sessions.claim(session.id, "0.1.0", lease_seconds=600)
    return started.job_id, session.id


def fixture_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


def x_page(name: str) -> dict[str, Any]:
    # Text, not a decoded object: the extension forwards response bodies exactly
    # as the platform sent them. Every test here used to decode first, which is
    # why a router that could not read the real wire shape passed the suite and
    # failed on the first live page.
    return {
        "type": "capture.response",
        "platform": "x",
        "kind": "x.bookmarks_page",
        "body": fixture_text("x", name),
    }


def bilibili_bundle(
    media: dict[str, Any],
    *,
    scope_id: str,
    scope_name: str,
    detail: dict[str, Any] | None = None,
    subtitle: dict[str, Any] | None = None,
    subtitle_raw: str | None = None,
    subtitle_mismatch: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "capture.bundle",
        "platform": "bilibili",
        "kind": "bilibili.video_bundle",
        "scopeId": scope_id,
        "scopeName": scope_name,
        "resource": media,
        "detail": detail,
        "subtitle": subtitle,
        "subtitleRaw": subtitle_raw,
        "subtitleMismatch": subtitle_mismatch,
    }


def zhihu_page(body: Any, *, scope_id: str, scope_name: str) -> dict[str, Any]:
    return {
        "type": "capture.response",
        "platform": "zhihu",
        "kind": "zhihu.items_page",
        "scopeId": scope_id,
        "scopeName": scope_name,
        "body": body,
    }


# -- session and event validation --------------------------------------------


def test_an_unknown_session_is_rejected(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, _, _ = stack
    with pytest.raises(BrowserIngestError):
        ingestor.handle("00000000-0000-0000-0000-000000000000", x_page("bookmarks-page-1.json"))


def test_an_event_for_the_wrong_platform_is_rejected(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    with pytest.raises(BrowserIngestError):
        ingestor.handle(session_id, zhihu_page({}, scope_id="1", scope_name="a"))


def test_a_session_that_is_not_capturing_is_rejected(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    sessions.pause(session_id, "rate_limited", "slow down")
    with pytest.raises(BrowserIngestError):
        ingestor.handle(session_id, x_page("bookmarks-page-1.json"))


def test_an_unknown_event_kind_is_rejected(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    with pytest.raises(BrowserIngestError):
        ingestor.handle(
            session_id,
            {"type": "capture.response", "platform": "x", "kind": "x.likes_page", "body": {}},
        )


def test_a_platform_without_a_browser_adapter_is_rejected_by_name(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """A platform missing from the routing table is refused by name.

    GitHub is collected without a browser, so it has no entry. The kind check
    used to index ``_EVENT_KINDS[platform]`` directly, so this raised a bare
    ``KeyError`` instead of a coded protocol error.
    """
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "github")
    with pytest.raises(BrowserIngestError) as error:
        ingestor.handle(
            session_id,
            {
                "type": "capture.response",
                "platform": "github",
                "kind": "zhihu.items_page",
                "body": {},
            },
        )
    assert "github" in error.value.message


def test_finishing_an_unroutable_session_still_closes_the_scan(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """The rejected event above leaves session state behind; ``finish`` must not
    trip over a platform that has no flush."""
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "github")
    with pytest.raises(BrowserIngestError):
        ingestor.handle(
            session_id,
            {"type": "capture.response", "platform": "github", "kind": "whatever", "body": {}},
        )
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes=None,
        scope_results=None,
    )
    assert sync.get_status(job_id)["platforms"][0]["platform"] == "github"


def test_a_platform_error_envelope_surfaces_its_stable_code(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    with pytest.raises(BrowserIngestError) as error:
        ingestor.handle(
            session_id,
            {
                "type": "capture.response",
                "platform": "x",
                "kind": "x.bookmarks_page",
                "body": fixture("x", "logged-out.json"),
            },
        )
    assert error.value.code == "login_required"


# -- X ------------------------------------------------------------------------


def test_a_body_that_is_not_json_is_a_platform_condition_not_a_crash(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """An HTML error page or a truncated response reads as `page_changed`."""
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    event = x_page("bookmarks-page-1.json") | {"body": "<!doctype html><title>rate limited"}
    with pytest.raises(BrowserIngestError) as raised:
        ingestor.handle(session_id, event)
    assert raised.value.code == "page_changed"


def test_an_already_decoded_body_is_still_accepted(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """Active-mode adapters can decode same-origin JSON themselves."""
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    event = x_page("bookmarks-page-1.json") | {"body": fixture("x", "bookmarks-page-1.json")}
    assert ingestor.handle(session_id, event)["mapped"] == 3


def test_x_pages_map_through_the_existing_parser(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "x")
    result = ingestor.handle(session_id, x_page("bookmarks-page-1.json"))
    assert result["mapped"] == 3
    assert result["has_more"] is True
    assert result["cursor"] == "HBaKufT89rLF+DMAAA=="
    assert result["submitted"] == []

    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=("1011048574412526505",),
        frontier_scopes=None,
        scope_results=None,
    )
    rows = sync.database.connection.execute(
        "SELECT source_id FROM items WHERE platform = 'x' ORDER BY source_id"
    ).fetchall()
    assert [str(row["source_id"]) for row in rows] == [
        "1011048574412526505",
        "1112444407014444922",
        "1816024121722501770",
    ]
    assert sync.get_status(job_id)["platforms"][0]["counts"]["added"] == 3


def test_x_flushes_a_batch_once_twenty_items_are_mapped(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """X may submit as it goes: it has no folders to deduplicate across."""
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "x")
    submitted: list[str] = []
    # 3 + 2 tweets per page pair; repeat until the 20-item threshold trips.
    for index in range(5):
        for name in ("bookmarks-page-1.json", "bookmarks-page-2.json"):
            body = fixture("x", name)
            result = ingestor.handle(
                session_id,
                {
                    "type": "capture.response",
                    "platform": "x",
                    "kind": "x.bookmarks_page",
                    "body": _renumber_x(body, index),
                },
            )
            submitted.extend(result["submitted"])
    # 25 mapped tweets: one 20-item flush mid-run, the rest waits for finish.
    assert submitted == ["browser-batch-0001"]
    assert sync.get_status(job_id)["platforms"][0]["counts"]["added"] == 20


def _renumber_x(body: Any, offset: int) -> Any:
    """Give each repeat distinct tweet ids without touching the parser."""
    raw = json.dumps(body)
    for original in (
        "1011048574412526505",
        "1816024121722501770",
        "1112444407014444922",
        "1330120223525413131",
        "1132003155372311920",
    ):
        raw = raw.replace(original, f"{original[:-1]}{offset}")
    return json.loads(raw)


def test_replaying_the_same_x_page_does_not_duplicate_items(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "x")
    ingestor.handle(session_id, x_page("bookmarks-page-1.json"))
    ingestor.handle(session_id, x_page("bookmarks-page-1.json"))
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes=None,
        scope_results=None,
    )
    counts = sync.get_status(job_id)["platforms"][0]["counts"]
    assert counts["added"] == 3
    assert counts["duplicates"] == 3


def test_x_tombstones_survive_as_metadata_only_items(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """A deleted bookmark stays as a metadata-only item, not a dropped page."""
    ingestor, sync, sessions = stack
    _, session_id = open_session(sync, sessions, "x")
    body = fixture("x", "bookmarks-page-1.json")
    entries = body["data"]["bookmark_timeline_v2"]["timeline"]["instructions"][0]["entries"]
    entries.insert(0, fixture("x", "tombstone.json"))

    result = ingestor.handle(
        session_id,
        {
            "type": "capture.response",
            "platform": "x",
            "kind": "x.bookmarks_page",
            "body": body,
        },
    )
    assert result["mapped"] == 4
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes=None,
        scope_results=None,
    )
    row = sync.database.connection.execute(
        "SELECT COUNT(*) AS count FROM items WHERE platform = 'x'"
    ).fetchone()
    assert int(row["count"]) == 4


# -- Bilibili -----------------------------------------------------------------


def test_bilibili_bundles_deduplicate_across_folders_at_finish(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "bilibili")
    sync.register_scopes(job_id, "bilibili", {"11": "稍后看", "22": "技术"})
    medias = fixture("bilibili", "resources-page-1.json")["data"]["medias"]
    detail = fixture("bilibili", "video-detail.json")
    subtitle = fixture("bilibili", "subtitle.json")

    shared = medias[1]  # BV1bkz2gvaz6, the one the detail fixture describes
    ingestor.handle(
        session_id,
        bilibili_bundle(
            shared,
            scope_id="11",
            scope_name="稍后看",
            detail=detail,
            subtitle=subtitle,
            subtitle_raw=json.dumps(subtitle),
        ),
    )
    ingestor.handle(session_id, bilibili_bundle(shared, scope_id="22", scope_name="技术"))
    ingestor.handle(session_id, bilibili_bundle(medias[0], scope_id="22", scope_name="技术"))

    # Nothing is written until folders can be compared.
    assert (
        sync.database.connection.execute(
            "SELECT COUNT(*) FROM items WHERE platform = 'bilibili'"
        ).fetchone()[0]
        == 0
    )

    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"11": ("BV1bkz2gvaz6",), "22": ("BV11dzv1qkfv",)},
        scope_results=None,
    )
    rows = {
        str(row["source_id"]): str(row["item_dir"])
        for row in sync.database.connection.execute(
            "SELECT source_id, item_dir FROM items WHERE platform = 'bilibili'"
        )
    }
    assert set(rows) == {"BV1bkz2gvaz6", "BV11dzv1qkfv"}
    data_root = sync.library.store.items_root.parent
    manifest = json.loads(
        (data_root / rows["BV1bkz2gvaz6"] / "source.json").read_text(encoding="utf-8")
    )
    assert sorted(manifest["collections"]) == ["技术", "稍后看"]


def test_bilibili_keeps_a_video_without_detail_or_subtitle(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "bilibili")
    sync.register_scopes(job_id, "bilibili", {"11": "稍后看"})
    medias = fixture("bilibili", "resources-page-1.json")["data"]["medias"]
    ingestor.handle(session_id, bilibili_bundle(medias[0], scope_id="11", scope_name="稍后看"))
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"11": ("BV11dzv1qkfv",)},
        scope_results=None,
    )
    assert (
        sync.database.connection.execute(
            "SELECT COUNT(*) FROM items WHERE platform = 'bilibili'"
        ).fetchone()[0]
        == 1
    )


def _run_bilibili(ingestor, sync, sessions, bundle_kwargs: dict[str, Any]) -> None:
    # Full mode throughout: an incremental run counts an item it already has as
    # a duplicate without comparing anything, so it could never overwrite the
    # transcript these tests are about.
    job_id, session_id = open_session(sync, sessions, "bilibili", SyncMode.FULL)
    sync.register_scopes(job_id, "bilibili", {"11": "稍后看"})
    medias = fixture("bilibili", "resources-page-1.json")["data"]["medias"]
    ingestor.handle(
        session_id,
        bilibili_bundle(medias[0], scope_id="11", scope_name="稍后看", **bundle_kwargs),
    )
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"11": ("BV11dzv1qkfv",)},
        scope_results=None,
    )
    sessions.complete(session_id)


def test_a_transcript_already_held_survives_a_run_that_was_offered_the_wrong_one(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """Refusing is right; letting the refusal overwrite a good transcript is not.

    Bilibili's fault is intermittent — videos that served a correct transcript,
    verified against the object name, were offered another video's an hour
    later. A refresh that stores the refusal replaces those words with nothing,
    so every run made while the platform misbehaves strips more of the library
    than the fault ever did. Five transcripts went that way before this.
    """
    ingestor, sync, sessions = stack
    document = {"lang": "zh", "body": [{"from": 0.0, "to": 1.0, "content": "真正的字幕"}]}
    _run_bilibili(
        ingestor, sync, sessions, {"subtitle": document, "subtitle_raw": json.dumps(document)}
    )
    store = sync.library.store
    first = store.read_source("bilibili", "BV11dzv1qkfv")
    assert (first["platform_metadata"] or {})["subtitle_status"] == "available"
    assert "真正的字幕" in first["body"]

    _run_bilibili(ingestor, sync, sessions, {"subtitle_mismatch": "/bfs/ai_subtitle/prod/999"})

    kept = store.read_source("bilibili", "BV11dzv1qkfv")
    assert (kept["platform_metadata"] or {})["subtitle_status"] == "available"
    assert "真正的字幕" in kept["body"]
    assert "transcript/0001.md" in dict(store.iter_index_markdown("bilibili", "BV11dzv1qkfv"))


def test_a_video_that_never_had_a_transcript_still_records_the_refusal(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """Nothing is being protected here, and the refusal is worth knowing."""
    ingestor, sync, sessions = stack

    _run_bilibili(ingestor, sync, sessions, {"subtitle_mismatch": "/bfs/ai_subtitle/prod/999"})

    stored = sync.library.store.read_source("bilibili", "BV11dzv1qkfv")
    metadata = stored["platform_metadata"] or {}
    assert metadata["subtitle_status"] == "wrong_video"
    assert metadata["subtitle_offered"] == "/bfs/ai_subtitle/prod/999"


def test_bilibili_rejects_a_bundle_for_an_unregistered_scope(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "bilibili")
    sync.register_scopes(job_id, "bilibili", {"11": "稍后看"})
    medias = fixture("bilibili", "resources-page-1.json")["data"]["medias"]
    with pytest.raises(BrowserIngestError):
        ingestor.handle(session_id, bilibili_bundle(medias[0], scope_id="99", scope_name="别的"))


# -- Zhihu --------------------------------------------------------------------


def test_zhihu_pages_merge_folders_and_keep_the_earliest_favorited_time(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "zhihu")
    sync.register_scopes(job_id, "zhihu", {"42": "默认收藏夹", "99": "技术"})
    page = fixture("zhihu", "items-page-answer-article.json")

    early = json.loads(json.dumps(page))
    early["data"][0]["created"] = "2020-09-13T20:26:40+08:00"
    late = json.loads(json.dumps(page))
    late["data"][0]["created"] = "2023-11-14T06:13:20+08:00"

    ingestor.handle(session_id, zhihu_page(late, scope_id="42", scope_name="默认收藏夹"))
    result = ingestor.handle(session_id, zhihu_page(early, scope_id="99", scope_name="技术"))
    assert result["is_end"] is True

    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes={"42": ("answer-1776858097",)},
        scope_results=None,
    )
    rows = {
        str(row["source_id"]): row
        for row in sync.database.connection.execute(
            "SELECT source_id, favorited_at, item_dir FROM items WHERE platform = 'zhihu'"
        )
    }
    assert set(rows) == {"answer-1776858097", "article-198662942"}
    assert str(rows["answer-1776858097"]["favorited_at"]).startswith("2020-")
    # One item, but both folders it was saved in: collapsing to a single
    # collection would lose the shelf the user actually filed it under.
    stored = json.loads(
        (Path(str(rows["answer-1776858097"]["item_dir"])) / "source.json").read_text("utf-8")
    )
    assert stored["collections"] == ["技术", "默认收藏夹"]


def test_zhihu_reports_a_short_page_as_not_the_end(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    """A deleted favorite shrinks a page; only paging.is_end ends a folder."""
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "zhihu")
    sync.register_scopes(job_id, "zhihu", {"42": "默认收藏夹"})
    result = ingestor.handle(
        session_id,
        zhihu_page(
            fixture("zhihu", "items-page-short-not-end.json"),
            scope_id="42",
            scope_name="默认收藏夹",
        ),
    )
    assert result["is_end"] is False
    assert result["mapped"] == 3


def test_zhihu_rate_limit_envelope_surfaces_its_code(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "zhihu")
    sync.register_scopes(job_id, "zhihu", {"42": "默认收藏夹"})
    with pytest.raises(BrowserIngestError) as error:
        ingestor.handle(
            session_id,
            zhihu_page(
                fixture("zhihu", "rate-limited.json"), scope_id="42", scope_name="默认收藏夹"
            ),
        )
    assert error.value.code == "rate_limited"


# -- lifecycle ----------------------------------------------------------------


def test_drop_clears_pending_state_without_writing_anything(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "zhihu")
    sync.register_scopes(job_id, "zhihu", {"42": "默认收藏夹"})
    ingestor.handle(
        session_id,
        zhihu_page(
            fixture("zhihu", "items-page-answer-article.json"),
            scope_id="42",
            scope_name="默认收藏夹",
        ),
    )
    ingestor.drop(session_id)
    assert (
        sync.database.connection.execute(
            "SELECT COUNT(*) FROM items WHERE platform = 'zhihu'"
        ).fetchone()[0]
        == 0
    )
    # Finishing after a drop is a no-op flush, not a crash.
    ingestor.finish(
        session_id,
        observed_end=False,
        max_scan_reached=True,
        visible_total=None,
        frontier_ids=(),
        frontier_scopes=None,
        scope_results=None,
    )
    assert sync.get_status(job_id)["platforms"][0]["status"] == "partial"


def test_finish_advances_the_frontier_only_through_sync_module(
    stack: tuple[BrowserIngestor, SyncModule, BrowserCaptureStore],
) -> None:
    ingestor, sync, sessions = stack
    job_id, session_id = open_session(sync, sessions, "x")
    ingestor.handle(session_id, x_page("bookmarks-page-1.json"))
    assert (
        sync.database.connection.execute("SELECT COUNT(*) FROM sync_frontiers").fetchone()[0] == 0
    )
    ingestor.finish(
        session_id,
        observed_end=True,
        max_scan_reached=False,
        visible_total=None,
        frontier_ids=("1011048574412526505",),
        frontier_scopes=None,
        scope_results=None,
    )
    row = sync.database.connection.execute(
        "SELECT source_ids_json FROM sync_frontiers WHERE platform = 'x'"
    ).fetchone()
    assert json.loads(str(row["source_ids_json"])) == ["1011048574412526505"]
    assert sync.get_status(job_id)["platforms"][0]["status"] == "completed"
