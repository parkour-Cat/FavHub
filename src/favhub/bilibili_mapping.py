"""Map parsed Bilibili values into platform-neutral captured items.

This module is pure: it depends only on the domain types and the Bilibili
value objects. It does not read the network, cookies, or the filesystem. One
unique video becomes one :class:`CapturedItem`; folder names observed across
scopes are unioned into ``collections``; the description and normalized,
timestamped subtitle cues go into ``body``; the raw subtitle response and a
normalized transcript are emitted as restricted text assets.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from favhub.bilibili_models import (
    SOURCE_UNAVAILABLE,
    BilibiliCaptureError,
    BilibiliListEntry,
    BilibiliSubtitle,
    BilibiliVideo,
)
from favhub.domain import CapturedAsset, CapturedItem, isoformat, sha256_text

EXTRACTOR_VERSION = "bilibili-browser-v1"
_TRANSCRIPT_PATH = "transcript/0001.md"


@dataclass(frozen=True, slots=True)
class VideoObservation:
    bvid: str
    entry: BilibiliListEntry
    collections: tuple[str, ...]
    favorited_at: datetime | None = None


def deduplicate(
    pages_by_scope: Mapping[str, Sequence[BilibiliListEntry]],
    folder_names: Mapping[str, str],
) -> dict[str, VideoObservation]:
    """Collapse per-folder list entries into one observation per unique video.

    Insertion order is preserved (first scope that surfaced the video), and
    every folder name that contained the video is unioned into ``collections``.
    """
    collections_by_bvid: dict[str, set[str]] = {}
    entry_by_bvid: dict[str, BilibiliListEntry] = {}
    favorited_by_bvid: dict[str, datetime | None] = {}
    order: list[str] = []
    for scope_id, entries in pages_by_scope.items():
        name = folder_names.get(scope_id, scope_id)
        for entry in entries:
            if entry.bvid not in entry_by_bvid:
                entry_by_bvid[entry.bvid] = entry
                collections_by_bvid[entry.bvid] = set()
                favorited_by_bvid[entry.bvid] = entry.fav_time
                order.append(entry.bvid)
            collections_by_bvid[entry.bvid].add(name)
            # First-favorited semantics: the earliest fav_time wins.
            known = favorited_by_bvid[entry.bvid]
            if entry.fav_time is not None and (known is None or entry.fav_time < known):
                favorited_by_bvid[entry.bvid] = entry.fav_time
    return {
        bvid: VideoObservation(
            bvid=bvid,
            entry=entry_by_bvid[bvid],
            collections=tuple(sorted(collections_by_bvid[bvid])),
            favorited_at=favorited_by_bvid[bvid],
        )
        for bvid in order
    }


def map_captured_item(
    observation: VideoObservation,
    *,
    detail: BilibiliVideo | BilibiliCaptureError | None,
    subtitle: BilibiliSubtitle | BilibiliCaptureError | None = None,
    subtitle_raw: str | None = None,
    subtitle_source: str | None = None,
    subtitle_mismatch: str | None = None,
    subtitle_asked: str | None = None,
    observed_at: datetime,
    extractor_version: str = EXTRACTOR_VERSION,
) -> CapturedItem:
    entry = observation.entry
    bvid = observation.bvid

    if isinstance(detail, BilibiliVideo):
        title = detail.title
        author = detail.author
        author_mid = detail.author_mid
        description = detail.description
        cover_url = detail.cover_url
        published_at = detail.published_at
        source_status = "available"
        duration = detail.duration
    else:
        title = entry.title
        author = entry.author
        author_mid = entry.author_mid
        description = entry.intro
        cover_url = entry.cover_url
        published_at = entry.published_at
        source_status = (
            detail.code if isinstance(detail, BilibiliCaptureError) else SOURCE_UNAVAILABLE
        )
        duration = None

    assets: list[CapturedAsset] = []
    body_parts: list[str] = []
    if description.strip():
        body_parts.append(description.strip())

    if isinstance(subtitle, BilibiliSubtitle):
        subtitle_status = "available"
        body_parts.append(_subtitle_section(subtitle))
        transcript_text = _transcript_markdown(subtitle)
        assets.append(
            CapturedAsset(
                relative_path=_TRANSCRIPT_PATH,
                media_type="text/markdown",
                text=transcript_text,
                sha256=sha256_text(transcript_text),
            )
        )
        if subtitle_raw is not None:
            raw_path = f"assets/subtitles/{_safe_language(subtitle.language)}.json"
            assets.append(
                CapturedAsset(
                    relative_path=raw_path,
                    media_type="application/json",
                    text=subtitle_raw,
                    sha256=sha256_text(subtitle_raw),
                )
            )
    elif isinstance(subtitle, BilibiliCaptureError):
        subtitle_status = subtitle.code
    elif subtitle_mismatch is not None:
        # Not the same fact as having no transcript, and the more useful one:
        # this video has one and Bilibili offered another video's instead.
        subtitle_status = "wrong_video"
    else:
        subtitle_status = "unavailable"

    platform_metadata: dict[str, object] = {
        "source_status": source_status,
        "subtitle_status": subtitle_status,
    }
    if subtitle_source is not None and isinstance(subtitle, BilibiliSubtitle):
        # Which video a transcript came from, which the document itself does not
        # say. Recorded because transcripts have arrived here belonging to other
        # videos entirely, and nothing in the stored item could tell.
        platform_metadata["subtitle_source"] = subtitle_source
    if subtitle_mismatch is not None:
        # The name that was refused, so the refusal can be checked rather than
        # taken on faith.
        platform_metadata["subtitle_offered"] = subtitle_mismatch
    if subtitle_asked is not None and subtitle_mismatch is not None:
        # What the refusal was measured against. Without it a wrong_video cannot
        # separate "answered wrongly" from "asked wrongly with a cid the detail
        # response got wrong", and both end here looking identical.
        platform_metadata["subtitle_asked"] = subtitle_asked
    if observation.favorited_at is not None:
        platform_metadata["favorited_at"] = isoformat(observation.favorited_at)
    if cover_url is not None:
        platform_metadata["cover_url"] = cover_url
    if author_mid is not None:
        platform_metadata["author_mid"] = author_mid
    if duration is not None:
        platform_metadata["duration"] = duration

    return CapturedItem(
        platform="bilibili",
        source_id=bvid,
        canonical_url=f"https://www.bilibili.com/video/{bvid}",
        title=title,
        author=author,
        published_at=published_at,
        observed_at=observed_at,
        body="\n\n".join(body_parts),
        collections=observation.collections,
        extractor_version=extractor_version,
        platform_metadata=platform_metadata,
        assets=tuple(assets),
        # Every capture asks the player for a transcript, so producing none is a
        # finding about the video and not a gap in what was attempted: whatever
        # transcript is on disk is stale and must not go on being indexed.
        authoritative_asset_roots=("transcript", "assets"),
    )


def _subtitle_section(subtitle: BilibiliSubtitle) -> str:
    lines = [f"[{_format_clock(cue.start)}] {cue.content}" for cue in subtitle.cues]
    return f"## 字幕（{subtitle.language}）\n" + "\n".join(lines)


def _transcript_markdown(subtitle: BilibiliSubtitle) -> str:
    header = f"# Transcript（{subtitle.language}）\n\n"
    lines = [
        f"[{_format_clock_ms(cue.start)} --> {_format_clock_ms(cue.end)}] {cue.content}"
        for cue in subtitle.cues
    ]
    return header + "\n".join(lines) + "\n"


def _format_clock(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_clock_ms(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def _safe_language(language: str) -> str:
    cleaned = "".join(
        char for char in language if char.isascii() and (char.isalnum() or char in "._-")
    )
    cleaned = cleaned.strip(".")
    return cleaned or "subtitle"
