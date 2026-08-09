from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from favhub.database import Database
from favhub.domain import CapturedItem
from favhub.enrich_gateway import EnrichGateway
from favhub.enrichment_queue import EnrichmentQueue
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.sync_gateway import Rejection, SyncArgumentError


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[tuple[EnrichGateway, Database, ItemStore, LibraryModule]]:
    database = Database.open(tmp_path / "favhub.sqlite3")
    store = ItemStore(tmp_path / "items")
    queue = EnrichmentQueue(database)
    library = LibraryModule(database, store, queue)
    gateway = EnrichGateway(
        database, queue, library, store, indexer=ContentIndexer(database, store, queue)
    )
    try:
        yield gateway, database, store, library
    finally:
        database.close()


def _create_job(database: Database, job_id: str) -> None:
    timestamp = "2026-07-26T00:00:00Z"
    database.connection.execute(
        """INSERT INTO sync_jobs(id, mode, status, options_json, created_at, updated_at)
           VALUES (?, 'full', 'running', '{}', ?, ?)""",
        (job_id, timestamp, timestamp),
    )


def _item(body: str, source_id: str = "42") -> CapturedItem:
    return CapturedItem(
        platform="x",
        source_id=source_id,
        canonical_url=f"https://x.com/example/status/{source_id}",
        title="Saved post",
        author="example",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body=body,
        collections=(),
        extractor_version="fixture-v1",
    )


def _ingest(library: LibraryModule, database: Database, job_id: str, item: CapturedItem) -> None:
    _create_job(database, job_id)
    library.ingest_batch(job_id, "x", f"{job_id}-b", [item], True)


SUBMIT_FIELDS: dict[str, Any] = {
    "summary": "总结这条收藏的核心内容与关键术语。",
    "tags": ["retrieval", "笔记"],
    "contentType": "text",
    "provider": "agent",
    "model": "claude-fable-5",
}


