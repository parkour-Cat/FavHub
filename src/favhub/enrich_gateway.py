"""Typed gateway between MCP enrichment tool arguments and the local stack.

The gateway hands persistent ``summarize`` tasks to a browser-less Agent and
receives back Agent-generated summaries/tags/content types. It never performs
network access; the intelligent generation happens in the calling Agent's own
model, and FavHub only validates and persists the result.

Error contract mirrors the sync gateway: :class:`SyncArgumentError` marks a
malformed top-level argument (JSON-RPC invalid params); plain ``ValueError``
maps to the sanitized ``invalid_argument`` tool error; ``KeyError`` maps to
``not_found``.

The rules this gateway enforces raise :class:`Rejection` instead, so the caller
is told which rule it broke rather than that something was invalid. They are
refusals it can satisfy on the next submission, and it cannot do that blind.
"""

import re
from collections.abc import Mapping
from typing import Any

from favhub.database import Database
from favhub.domain import (
    ENRICHMENT_CONTENT_TYPES,
    ITEM_AVAILABLE,
    MAX_TAGS,
    SUPPORTED_PLATFORMS,
)
from favhub.enrichment_queue import EnrichmentQueue
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.sync_gateway import (
    Rejection,
    SyncArgumentError,
    _required_string,
    _sanitized_message,
)

SKIP_CODES = frozenset({"generation_failed", "content_unsupported"})

# Total characters of system markdown handed to the Agent per task; larger
# items are truncated and flagged so summaries note partial coverage.
MAX_TASK_CONTENT_CHARS = 100_000

# Below this a body may have nothing left to compress, so the summary is not
# held to being shorter. Above it, one that is not shorter bought nothing.
COMPRESSIBLE_BODY_CHARS = 200

# Text left after removing urls and the media list. The bar sits just above
# nothing on purpose: the bodies that produced invented summaries had zero
# characters left, while "#空投101" has seven and is a real, if terse, subject.
# Anything stricter would refuse content for being short rather than absent.
SUMMARISABLE_BODY_CHARS = 4

_URL = re.compile(r"https?://\S+")
_MEDIA_SECTION = re.compile(r"^##\s*(媒体|图片).*", re.MULTILINE | re.DOTALL)


def _readable_characters(body: str) -> int:
    """Body length once links and the media manifest are taken out.

    A bookmark whose whole text is a t.co link has nothing a summary could be
    about. Asked for one anyway, a model writes "X post sharing a link" — or
    worse, invents a subject for it, which is how "X post about AI, ads and
    apps" appeared under a body containing nothing but the url.
    """
    without_media = _MEDIA_SECTION.sub("", body)
    return len(_URL.sub("", without_media).strip())


# CJK ideographs, kana and hangul — the scripts whose words a model is tempted
# to transliterate rather than keep.
_CJK = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿가-힯]")

# Share of a body's non-space characters that must be CJK before its tags are
# expected to contain any.
_CJK_BODY_SHARE = 0.5


def _is_mostly_cjk(text: str) -> bool:
    dense = [character for character in text if not character.isspace()]
    if not dense:
        return False
    return len(_CJK.findall(text)) / len(dense) >= _CJK_BODY_SHARE


