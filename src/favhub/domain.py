import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")
SUPPORTED_PLATFORMS = frozenset({"bilibili", "github", "x", "zhihu"})
# Platforms whose saved items live in user-created folders, each keeping its own
# sync frontier. X and GitHub have a single list, so scope arguments are rejected.
SCOPED_PLATFORMS = frozenset({"bilibili", "zhihu"})
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)

# Restricted text assets (e.g. raw subtitle responses, normalized transcripts)
# are the only non-source files a caller may hand to the store. They must be
# small UTF-8 text rooted under one of these directories inside the item.
MAX_ASSET_BYTES = 2 * 1024 * 1024
ASSET_ROOTS = ("assets", "transcript", "ocr")
TEXT_ASSET_MEDIA_TYPES = frozenset(
    {
        "application/json",
        "text/plain",
        "text/markdown",
        "text/vtt",
    }
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# What `items.access_status` means. Three states, and two of them are about
# entirely different worlds — which is why they are spelled out here rather
# than left as literals at each call site.
#
# `missing` is FavHub's own problem: the local snapshot went away, and finding
# it again repairs the item. `unavailable` is a fact about the platform: the
# source is gone, and no amount of local health refutes it. Startup maintenance
# repairs the first and must never overwrite the second — it did, once, and
# quietly resurrected 238 tombstones on the next command.
ITEM_AVAILABLE = "available"
ITEM_MISSING = "missing"
ITEM_UNAVAILABLE = "unavailable"

# Agent-generated enrichment (summary/tags/content type) recorded per item.
# The block is validated on read and write but never participates in
# content_hash, so re-enrichment cannot trigger re-capture.
ENRICHMENT_CONTENT_TYPES = frozenset({"text", "video", "image", "mixed"})
MAX_SUMMARY_CHARS = 2000
MAX_TAGS = 8
MAX_TAG_CHARS = 40
MAX_ENRICHMENT_FIELD_CHARS = 200
_ENRICHMENT_KEYS = frozenset(
    {"summary", "tags", "content_type", "provider", "model", "generated_at", "input_hash"}
)


def validate_enrichment(payload: Any) -> dict[str, Any]:
    """Validate an enrichment block and return a defensive copy."""
    if not isinstance(payload, dict):
        raise ValueError("enrichment must be an object")
    unknown = sorted(set(payload) - _ENRICHMENT_KEYS)
    if unknown:
        raise ValueError(f"unknown enrichment field: {unknown[0]}")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("enrichment summary must be a non-blank string")
    if len(summary) > MAX_SUMMARY_CHARS:
        raise ValueError(f"enrichment summary must be at most {MAX_SUMMARY_CHARS} characters")
    tags = payload.get("tags")
    if not isinstance(tags, list) or not 1 <= len(tags) <= MAX_TAGS:
        raise ValueError(f"enrichment tags must contain 1 to {MAX_TAGS} entries")
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            raise ValueError("enrichment tags must be non-blank strings")
        if len(tag) > MAX_TAG_CHARS:
            raise ValueError(f"enrichment tags must be at most {MAX_TAG_CHARS} characters")
    if len({tag.casefold() for tag in tags}) != len(tags):
        raise ValueError("enrichment tags must be unique")
    content_type = payload.get("content_type")
    if content_type not in ENRICHMENT_CONTENT_TYPES:
        raise ValueError(
            f"enrichment content_type must be one of {sorted(ENRICHMENT_CONTENT_TYPES)}"
        )
    for field_name in ("provider", "model", "input_hash"):
        value = payload.get(field_name)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > MAX_ENRICHMENT_FIELD_CHARS
        ):
            raise ValueError(
                f"enrichment {field_name} must be a non-blank string of at most "
                f"{MAX_ENRICHMENT_FIELD_CHARS} characters"
            )
    generated_at = payload.get("generated_at")
    if not isinstance(generated_at, str):
        raise ValueError("enrichment generated_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("enrichment generated_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("enrichment generated_at must include a timezone")
    return {
        "summary": summary,
        "tags": list(tags),
        "content_type": content_type,
        "provider": payload["provider"],
        "model": payload["model"],
        "generated_at": generated_at,
        "input_hash": payload["input_hash"],
    }


def _validate_safe_component(field: str, value: str) -> None:
    if SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe identifier")
    if (
        value in {".", ".."}
        or value.endswith(".")
        or (value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES)
    ):
        raise ValueError(f"{field} must not be a reserved path name")


class SyncMode(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"


class CaptureStatus(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CapturedAsset:
    """A small restricted text file to persist alongside an item's source.

    Contents never leave memory as anything but validated UTF-8 text. The
    relative path is confined to the item's ``assets/`` or ``transcript/``
    subtrees; absolute paths, traversal, and unsafe components are rejected.
    """

    relative_path: str
    media_type: str
    text: str
    sha256: str

    def __post_init__(self) -> None:
        path = self.relative_path
        if not isinstance(path, str) or not path:
            raise ValueError("asset relative_path must be a non-empty string")
        if path.startswith(("/", "\\")) or "\\" in path or ":" in path:
            raise ValueError("asset relative_path must be a relative POSIX path")
        parts = path.split("/")
        if parts[0] not in ASSET_ROOTS:
            raise ValueError(f"asset relative_path must be under one of {ASSET_ROOTS}")
        if len(parts) < 2:
            raise ValueError("asset relative_path must include a file name")
        for part in parts:
            _validate_safe_component("asset path component", part)
        if self.media_type not in TEXT_ASSET_MEDIA_TYPES:
            raise ValueError(f"asset media_type must be one of {sorted(TEXT_ASSET_MEDIA_TYPES)}")
        if not isinstance(self.text, str):
            raise ValueError("asset text must be a string")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("asset text must be valid UTF-8") from exc
        if len(encoded) > MAX_ASSET_BYTES:
            raise ValueError("asset text exceeds the maximum size")
        if self.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("asset sha256 does not match text")

    def descriptor(self) -> dict[str, Any]:
        """A content-free descriptor for embedding in ``source.json``."""
        return {
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": len(self.text.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class CapturedItem:
    platform: str
    source_id: str
    canonical_url: str
    title: str
    author: str | None
    published_at: datetime
    observed_at: datetime
    body: str
    collections: tuple[str, ...]
    extractor_version: str
    platform_metadata: dict[str, Any] | None = None
    assets: tuple[CapturedAsset, ...] = ()
    # Asset roots this capture knows the full contents of, so a file it did not
    # produce can be removed rather than left to be indexed forever.
    #
    # Emptiness is not the signal. A Bilibili capture always attempts the
    # transcript, so producing none means the video has none now and the old one
    # must go. An X capture only produces OCR when it was asked to, so producing
    # none is the ordinary case and deleting on it would erase every image
    # description in the library on the next sync. Only the mapper knows which
    # of the two it is, so only the mapper may say.
    authoritative_asset_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {self.platform}")
        if not SAFE_ID.fullmatch(self.source_id):
            raise ValueError(
                "source_id must contain only letters, digits, dot, underscore, or dash"
            )
        source_basename = self.source_id.split(".", 1)[0].upper()
        if (
            self.source_id in {".", ".."}
            or self.source_id.endswith(".")
            or source_basename in WINDOWS_RESERVED_NAMES
        ):
            raise ValueError("source_id must not be a reserved path name")
        parsed = urlparse(self.canonical_url)
        try:
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("canonical_url must be an absolute HTTP(S) URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or any(
                char.isspace() or unicodedata.category(char) == "Cc" for char in self.canonical_url
            )
        ):
            raise ValueError("canonical_url must be an absolute HTTP(S) URL")
        if (
            self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("timestamps must be timezone-aware")
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if not self.extractor_version.strip():
            raise ValueError("extractor_version must not be blank")
        unknown_roots = [root for root in self.authoritative_asset_roots if root not in ASSET_ROOTS]
        if unknown_roots:
            raise ValueError(f"authoritative_asset_roots must be under one of {ASSET_ROOTS}")
        if self.platform_metadata is not None:
            if not isinstance(self.platform_metadata, dict) or not all(
                isinstance(key, str) for key in self.platform_metadata
            ):
                raise ValueError("platform_metadata must be a mapping with string keys or null")
            try:
                json.dumps(self.platform_metadata, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise ValueError("platform_metadata must be JSON-serializable") from exc
        if not isinstance(self.assets, tuple):
            raise ValueError("assets must be a tuple of CapturedAsset")
        seen_paths: set[str] = set()
        for asset in self.assets:
            if not isinstance(asset, CapturedAsset):
                raise ValueError("assets must contain CapturedAsset values")
            if asset.relative_path in seen_paths:
                raise ValueError(f"duplicate asset path: {asset.relative_path}")
            seen_paths.add(asset.relative_path)

    @property
    def content_hash(self) -> str:
        payload = {
            "author": self.author,
            "body": self.body,
            "canonical_url": self.canonical_url,
            "platform": self.platform,
            "published_at": isoformat(self.published_at),
            "source_id": self.source_id,
            "title": self.title,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_source_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": 1,
            "platform": self.platform,
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "author": self.author,
            "published_at": isoformat(self.published_at),
            "observed_at": isoformat(self.observed_at),
            "body": self.body,
            "collections": sorted(set(self.collections)),
            "extractor_version": self.extractor_version,
            "content_hash": self.content_hash,
        }
        # Extension fields are emitted only when present so base items keep a
        # byte-identical snapshot and are excluded from ``content_hash`` (the
        # subtitle text itself lives in ``body`` and is therefore hashed).
        if self.platform_metadata:
            payload["platform_metadata"] = dict(self.platform_metadata)
        if self.assets:
            payload["assets"] = [asset.descriptor() for asset in self.assets]
        return payload
