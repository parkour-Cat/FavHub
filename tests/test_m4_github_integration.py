"""End-to-end M4 coverage: fake starred API -> parsers -> MCP -> retrieval."""

from datetime import UTC, datetime
from pathlib import Path

from favhub.application import Application
from favhub.domain import isoformat
from favhub.github_mapping import map_captured_item, safe_source_id
from favhub.github_parsers import parse_starred_page
from favhub.retrieval import SearchRequest
from favhub.sync_gateway import SyncGateway

OBSERVED = datetime(2026, 7, 26, tzinfo=UTC)


def _star(full_name: str, starred_at: str, description: str) -> dict:
    owner, name = full_name.split("/")
    return {
        "starred_at": starred_at,
        "repo": {
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "description": description,
            "language": "Python",
            "topics": ["agent", "rag"],
            "owner": {"login": owner},
            "default_branch": "main",
            "created_at": "2025-01-01T00:00:00Z",
            "pushed_at": "2026-07-01T00:00:00Z",
            "stargazers_count": 42,
            "archived": False,
            "fork": False,
        },
    }


def _to_mcp(item) -> dict:
    return {
        "sourceId": item.source_id,
        "canonicalUrl": item.canonical_url,
        "title": item.title,
        "author": item.author,
        "publishedAt": isoformat(item.published_at),
        "observedAt": isoformat(item.observed_at),
        "body": item.body,
        "collections": [],
        "extractorVersion": item.extractor_version,
        "platformMetadata": item.platform_metadata,
    }


def test_github_collection_end_to_end(tmp_path: Path) -> None:
    pages = [
        [
            _star("alpha/retrieval-kit", "2026-07-20T10:00:00Z", "检索工具箱 hybrid search"),
            _star("beta/agent-loop", "2026-06-01T09:00:00Z", "agent runtime loop"),
        ],
        [],
    ]
    with Application.open(tmp_path / "root") as app:
        gateway = SyncGateway(app.sync)
        job = gateway.start({"platform": "github", "mode": "full"})
        collected = []
        for raw_page in pages:
            stars = parse_starred_page(raw_page)
            if not stars:
                break
            collected.extend(stars)
        items = [
            map_captured_item(
                star,
                readme_text="# Kit\n\nusage docs 检索示例"
                if "retrieval" in star.full_name
                else None,
                observed_at=OBSERVED,
            )
            for star in collected
        ]
        receipt = gateway.submit_batch(
            {
                "jobId": job["job_id"],
                "platform": "github",
                "batchId": "b-0000",
                "items": [_to_mcp(i) for i in items],
            }
        )
        assert receipt["added"] == 2
        gateway.finish(
            {
                "jobId": job["job_id"],
                "platform": "github",
                "observedEnd": True,
                "maxScanReached": False,
                "frontierIds": [safe_source_id(s.full_name) for s in collected][:20],
            }
        )

        # Column lift: real starred_at became favorited_at.
        rows = dict(
            app.database.connection.execute(
                "SELECT source_id, favorited_at FROM items WHERE platform='github'"
            ).fetchall()
        )
        assert rows["alpha__retrieval-kit"] == "2026-07-20T10:00:00Z"

        # Frontier established; incremental discovers only the new star.
        resumed = gateway.start({"platform": "github", "mode": "incremental"})
        assert resumed["frontiers"]["github"][0] == "alpha__retrieval-kit"
        fresh_page = [
            _star("gamma/new-shiny", "2026-07-25T08:00:00Z", "brand new"),
            *pages[0],
        ]
        frontier = set(resumed["frontiers"]["github"])
        stars = []
        for star in parse_starred_page(fresh_page):
            if safe_source_id(star.full_name) in frontier:
                break
            stars.append(star)
        assert [s.full_name for s in stars] == ["gamma/new-shiny"]
        receipt2 = gateway.submit_batch(
            {
                "jobId": resumed["job_id"],
                "platform": "github",
                "batchId": "b-0000",
                "items": [
                    _to_mcp(map_captured_item(s, readme_text=None, observed_at=OBSERVED))
                    for s in stars
                ],
            }
        )
        assert receipt2["added"] == 1 and receipt2["duplicates"] == 0

        # Index and retrieve README content with citations + favorited filter.
        while app.indexer.index_next() is not None:
            pass
        hits = app.retrieval.search(SearchRequest("检索示例", limit=5))
        assert hits.found is True
        top = hits.hits[0]
        assert top.platform == "github" and top.source_id == "alpha__retrieval-kit"
        assert top.citation_id.startswith("favhub:github/alpha__retrieval-kit#chunk-")

        windowed = app.retrieval.search(
            SearchRequest(
                "agent",
                platforms=("github",),
                favorited_since=datetime(2026, 7, 1, tzinfo=UTC),
                limit=10,
            )
        )
        assert {h.source_id for h in windowed.hits} <= {
            "alpha__retrieval-kit",
            "gamma__new-shiny",
        }
        assert "beta__agent-loop" not in {h.source_id for h in windowed.hits}