class EnrichGateway:
    def __init__(
        self,
        database: Database,
        queue: EnrichmentQueue,
        library: LibraryModule,
        store: ItemStore,
        indexer: ContentIndexer | None = None,
    ) -> None:
        self._database = database
        self._queue = queue
        self._library = library
        self._store = store
        self._indexer = indexer

    def next(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """Claim the next live summarize task, auto-completing superseded ones.

        An optional ``platform`` scopes the claim. Enrichment is the one part of
        FavHub whose cost is paid per item by the caller, so being able to spend
        it on the cheap platform first, and stop, is worth an argument.
        """
        unknown = sorted(set(arguments) - {"platform"})
        if unknown:
            raise SyncArgumentError(f"unknown argument: {unknown[0]}")
        platform = arguments.get("platform")
        if platform is not None and platform not in SUPPORTED_PLATFORMS:
            raise SyncArgumentError(f"platform must be one of {sorted(SUPPORTED_PLATFORMS)}")
        excluded: list[str] = []
        while True:
            task = self._queue.claim_next(
                kind="summarize", platform=platform, excluded_ids=excluded
            )
            if task is None:
                return {"task": None}
            row = self._database.connection.execute(
                "SELECT content_hash, access_status FROM items "
                "WHERE platform = ? AND source_id = ?",
                (task.platform, task.source_id),
            ).fetchone()
            if row is None or str(row["content_hash"]) != task.input_hash:
                # The item vanished or its content moved on; the task for the
                # current hash (if any) is a separate queue entry.
                self._queue.complete(task.id)
                excluded.append(task.id)
                continue
            if str(row["access_status"]) != ITEM_AVAILABLE:
                # A tombstone has nothing to summarise — the platform dropped it
                # before FavHub ever read it, so the body is a placeholder. Every
                # search path filters it out anyway, so a summary would cost
                # model budget to produce something nothing can ever return.
                self._queue.complete(task.id)
                excluded.append(task.id)
                continue
            snapshot = self._store.read_source(task.platform, task.source_id)
            if snapshot is None:
                self._queue.complete(task.id)
                excluded.append(task.id)
                continue
            content, truncated = self._task_content(task.platform, task.source_id)
            return {
                "task": {
                    "task_id": task.id,
                    "platform": task.platform,
                    "source_id": task.source_id,
                    "input_hash": task.input_hash,
                    "attempts": task.attempts,
                    "title": snapshot.get("title"),
                    "author": snapshot.get("author"),
                    "canonical_url": snapshot.get("canonical_url"),
                    "content": content,
                    "truncated": truncated,
                }
            }

    def submit(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        task_id = _required_string(arguments, "taskId")
        unknown = sorted(
            set(arguments) - {"taskId", "summary", "tags", "contentType", "provider", "model"}
        )
        if unknown:
            raise SyncArgumentError(f"unknown argument: {unknown[0]}")
        summary = _required_string(arguments, "summary")
        tags = _normalized_tags(arguments.get("tags"))
        # Gateway normalization (casefold + dedupe) is deliberately looser
        # than domain.validate_enrichment, which rejects duplicates after
        # this normalization has already run.
        content_type = arguments.get("contentType")
        if content_type not in ENRICHMENT_CONTENT_TYPES:
            raise SyncArgumentError(
                f"contentType must be one of {sorted(ENRICHMENT_CONTENT_TYPES)}"
            )
        provider = _required_string(arguments, "provider")
        model = _required_string(arguments, "model")
        self._reject_summary_that_saves_nothing(task_id, summary)
        self._reject_tags_that_no_one_would_search(task_id, tags)
        outcome = self._library.apply_enrichment(
            task_id,
            {
                "summary": summary,
                "tags": tags,
                "content_type": content_type,
                "provider": provider,
                "model": model,
            },
        )
        if outcome == "applied":
            self._index_what_was_just_rewritten()
        return {"task_id": task_id, "outcome": outcome}

    def _index_what_was_just_rewritten(self) -> None:
        """Fold the enrichment into the search index before returning.

        Applying an enrichment rewrites ``content.md`` and queues an index task,
        and nothing was draining that queue: indexing runs from the CLI, which
        refuses to start while FavHub holds the data root — which it does for as
        long as the window that just produced the enrichment is open. So the
        summaries a run spent real money on stayed invisible to search until the
        next time the whole application happened to be shut down. This library
        accumulated a backlog of 180 that way, then 210, then 14.

        The work is chunking and FTS for one item, which is why it can be done
        inline. Embeddings are a separate task and stay a batch job.
        """
        if self._indexer is None:
            return
        while self._indexer.index_next() is not None:
            pass

    def skip(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        task_id = _required_string(arguments, "taskId")
        code = _required_string(arguments, "code")
        if code not in SKIP_CODES:
            raise SyncArgumentError(f"code must be one of {sorted(SKIP_CODES)}")
        message = _sanitized_message(_required_string(arguments, "message"))
        # The two codes already meant different things; now they do different
        # things. A failed generation is worth another attempt. A judgement
        # that the content holds nothing to summarise is not, and requeueing it
        # hands the same task back on the very next claim.
        if code == "content_unsupported":
            self._queue.decline(task_id, f"{code}: {message}")
            return {"task_id": task_id, "outcome": "declined", "code": code}
        self._queue.fail(task_id, f"{code}: {message}")
        return {"task_id": task_id, "outcome": "retryable", "code": code}

    def _reject_summary_that_saves_nothing(self, task_id: str, summary: str) -> None:
        """A summary at least as long as its source is not a summary.

        Measured across this library, 17% of enriched items had one: the reader
        gains nothing and the tokens bought a paraphrase. The rule went into the
        Skill first and was broken twice in the next five items, which is why it
        is checked here instead of asked for.

        Only bodies long enough to compress are held to it. Under
        ``COMPRESSIBLE_BODY_CHARS`` there may be nothing to cut, and a post of a
        dozen words is already its own summary — the tags are what that item is
        being enriched for.
        """
        row = self._database.connection.execute(
            "SELECT platform, source_id FROM enrichment_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return
        snapshot = self._store.read_source(str(row["platform"]), str(row["source_id"]))
        body = str((snapshot or {}).get("body") or "")
        if _readable_characters(body) < SUMMARISABLE_BODY_CHARS:
            raise Rejection(
                "this item has no readable content to summarise — decline it with "
                "content_unsupported instead of describing the link"
            )
        if len(body) < COMPRESSIBLE_BODY_CHARS:
            return
        if len(summary) >= len(body):
            raise Rejection(
                "summary must be shorter than the content it summarises "
                f"({len(summary)} >= {len(body)} characters)"
            )

    def _reject_tags_that_no_one_would_search(self, task_id: str, tags: list[str]) -> None:
        """Chinese content needs at least one tag someone could actually type.

        A cheap model asked for tags on Chinese text will sometimes transliterate
        instead of translating: 闲鱼 comes back as "xianyuuyu", 副业 as "fuyeu".
        In one measured batch 46% of tags came back that way, misspelled often
        enough that they do not even work as pinyin. Tags are the whole point of
        enriching a short item, and nobody searches for those.

        The threshold is one tag, not a majority. The failure is categorical —
        every tag transliterated, no Chinese character anywhere — so one is
        enough to catch it, while a majority rule would punish a Chinese post
        whose subject really is Cursor, Claude and GitHub.
        """
        row = self._database.connection.execute(
            "SELECT platform, source_id FROM enrichment_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return
        snapshot = self._store.read_source(str(row["platform"]), str(row["source_id"]))
        body = str((snapshot or {}).get("body") or "")
        if not _is_mostly_cjk(body):
            return
        if not any(_CJK.search(tag) for tag in tags):
            raise Rejection(
                "content in Chinese needs at least one tag containing Chinese "
                "characters; transliterated tags are not searchable"
            )

    def _task_content(self, platform: str, source_id: str) -> tuple[list[dict[str, Any]], bool]:
        entries = self._store.iter_index_markdown(platform, source_id)
        remaining = MAX_TASK_CONTENT_CHARS
        content: list[dict[str, Any]] = []
        truncated = False
        for path, text in entries:
            if remaining <= 0:
                truncated = True
                break
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            remaining -= len(text)
            content.append({"path": path, "text": text})
        return content, truncated


def _normalized_tags(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise SyncArgumentError(f"tags must be a non-empty array of at most {MAX_TAGS} entries")
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        if not isinstance(tag, str) or not tag.strip():
            raise SyncArgumentError("tags must contain non-blank strings")
        cleaned = tag.strip().casefold()
        if cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    if len(normalized) > MAX_TAGS:
        raise SyncArgumentError(f"tags must contain at most {MAX_TAGS} unique entries")
    return normalized


__all__ = ["MAX_TASK_CONTENT_CHARS", "SKIP_CODES", "EnrichGateway"]
