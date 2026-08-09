"""End-to-end M6 coverage: fake collections API -> parsers -> MCP -> retrieval."""

from datetime import UTC, datetime
from pathlib import Path

from favhub.application import Application
from favhub.domain import isoformat
from favhub.retrieval import SearchRequest
from favhub.sync_gateway import SyncGateway
from favhub.zhihu_mapping import deduplicate, map_captured_item, source_id_for
from favhub.zhihu_parsers import parse_collections_page, parse_items_page

OBSERVED = datetime(2026, 7, 26, tzinfo=UTC)


def _answer(answer_id: str, question: str, html: str, created: str) -> dict:
    return {
        "created": created,
        "content": {
            "type": "answer",
            "id": answer_id,
            "url": f"https://www.zhihu.com/question/1/answer/{answer_id}",
            "content": html,
            "excerpt": html[:40],
            "question": {"id": "1", "title": question},
            "author": {"name": "答主"},
            "voteup_count": 7,
            "created_time": 1699082533,
            "updated_time": 1699082533,
        },
    }


def _page(entries: list[dict], *, is_end: bool, totals: int) -> dict:
    return {"data": entries, "paging": {"is_end": is_end, "totals": totals}}


def _to_mcp(item) -> dict:
    return {
        "sourceId": item.source_id,
        "canonicalUrl": item.canonical_url,
        "title": item.title,
        "author": item.author,
        "publishedAt": isoformat(item.published_at),
        "observedAt": isoformat(item.observed_at),
        "body": item.body,
        "collections": list(item.collections),
        "extractorVersion": item.extractor_version,
        "platformMetadata": item.platform_metadata,
    }


def test_zhihu_collection_end_to_end(tmp_path: Path) -> None:
    collections_payload = {
        "data": [
            {"id": 100, "title": "示例收藏夹", "item_count": 2, "is_default": False},
            {"id": 200, "title": "编程", "item_count": 1, "is_default": True},
        ],
        "paging": {"is_end": True},
    }
    duplicate = _answer(
        "11", "如何入门检索系统？", "<p>hybrid 检索实践 示例正文</p>", "2024-01-05T10:00:00+08:00"
    )
    pages_by_scope = {
        "100": [
            # Short page (1 < totals 2) that is NOT the end, then the real end.
            _page([duplicate], is_end=False, totals=2),
            _page(
                [
                    _answer(
                        "12", "怎么学习算法？", "<p>刷题与图论笔记</p>", "2023-06-01T09:00:00+08:00"
                    )
                ],
                is_end=True,
                totals=2,
            ),
        ],
        "200": [
            # Same favorite saved earlier in another folder.
            _page(
                [dict(duplicate, created="2022-03-03T08:00:00+08:00")],
                is_end=True,
                totals=1,
            )
        ],
    }

    folders = parse_collections_page(collections_payload)
    folder_names = {f.scope_id: f.title for f in folders}
    favorites_by_scope = {
        scope: [favorite for raw in pages for favorite in parse_items_page(raw).favorites]
        for scope, pages in pages_by_scope.items()
    }
    observations = deduplicate(favorites_by_scope, folder_names)
    items = [
        map_captured_item(o.favorite, collection_titles=o.collections, observed_at=OBSERVED)
        for o in observations.values()
    ]

    with Application.open(tmp_path / "root") as app:
        gateway = SyncGateway(app.sync)
        job = gateway.start(
            {
                "platform": "zhihu",
                "mode": "full",
                "scopes": [{"scopeId": f.scope_id, "scopeName": f.title} for f in folders],
            }
        )
        receipt = gateway.submit_batch(
            {
                "jobId": job["job_id"],
                "platform": "zhihu",
                "batchId": "z-0000",
                "items": [_to_mcp(i) for i in items],
                "scopeScans": {
                    scope: [source_id_for(f.content) for f in favorites]
                    for scope, favorites in favorites_by_scope.items()
                },
            }
        )
        assert receipt["added"] == 2  # duplicate collapsed across folders
        gateway.finish(
            {
                "jobId": job["job_id"],
                "platform": "zhihu",
                "observedEnd": True,
                "maxScanReached": False,
                "frontierScopes": {"100": ["answer-11"], "200": ["answer-11"]},
            }
        )

        # Earliest favorited time won and reached the column.
        rows = dict(
            app.database.connection.execute(
                "SELECT source_id, favorited_at FROM items WHERE platform='zhihu'"
            ).fetchall()
        )
        assert rows["answer-11"] == "2022-03-03T00:00:00Z"

        # Cross-folder collections merged on the stored snapshot.
        snapshot = app.store.read_source("zhihu", "answer-11")
        assert snapshot is not None
        assert sorted(snapshot["collections"]) == ["示例收藏夹", "编程"]

        # Scoped frontiers feed the next incremental run.
        resumed = gateway.start(
            {
                "platform": "zhihu",
                "mode": "incremental",
                "scopes": [{"scopeId": f.scope_id, "scopeName": f.title} for f in folders],
            }
        )
        assert resumed["scoped_frontiers"] == {"100": ["answer-11"], "200": ["answer-11"]}
        frontier = set(resumed["scoped_frontiers"]["100"])
        fresh = _page(
            [
                _answer("13", "新收藏的问题？", "<p>新增内容</p>", "2026-07-20T12:00:00+08:00"),
                duplicate,
            ],
            is_end=False,
            totals=3,
        )
        discovered = []
        for favorite in parse_items_page(fresh).favorites:
            if source_id_for(favorite.content) in frontier:
                break
            discovered.append(favorite)
        assert [source_id_for(f.content) for f in discovered] == ["answer-13"]

        # Retrieval cites the rendered answer body.
        while app.indexer.index_next() is not None:
            pass
        hits = app.retrieval.search(SearchRequest("hybrid 检索实践", limit=5))
        assert hits.found is True
        top = hits.hits[0]
        assert top.platform == "zhihu" and top.source_id == "answer-11"
        assert top.citation_id.startswith("favhub:zhihu/answer-11#chunk-")

        windowed = app.retrieval.search(
            SearchRequest(
                "检索",
                platforms=("zhihu",),
                favorited_since=datetime(2023, 1, 1, tzinfo=UTC),
                limit=10,
            )
        )
        assert "answer-11" not in {h.source_id for h in windowed.hits}
