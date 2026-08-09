"""Map Zhihu favorites into platform-neutral captured items.

Pure module. ``source_id`` is ``{type}-{id}`` (type prefix keeps answer and
article id spaces apart). The per-favorite ``created`` timestamp is the real
favorited time and feeds ``favorited_at`` without an estimate flag; a favorite
saved into several folders keeps the earliest one (bilibili semantics).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from favhub.domain import CapturedItem, isoformat
from favhub.html_text import render_text
from favhub.zhihu_models import ZhihuAnswer, ZhihuArticle, ZhihuFavorite, ZhihuOther

EXTRACTOR_VERSION = "zhihu-browser-v1"

__all__ = [
    "EXTRACTOR_VERSION",
    "ZhihuObservation",
    "deduplicate",
    "map_captured_item",
    "source_id_for",
]


@dataclass(frozen=True, slots=True)
class ZhihuObservation:
    favorite: ZhihuFavorite
    collections: tuple[str, ...]


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", value).strip(".")
    return cleaned or "unknown"


def source_id_for(content: ZhihuAnswer | ZhihuArticle | ZhihuOther) -> str:
    if isinstance(content, ZhihuAnswer):
        return f"answer-{content.answer_id}"
    if isinstance(content, ZhihuArticle):
        return f"article-{content.article_id}"
    # Degrade types come straight from the API; keep the id SAFE_ID-clean.
    return f"{_safe_component(content.type_raw)}-{_safe_component(content.item_id)}"


def deduplicate(
    favorites_by_scope: Mapping[str, Sequence[ZhihuFavorite]],
    folder_names: Mapping[str, str],
) -> dict[str, ZhihuObservation]:
    observations: dict[str, ZhihuObservation] = {}
    for scope_id, favorites in favorites_by_scope.items():
        folder = folder_names.get(scope_id, scope_id)
        for favorite in favorites:
            key = source_id_for(favorite.content)
            existing = observations.get(key)
            if existing is None:
                observations[key] = ZhihuObservation(favorite, (folder,))
                continue
            collections = existing.collections
            if folder not in collections:
                collections = tuple(sorted((*collections, folder)))
            kept = existing.favorite
            if favorite.favorited_at < kept.favorited_at:
                kept = replace(kept, favorited_at=favorite.favorited_at)
            observations[key] = ZhihuObservation(kept, collections)
    return observations


def map_captured_item(
    favorite: ZhihuFavorite,
    *,
    collection_titles: Sequence[str],
    observed_at: datetime,
    extractor_version: str = EXTRACTOR_VERSION,
) -> CapturedItem:
    content = favorite.content
    if isinstance(content, ZhihuOther):
        type_raw = content.type_raw
    else:
        type_raw = "answer" if isinstance(content, ZhihuAnswer) else "article"
    metadata: dict[str, object] = {
        "favorited_at": isoformat(favorite.favorited_at),
        "content_type_raw": type_raw,
    }

    if isinstance(content, ZhihuOther):
        title = content.title or f"{content.type_raw} {content.item_id}"
        url = content.url or f"https://www.zhihu.com/{content.type_raw}/{content.item_id}"
        body = content.excerpt or ""
        published_at = observed_at
        author = content.author
    else:
        text, images = render_text(content.html)
        body = text
        if isinstance(content, ZhihuAnswer) and content.video_title is not None:
            section = f"## 视频\n\n{content.video_title}"
            body = f"{body}\n\n{section}" if body else section
            metadata["video_title"] = content.video_title
        if images:
            body = body + "\n\n## 图片\n\n" + "\n".join(f"- {url}" for url in images)
        published_at = content.created_at or observed_at
        author = content.author
        if content.voteup_count is not None:
            metadata["voteup_count"] = content.voteup_count
        if content.updated_at is not None:
            metadata["updated_at"] = isoformat(content.updated_at)
        if isinstance(content, ZhihuAnswer):
            title = content.question_title
            url = content.url
            if content.question_id is not None:
                metadata["question_id"] = content.question_id
        else:
            title = content.title
            url = content.url
            if content.image_url is not None:
                metadata["image_url"] = content.image_url

    return CapturedItem(
        platform="zhihu",
        source_id=source_id_for(content),
        canonical_url=url,
        title=title,
        author=author,
        published_at=published_at,
        observed_at=observed_at,
        body=body,
        collections=tuple(collection_titles),
        extractor_version=extractor_version,
        platform_metadata=metadata,
    )