def test_next_returns_null_when_queue_is_empty(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, _, _, _ = stack
    assert gateway.next({}) == {"task": None}


def test_next_claims_task_with_readable_content(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, _, library = stack
    item = _item("正文内容 hybrid retrieval 实践")
    _ingest(library, database, "job-1", item)

    payload = gateway.next({})
    task = payload["task"]
    assert task is not None
    assert task["platform"] == "x"
    assert task["source_id"] == "42"
    assert task["input_hash"] == item.content_hash
    assert task["title"] == "Saved post"
    assert task["canonical_url"].startswith("https://x.com/")
    contents = {entry["path"]: entry["text"] for entry in task["content"]}
    assert "hybrid retrieval" in contents["content.md"]
    assert task["truncated"] is False
    status = database.connection.execute(
        "SELECT status FROM enrichment_tasks WHERE id = ?", (task["task_id"],)
    ).fetchone()[0]
    assert status == "running"


def _ingest_platform(
    library: LibraryModule,
    database: Database,
    job_id: str,
    platform: str,
    source_id: str,
) -> CapturedItem:
    item = CapturedItem(
        platform=platform,
        source_id=source_id,
        canonical_url=f"https://example.com/{platform}/{source_id}",
        title=f"{platform} item",
        author="example",
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=datetime(2026, 7, 18, tzinfo=UTC),
        body="正文内容",
        collections=(),
        extractor_version="fixture-v1",
    )
    _create_job(database, job_id)
    library.ingest_batch(job_id, platform, f"{job_id}-b", [item], True)
    return item


def test_next_can_be_scoped_to_one_platform(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """Enrichment is billed per item, so a run has to be able to buy a slice.

    Bilibili is queued first here; without a scope it would be claimed first,
    and the only way to reach the cheap platform would be skipping items nobody
    meant to refuse.
    """
    gateway, database, _, library = stack
    _ingest_platform(library, database, "job-1", "bilibili", "BV1")
    _ingest_platform(library, database, "job-2", "x", "42")

    claimed = gateway.next({"platform": "x"})["task"]

    assert claimed is not None
    assert (claimed["platform"], claimed["source_id"]) == ("x", "42")
    # The unclaimed platform is untouched, not skipped.
    status = database.connection.execute(
        "SELECT status FROM enrichment_tasks WHERE platform='bilibili' AND kind='summarize'"
    ).fetchone()[0]
    assert status == "pending"


def test_a_scoped_claim_reports_empty_rather_than_reaching_past_its_scope(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, _, library = stack
    _ingest_platform(library, database, "job-1", "bilibili", "BV1")

    assert gateway.next({"platform": "x"}) == {"task": None}


def test_next_rejects_an_unknown_platform_and_unknown_arguments(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, _, _, _ = stack
    with pytest.raises(SyncArgumentError, match="platform"):
        gateway.next({"platform": "myspace"})
    with pytest.raises(SyncArgumentError, match="unknown argument"):
        gateway.next({"platfrom": "x"})


def test_submit_applies_and_completes(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, store, library = stack
    _ingest(library, database, "job-1", _item("body"))
    task = gateway.next({})["task"]

    result = gateway.submit({"taskId": task["task_id"], **SUBMIT_FIELDS})

    assert result == {"task_id": task["task_id"], "outcome": "applied"}
    snapshot = store.read_source("x", "42")
    assert snapshot is not None
    assert snapshot["enrichment"]["tags"] == ["retrieval", "笔记"]
    row = database.connection.execute(
        "SELECT content_type FROM items WHERE platform='x' AND source_id='42'"
    ).fetchone()
    assert row["content_type"] == "text"
    # Task no longer running: a second submit is rejected.
    with pytest.raises(ValueError, match="running"):
        gateway.submit({"taskId": task["task_id"], **SUBMIT_FIELDS})


def test_next_supersedes_obsolete_tasks_automatically(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, _, library = stack
    old = _item("v1")
    new = _item("v2")
    _ingest(library, database, "job-1", old)
    _ingest(library, database, "job-2", new)

    payload = gateway.next({})
    task = payload["task"]
    assert task is not None
    assert task["input_hash"] == new.content_hash
    statuses = {
        str(row["input_hash"]): str(row["status"])
        for row in database.connection.execute(
            "SELECT input_hash, status FROM enrichment_tasks WHERE kind='summarize'"
        )
    }
    assert statuses[old.content_hash] == "completed"
    assert statuses[new.content_hash] == "running"


def test_next_never_hands_out_an_item_the_platform_dropped(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """A tombstone has nothing to summarise, and no search can ever return it."""
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("a placeholder body", source_id="gone"))
    database.connection.execute(
        "UPDATE items SET access_status = 'unavailable' WHERE source_id = 'gone'"
    )
    database.connection.commit()

    assert gateway.next({})["task"] is None
    # Closed rather than left pending, so it does not resurface on every pass.
    status = database.connection.execute(
        "SELECT status FROM enrichment_tasks WHERE kind='summarize' AND source_id='gone'"
    ).fetchone()
    assert status is not None and str(status["status"]) == "completed"


def test_skip_records_error_and_keeps_task_retryable(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("body"))
    task = gateway.next({})["task"]

    result = gateway.skip(
        {"taskId": task["task_id"], "code": "generation_failed", "message": "模型输出为空"}
    )

    assert result["outcome"] == "retryable"
    row = database.connection.execute(
        "SELECT status, attempts, error FROM enrichment_tasks WHERE id = ?",
        (task["task_id"],),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert "generation_failed" in row["error"]


def test_unsupported_content_leaves_the_queue_instead_of_being_handed_back(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """One unsummarizable item used to stall every run behind it.

    skip() returned the task to pending, and the next claim takes the oldest
    pending task — which is the one just skipped. An agent following the
    documented loop never reaches item two. A bare t.co link with no text is
    the real case that found this.
    """
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("https://t.co/Uduj0nKC7s"))
    blocked = gateway.next({})["task"]

    result = gateway.skip(
        {
            "taskId": blocked["task_id"],
            "code": "content_unsupported",
            "message": "正文仅含一个短链",
        }
    )

    assert result["outcome"] == "declined"
    row = database.connection.execute(
        "SELECT status, error FROM enrichment_tasks WHERE id = ?",
        (blocked["task_id"],),
    ).fetchone()
    assert row["status"] == "declined"
    # The reason survives, and the queue moves on rather than looping.
    assert "content_unsupported" in row["error"]
    assert gateway.next({}) == {"task": None}


def test_a_declined_item_returns_when_its_content_changes(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """The verdict is about one version of the content, not the item forever."""
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("https://t.co/Uduj0nKC7s"))
    blocked = gateway.next({})["task"]
    gateway.skip(
        {"taskId": blocked["task_id"], "code": "content_unsupported", "message": "仅含短链"}
    )

    # The author edited the post; a new content_hash enqueues a new task.
    _ingest(library, database, "job-2", _item("现在有正文了，讲的是 hybrid retrieval"))

    revived = gateway.next({})["task"]
    assert revived is not None
    assert revived["task_id"] != blocked["task_id"]


def test_a_summary_no_shorter_than_its_source_is_rejected(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """The rule lived in the Skill and was broken twice in the next five items.

    17% of this library's existing summaries were at least as long as the body
    they summarised. That is a paraphrase billed as a summary: the reader saves
    nothing and the tokens were spent anyway.
    """
    gateway, database, _, library = stack
    body = "正文" * 200  # 400 characters, comfortably compressible
    _ingest(library, database, "job-1", _item(body))
    task = gateway.next({})["task"]

    with pytest.raises(ValueError, match="shorter"):
        gateway.submit(
            {
                "taskId": task["task_id"],
                **SUBMIT_FIELDS,
                "summary": "改写" * 200 + "还更长",
            }
        )

    # The task is untouched, so a corrected submission still lands.
    assert gateway.submit({"taskId": task["task_id"], **SUBMIT_FIELDS})["outcome"] == "applied"


def test_an_item_that_is_only_a_link_cannot_be_given_a_summary(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """Told to use whatever content exists, a model summarised items with none.

    A body of one t.co link produced "X post sharing a link", which says
    nothing, and "X post about AI, ads and apps", which was invented outright.
    Declining is the honest answer and now the only available one.
    """
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("https://t.co/Uduj0nKC7s"))
    task = gateway.next({})["task"]

    with pytest.raises(ValueError, match="no readable content"):
        gateway.submit(
            {
                "taskId": task["task_id"],
                **SUBMIT_FIELDS,
                "summary": "一条分享链接的推文。",
            }
        )

    assert (
        gateway.skip(
            {
                "taskId": task["task_id"],
                "code": "content_unsupported",
                "message": "仅含短链",
            }
        )["outcome"]
        == "declined"
    )


def test_an_applied_enrichment_is_searchable_without_a_separate_cli_run(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """Indexing ran only from the CLI, which cannot run while FavHub is open.

    Applying an enrichment rewrites content.md and queues an index task, so the
    summaries a run paid for stayed out of search until the whole application
    next happened to be shut down. This library reached a backlog of 180 that
    way, then 210, then 14 — each time only visible by going looking for it.
    """
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("检索用的原文内容。" * 30))
    task = gateway.next({})["task"]

    gateway.submit({"taskId": task["task_id"], **SUBMIT_FIELDS})

    pending = database.connection.execute(
        "SELECT COUNT(*) FROM enrichment_tasks WHERE kind = 'index_content' AND status = 'pending'"
    ).fetchone()[0]
    assert pending == 0
    chunks = database.connection.execute(
        "SELECT COUNT(*) FROM content_chunks WHERE platform = 'x' AND source_id = '42'"
    ).fetchone()[0]
    assert chunks > 0


def test_every_rule_is_raised_as_something_the_caller_will_be_told(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """The rules only work if the caller learns which one it broke.

    Each is enforced here but reported by the MCP layer, which replaces a plain
    ValueError with a constant to keep library content out of tool errors. A
    rule raised as one arrives as "Invalid tool argument.", and a run that hit
    the empty-body rule answered by varying the fields fifteen times and then
    reporting the tool as broken. Rejection is the type that reaches the caller.
    """
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("https://t.co/Uduj0nKC7s"))
    empty = gateway.next({})["task"]
    with pytest.raises(Rejection):
        gateway.submit({"taskId": empty["task_id"], **SUBMIT_FIELDS, "summary": "一条推文。"})
    gateway.skip({"taskId": empty["task_id"], "code": "content_unsupported", "message": "仅含短链"})

    _ingest(library, database, "job-2", _item("闲鱼副业实操记录，" * 30))
    chinese = gateway.next({})["task"]
    with pytest.raises(Rejection):
        gateway.submit({"taskId": chinese["task_id"], **SUBMIT_FIELDS, "tags": ["xianyuuyu"]})
    with pytest.raises(Rejection):
        gateway.submit({"taskId": chinese["task_id"], **SUBMIT_FIELDS, "summary": "改写" * 300})


def test_a_link_with_a_sentence_beside_it_is_still_summarisable(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """The test is what survives removing the url, not the body's length."""
    gateway, database, _, library = stack
    _ingest(
        library,
        database,
        "job-1",
        _item("多的不敢说，一个月几千零花钱还是很香的 https://t.co/vnHyz0wYEH"),
    )
    task = gateway.next({})["task"]

    result = gateway.submit(
        {
            "taskId": task["task_id"],
            **SUBMIT_FIELDS,
            "summary": "作者称该方法每月可带来几千元零花钱。",
            "tags": ["副业", "变现"],
        }
    )

    assert result["outcome"] == "applied"


def test_a_body_too_short_to_compress_is_not_held_to_the_rule(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """A dozen words is already its own summary; tags are the point there."""
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("#空投101"))
    task = gateway.next({})["task"]

    result = gateway.submit(
        {
            "taskId": task["task_id"],
            **SUBMIT_FIELDS,
            "summary": "一条关于空投入门的短推文，正文只有一个话题标签。",
        }
    )

    assert result["outcome"] == "applied"


def test_chinese_content_needs_at_least_one_chinese_tag(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """A cheap model transliterates instead of translating, and nobody searches that.

    46% of one measured batch came back as pinyin — 闲鱼 as "xianyuuyu", 副业 as
    "fuyeu" — misspelled often enough to fail even as pinyin. Tags are the whole
    reason to enrich a short item.
    """
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("我讲一下我在闲鱼的获客逻辑，这是个很吃执行的副业。"))
    task = gateway.next({})["task"]

    with pytest.raises(ValueError, match="Chinese"):
        gateway.submit(
            {
                "taskId": task["task_id"],
                **SUBMIT_FIELDS,
                "summary": "闲鱼获客逻辑简述。",
                "tags": ["xianyuuyu", "fuyeu", "dianshang"],
            }
        )

    assert (
        gateway.submit(
            {
                "taskId": task["task_id"],
                **SUBMIT_FIELDS,
                "summary": "闲鱼获客逻辑简述。",
                "tags": ["闲鱼", "副业", "电商"],
            }
        )["outcome"]
        == "applied"
    )


def test_english_tags_are_fine_on_english_content(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """The rule is about transliteration, not about banning ASCII."""
    gateway, database, _, library = stack
    _ingest(
        library,
        database,
        "job-1",
        _item("We removed about 80% of the Claude Code system prompt and it still works."),
    )
    task = gateway.next({})["task"]

    result = gateway.submit(
        {
            "taskId": task["task_id"],
            **SUBMIT_FIELDS,
            "summary": "A note on trimming the system prompt.",
            "tags": ["claude", "prompt", "context"],
        }
    )

    assert result["outcome"] == "applied"


def test_a_chinese_post_about_english_tools_keeps_one_chinese_tag(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    """One tag, not a majority: the looser threshold is what makes this pass."""
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("这两天用 Cursor 配 Claude 写代码，效率提升明显。"))
    task = gateway.next({})["task"]

    result = gateway.submit(
        {
            "taskId": task["task_id"],
            **SUBMIT_FIELDS,
            "summary": "记录 Cursor 搭配 Claude 的使用体验。",
            "tags": ["cursor", "claude", "编程"],
        }
    )

    assert result["outcome"] == "applied"


def test_next_truncates_oversized_content(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("长" * 120_000))
    task = gateway.next({})["task"]
    total = sum(len(entry["text"]) for entry in task["content"])
    assert task["truncated"] is True
    assert total <= 100_000


@pytest.mark.parametrize(
    "overrides",
    [
        {"summary": ""},
        {"tags": []},
        {"tags": "not-a-list"},
        {"tags": ["ok", 7]},
        {"contentType": "audio"},
        {"provider": "  "},
        {"model": ""},
        {"cookie": "secret"},
    ],
)
def test_submit_rejects_malformed_arguments(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
    overrides: dict[str, Any],
) -> None:
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("body"))
    task = gateway.next({})["task"]
    arguments = {"taskId": task["task_id"], **SUBMIT_FIELDS}
    arguments.update(overrides)
    with pytest.raises(SyncArgumentError):
        gateway.submit(arguments)


def test_submit_unknown_task_is_not_found(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, _, _, _ = stack
    with pytest.raises(KeyError):
        gateway.submit({"taskId": "no-such-task", **SUBMIT_FIELDS})


def test_skip_rejects_unknown_code(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, _, library = stack
    _ingest(library, database, "job-1", _item("body"))
    task = gateway.next({})["task"]
    with pytest.raises(SyncArgumentError):
        gateway.skip({"taskId": task["task_id"], "code": "made_up", "message": "x"})


def test_tags_are_normalized_lowercase_and_deduped(
    stack: tuple[EnrichGateway, Database, ItemStore, LibraryModule],
) -> None:
    gateway, database, store, library = stack
    _ingest(library, database, "job-1", _item("body"))
    task = gateway.next({})["task"]
    gateway.submit(
        {
            "taskId": task["task_id"],
            **{**SUBMIT_FIELDS, "tags": ["  RAG ", "rag", "视频剪辑"]},
        }
    )
    snapshot = store.read_source("x", "42")
    assert snapshot is not None
    assert snapshot["enrichment"]["tags"] == ["rag", "视频剪辑"]
