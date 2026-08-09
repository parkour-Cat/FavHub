import errno
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from favhub.domain import (
    ASSET_ROOTS,
    SAFE_ID,
    SUPPORTED_PLATFORMS,
    WINDOWS_RESERVED_NAMES,
    CapturedAsset,
    CapturedItem,
    validate_enrichment,
)

_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset({errno.EINVAL, errno.ENOTSUP, errno.ENOSYS})


def _fsync_directory(directory: Path) -> None:
    """Flush a POSIX directory entry, with no directory durability claim on Windows."""
    if os.name == "nt":
        # Windows gets file-level fsync plus os.replace only; directory-entry
        # power-loss durability is not claimed.
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


class SourceSnapshotError(ValueError):
    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        super().__init__(f"invalid source snapshot {path}: {reason}")


@dataclass(frozen=True, slots=True)
class StoredItem:
    directory: Path
    content_hash: str


class ItemStore:
    _REQUIRED_SOURCE_FIELDS = frozenset(
        {
            "schema_version",
            "platform",
            "source_id",
            "canonical_url",
            "title",
            "author",
            "published_at",
            "observed_at",
            "body",
            "collections",
            "extractor_version",
            "content_hash",
        }
    )

    def __init__(self, items_root: Path) -> None:
        self.items_root = items_root
        self._casefold_indexes: dict[str, dict[str, str]] = {}
        self._casefold_signatures: dict[str, tuple[bool, int, int, int]] = {}

    def write(self, item: CapturedItem) -> StoredItem:
        directory = self._item_directory(item.platform, item.source_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._remember_source_id(item.platform, item.source_id)
        snapshot = item.to_source_dict()
        # An enrichment block keyed to the same content survives the rewrite;
        # a content change drops it (its summarize task is superseded anyway).
        previous = self.read_source(item.platform, item.source_id)
        if previous is not None:
            enrichment = previous.get("enrichment")
            if (
                isinstance(enrichment, Mapping)
                and enrichment.get("input_hash") == item.content_hash
            ):
                snapshot["enrichment"] = dict(enrichment)
        source = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        content = self._render_content(snapshot)
        self._atomic_text(directory / "content.md", content)
        self._create_text_if_absent(directory / "notes.md", "# Notes\n")
        (directory / "assets").mkdir(exist_ok=True)
        for asset in item.assets:
            self._write_asset(directory, asset)
        self._remove_assets_no_longer_captured(directory, item)
        # source.json is written last: it is the durable commit point and its
        # presence signals a complete item. notes.md is never replaced above.
        self._atomic_text(directory / "source.json", source)
        return StoredItem(directory=directory, content_hash=item.content_hash)

    def _remove_assets_no_longer_captured(self, directory: Path, item: CapturedItem) -> None:
        """Drop generated files this capture did not produce.

        Publishing overwrites what it has and used to leave the rest, so an item
        that lost a file kept the old one. That is invisible in ``content.md``,
        which is rebuilt, and not invisible at all to search: ``transcript/`` is
        indexed on its own. A Bilibili video whose transcript was refused for
        belonging to another video came out of the refresh with a clean
        ``content.md`` and twelve kilobytes of the wrong video's words still in
        the index, which is the exact outcome the refusal existed to prevent.

        Only roots the capture claims to know the full contents of are walked,
        and only the mapper can make that claim: producing no OCR is the
        ordinary state of an X sync that was not asked for any, and pruning on
        it would erase every image description in the library. ``notes.md`` is
        the user's, lives outside every asset root, and is out of reach here.
        """
        kept = {Path(asset.relative_path).as_posix() for asset in item.assets}
        for root_name in item.authoritative_asset_roots:
            root = directory / root_name
            if not root.is_dir():
                continue
            self._ensure_not_reparse(root, "asset root")
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_dir():
                    if not any(path.iterdir()):
                        path.rmdir()
                    continue
                self._ensure_not_reparse(path, "stale asset")
                if path.relative_to(directory).as_posix() not in kept:
                    path.unlink()

    def _write_asset(self, directory: Path, asset: CapturedAsset) -> None:
        """Persist one restricted text asset inside the item, safely confined.

        The path was already validated on ``CapturedAsset``; this re-validates
        independently (defense in depth) and refuses symlinks/reparse points
        or any path escaping the item directory.
        """
        parts = asset.relative_path.split("/")
        if parts[0] not in ASSET_ROOTS or len(parts) < 2:
            raise ValueError(f"asset path must be under {ASSET_ROOTS}: {asset.relative_path}")
        for part in parts:
            self._validate_path_component("asset path component", part)
        root = directory.resolve()
        target = directory / Path(asset.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        probe = target.parent
        while True:
            if probe.exists():
                self._ensure_not_reparse(probe, "asset directory")
            if probe.resolve() == root:
                break
            if probe.parent == probe:
                raise ValueError(f"asset path escapes item directory: {target}")
            probe = probe.parent
        try:
            target.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"asset path escapes item directory: {target}") from exc
        if target.exists():
            self._ensure_not_reparse(target, "asset file")
        self._atomic_text(target, asset.text)

    def published_snapshot_matches(self, item: CapturedItem) -> bool:
        snapshot = self.read_source(item.platform, item.source_id)
        if snapshot is None:
            return False
        published_assets = sorted(
            (a["relative_path"], a["sha256"]) for a in snapshot.get("assets") or []
        )
        captured_assets = sorted((a.relative_path, a.sha256) for a in item.assets)
        return (
            snapshot["content_hash"] == item.content_hash
            and sorted(set(snapshot["collections"])) == sorted(set(item.collections))
            and snapshot["extractor_version"] == item.extractor_version
            and published_assets == captured_assets
        )

    def ensure_content_from_source(self, platform: str, source_id: str) -> bool:
        snapshot = self.read_source(platform, source_id)
        if snapshot is None:
            return False
        content_path = self._item_directory(platform, source_id) / "content.md"
        expected = self._render_content(snapshot)
        try:
            existing = content_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            existing = None
        if existing == expected:
            return False
        self._atomic_text(content_path, expected)
        return True

    def apply_enrichment(self, platform: str, source_id: str, enrichment: dict[str, Any]) -> None:
        """Attach a validated enrichment block and re-render content.md.

        The block never participates in content_hash; content.md stays
        deterministically rebuildable from source.json.
        """
        payload = validate_enrichment(enrichment)
        snapshot = self.read_source(platform, source_id)
        if snapshot is None:
            raise KeyError(f"unknown item: {platform}/{source_id}")
        snapshot["enrichment"] = payload
        directory = self._item_directory(platform, source_id)
        source = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        # Unlike write(), source.json goes first: the item already exists, and
        # content.md must stay rebuildable from source.json at every instant —
        # a crash after this write leaves a stale content.md that startup
        # reconciliation regenerates.
        self._atomic_text(directory / "source.json", source)
        self._atomic_text(directory / "content.md", self._render_content(snapshot))

    def read_source(self, platform: str, source_id: str) -> dict[str, Any] | None:
        path = self._source_path(platform, source_id)
        try:
            return self._read_source(path)
        except FileNotFoundError:
            return None

    def _item_directory(self, platform: str, source_id: str) -> Path:
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"item path platform is unsupported: {platform!r}")
        self._validate_path_component("platform", platform)
        self._validate_path_component("source_id", source_id)

        if self._ensure_not_reparse(self.items_root, "items root") and not self.items_root.is_dir():
            raise ValueError(f"item path items root is not a directory: {self.items_root}")
        platform_directory = self.items_root / platform
        if self._ensure_not_reparse(platform_directory, "platform directory") and not (
            platform_directory.is_dir()
        ):
            raise ValueError(f"item path platform is not a directory: {platform_directory}")
        casefold_index = self._casefold_index(platform, platform_directory)
        existing = casefold_index.get(source_id.casefold())
        if existing is not None and existing != source_id:
            raise ValueError(
                f"case-insensitive source_id collision: {existing!r} conflicts with {source_id!r}"
            )

        directory = platform_directory / source_id
        self._ensure_not_reparse(directory, "item directory")
        root = self.items_root.resolve()
        resolved_directory = directory.resolve()
        try:
            resolved_directory.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"item path escapes items root: {directory}") from exc
        return directory

    def _source_path(self, platform: str, source_id: str) -> Path:
        directory = self._item_directory(platform, source_id)
        source_path = directory / "source.json"
        self._ensure_not_reparse(source_path, "source.json")
        root = self.items_root.resolve()
        try:
            source_path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"item path escapes items root: {source_path}") from exc
        return source_path

    def _casefold_index(self, platform: str, platform_directory: Path) -> dict[str, str]:
        signature = self._path_signature(platform_directory)
        cached = self._casefold_indexes.get(platform)
        if cached is not None and self._casefold_signatures.get(platform) == signature:
            return cached
        _, index, signature = self._scan_platform_directory(platform_directory)
        self._casefold_indexes[platform] = index
        self._casefold_signatures[platform] = signature
        return index

    def _remember_source_id(self, platform: str, source_id: str) -> None:
        index = self._casefold_indexes.setdefault(platform, {})
        folded = source_id.casefold()
        existing = index.get(folded)
        if existing is not None and existing != source_id:
            raise ValueError(
                f"case-insensitive source_id collision: {existing!r} conflicts with {source_id!r}"
            )
        index[folded] = source_id
        self._casefold_signatures[platform] = self._path_signature(self.items_root / platform)

    def _scan_platform_directory(
        self, platform_directory: Path
    ) -> tuple[list[Path], dict[str, str], tuple[bool, int, int, int]]:
        signature = self._path_signature(platform_directory)
        if not self._ensure_not_reparse(platform_directory, "platform directory"):
            return [], {}, signature
        if not platform_directory.is_dir():
            return [], {}, signature

        source_directories: list[Path] = []
        index: dict[str, str] = {}
        for source_directory in sorted(platform_directory.iterdir()):
            if not self._ensure_not_reparse(source_directory, "item directory"):
                continue
            if not source_directory.is_dir():
                continue
            folded = source_directory.name.casefold()
            previous = index.get(folded)
            if previous is not None and previous != source_directory.name:
                raise ValueError(
                    "case-insensitive source_id collision: "
                    f"{previous!r} conflicts with {source_directory.name!r}"
                )
            index[folded] = source_directory.name
            source_directories.append(source_directory)
        return source_directories, index, self._path_signature(platform_directory)

    @staticmethod
    def _path_signature(path: Path) -> tuple[bool, int, int, int]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False, 0, 0, 0
        return (
            True,
            int(getattr(metadata, "st_mtime_ns", 0)),
            int(getattr(metadata, "st_ino", 0)),
            int(getattr(metadata, "st_size", 0)),
        )

    @staticmethod
    def _ensure_not_reparse(path: Path, kind: str) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if stat.S_ISLNK(metadata.st_mode) or path.is_junction() or bool(attributes & reparse_flag):
            raise ValueError(f"item path {kind} must not be a symlink or reparse point: {path}")
        return True

    @staticmethod
    def _validate_path_component(field: str, value: str) -> None:
        if not isinstance(value, str) or SAFE_ID.fullmatch(value) is None:
            raise ValueError(f"item path {field} must be a safe identifier")
        if (
            value in {".", ".."}
            or value.endswith(".")
            or value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        ):
            raise ValueError(f"item path {field} must not be a reserved name")

    def iter_sources(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        root_exists = self._ensure_not_reparse(self.items_root, "items root")
        if not root_exists:
            self._casefold_indexes.clear()
            self._casefold_signatures.clear()
            return snapshots
        if not self.items_root.is_dir():
            raise ValueError(f"item path items root is not a directory: {self.items_root}")
        self._casefold_indexes.clear()
        self._casefold_signatures.clear()
        for platform_directory in sorted(self.items_root.iterdir()):
            source_directories, index, signature = self._scan_platform_directory(platform_directory)
            platform = platform_directory.name
            self._casefold_indexes[platform] = index
            self._casefold_signatures[platform] = signature
            if not source_directories:
                continue
            for source_directory in source_directories:
                source_path = self._source_path(platform, source_directory.name)
                if not source_path.exists():
                    continue
                snapshots.append(self._read_source(source_path))
        return snapshots

    def iter_index_markdown(self, platform: str, source_id: str) -> list[tuple[str, str]]:
        """Read system-generated Markdown files from an item directory safely.

        Only regular Markdown files below the registered item directory are
        returned. Durable ``notes.md``, metadata, assets and reparse points are
        intentionally excluded. Results are stable, with ``content.md`` first.
        """
        directory = self._item_directory(platform, source_id)
        if not directory.exists():
            raise FileNotFoundError(directory)
        root = directory.resolve()
        files: list[Path] = []
        for entry in directory.rglob("*"):
            self._ensure_not_reparse(entry, "index entry")
        for path in directory.rglob("*.md"):
            self._ensure_not_reparse(path, "index markdown")
            if not path.is_file():
                continue
            relative = path.relative_to(directory)
            parts = tuple(part.casefold() for part in relative.parts)
            if parts[-1] == "notes.md" or "assets" in parts:
                continue
            allowed = (
                len(parts) == 1
                and parts[0] in {"content.md", "transcript.md", "ocr.md", "visual.md"}
            ) or (len(parts) >= 2 and parts[0] in {"transcript", "ocr"})
            if not allowed:
                continue
            try:
                path.resolve().relative_to(root)
            except ValueError as exc:
                raise ValueError(f"index markdown path escapes item directory: {path}") from exc
            files.append(path)
        files.sort(
            key=lambda path: (
                0 if path.name.casefold() == "content.md" and path.parent == directory else 1,
                path.relative_to(directory).as_posix().casefold(),
                path.relative_to(directory).as_posix(),
            )
        )
        return [
            (
                path.relative_to(directory).as_posix(),
                self._safe_read_index_text(path, root),
            )
            for path in files
        ]

    def _safe_read_index_text(self, path: Path, root: Path) -> str:
        current = path
        while True:
            self._ensure_not_reparse(current, "index path")
            if current == root:
                break
            if current.parent == current:
                raise ValueError(f"index path escapes item directory: {path}")
            current = current.parent
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"index path escapes item directory: {path}") from exc
        before = path.lstat()
        identity = self._file_identity(before)
        descriptor = os.open(path, os.O_RDONLY)
        try:
            opened = os.fstat(descriptor)
            if self._file_identity(opened) != identity:
                raise OSError(f"index file changed while opening: {path}")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                text = stream.read()
                after_fd = os.fstat(stream.fileno())
            if self._file_identity(after_fd) != identity:
                raise OSError(f"index file changed while reading: {path}")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._ensure_not_reparse(path, "index path")
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError(f"index path escapes item directory: {path}") from exc
        if self._file_identity(path.lstat()) != identity:
            raise OSError(f"index file was replaced while reading: {path}")
        return text

    @staticmethod
    def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(getattr(metadata, "st_mtime_ns", 0)),
            int(getattr(metadata, "st_file_attributes", 0)),
        )

    def index_fingerprint(self, platform: str, source_id: str) -> str:
        return self.fingerprint_index_markdown(self.iter_index_markdown(platform, source_id))

    @staticmethod
    def fingerprint_index_markdown(entries: list[tuple[str, str]]) -> str:
        digest = hashlib.sha256()
        for relative_path, text in entries:
            path_bytes = relative_path.encode("utf-8")
            content_bytes = text.encode("utf-8")
            digest.update(len(path_bytes).to_bytes(8, "big"))
            digest.update(path_bytes)
            digest.update(len(content_bytes).to_bytes(8, "big"))
            digest.update(content_bytes)
        return digest.hexdigest()

    @classmethod
    def _read_source(cls, path: Path) -> dict[str, Any]:
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceSnapshotError(path, str(exc)) from exc
        if not isinstance(decoded, dict):
            type_error = TypeError(f"expected object, got {type(decoded).__name__}")
            raise SourceSnapshotError(path, str(type_error)) from type_error
        missing = cls._REQUIRED_SOURCE_FIELDS.difference(decoded)
        if missing:
            missing_error = KeyError(", ".join(sorted(missing)))
            raise SourceSnapshotError(path, str(missing_error)) from missing_error
        cls._validate_source_snapshot(path, decoded)
        return decoded

    @staticmethod
    def _render_content(snapshot: dict[str, Any]) -> str:
        author = snapshot["author"] or "unknown"
        collections = json.dumps(sorted(set(snapshot["collections"])), ensure_ascii=False)
        rendered = (
            "---\n"
            f"platform: {json.dumps(snapshot['platform'])}\n"
            f"source_id: {json.dumps(snapshot['source_id'])}\n"
            f"canonical_url: {json.dumps(snapshot['canonical_url'])}\n"
            f"author: {json.dumps(author, ensure_ascii=False)}\n"
            f"published_at: {json.dumps(snapshot['published_at'])}\n"
            f"collections: {collections}\n"
            "---\n\n"
            f"# {snapshot['title']}\n\n{snapshot['body'].rstrip()}\n"
        )
        enrichment = snapshot.get("enrichment")
        if enrichment:
            tags_line = " · ".join(enrichment["tags"])
            rendered += f"\n## 摘要\n\n{enrichment['summary'].rstrip()}\n\n标签：{tags_line}\n"
        return rendered

    @classmethod
    def _validate_source_snapshot(cls, path: Path, snapshot: dict[str, Any]) -> None:
        schema_version = snapshot["schema_version"]
        if type(schema_version) is not int or schema_version != 1:
            cause = ValueError("schema_version must be 1")
            raise SourceSnapshotError(path, str(cause)) from cause

        platform = cls._require_string(path, snapshot, "platform")
        source_id = cls._require_string(path, snapshot, "source_id")
        canonical_url = cls._require_string(path, snapshot, "canonical_url")
        title = cls._require_string(path, snapshot, "title")
        body = cls._require_string(path, snapshot, "body")
        extractor_version = cls._require_string(path, snapshot, "extractor_version")
        content_hash = cls._require_string(path, snapshot, "content_hash")

        author_value = snapshot["author"]
        if author_value is not None and not isinstance(author_value, str):
            author_error = TypeError("author must be a string or null")
            raise SourceSnapshotError(path, str(author_error)) from author_error
        author = author_value

        collections_value = snapshot["collections"]
        if not isinstance(collections_value, list) or not all(
            isinstance(collection, str) for collection in collections_value
        ):
            collections_error = TypeError("collections must be a list of strings")
            raise SourceSnapshotError(path, str(collections_error)) from collections_error
        collections = tuple(collections_value)

        published_at = cls._parse_timestamp(path, "published_at", snapshot["published_at"])
        observed_at = cls._parse_timestamp(path, "observed_at", snapshot["observed_at"])

        if path.parent.parent.name != platform:
            cause = ValueError("platform does not match parent directory")
            raise SourceSnapshotError(path, str(cause)) from cause
        if path.parent.name != source_id:
            cause = ValueError("source_id does not match parent directory")
            raise SourceSnapshotError(path, str(cause)) from cause

        try:
            item = CapturedItem(
                platform=platform,
                source_id=source_id,
                canonical_url=canonical_url,
                title=title,
                author=author,
                published_at=published_at,
                observed_at=observed_at,
                body=body,
                collections=collections,
                extractor_version=extractor_version,
            )
        except ValueError as exc:
            raise SourceSnapshotError(path, str(exc)) from exc
        if item.content_hash != content_hash:
            cause = ValueError("content_hash does not match snapshot content")
            raise SourceSnapshotError(path, str(cause)) from cause

        # Optional M2C extension fields. They are not part of content_hash, so
        # they only need to be well-formed when present.
        platform_metadata = snapshot.get("platform_metadata")
        if platform_metadata is not None and not isinstance(platform_metadata, dict):
            metadata_error: Exception = TypeError("platform_metadata must be an object")
            raise SourceSnapshotError(path, str(metadata_error)) from metadata_error
        assets = snapshot.get("assets")
        if assets is not None:
            if not isinstance(assets, list):
                assets_error: Exception = TypeError("assets must be a list")
                raise SourceSnapshotError(path, str(assets_error)) from assets_error
            for descriptor in assets:
                if not isinstance(descriptor, dict):
                    descriptor_error: Exception = TypeError("asset descriptor must be an object")
                    raise SourceSnapshotError(path, str(descriptor_error)) from descriptor_error
                for key in ("relative_path", "media_type", "sha256"):
                    if not isinstance(descriptor.get(key), str):
                        field_error: Exception = TypeError(
                            f"asset descriptor {key} must be a string"
                        )
                        raise SourceSnapshotError(path, str(field_error)) from field_error
        enrichment = snapshot.get("enrichment")
        if enrichment is not None:
            try:
                validate_enrichment(enrichment)
            except ValueError as exc:
                raise SourceSnapshotError(path, str(exc)) from exc

    @staticmethod
    def _require_string(path: Path, snapshot: dict[str, Any], field: str) -> str:
        value = snapshot[field]
        if not isinstance(value, str):
            cause = TypeError(f"{field} must be a string")
            raise SourceSnapshotError(path, str(cause)) from cause
        return value

    @staticmethod
    def _parse_timestamp(path: Path, field: str, value: Any) -> datetime:
        if not isinstance(value, str):
            cause = TypeError(f"{field} must be a string")
            raise SourceSnapshotError(path, str(cause)) from cause
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceSnapshotError(path, f"{field} is not a valid timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timezone_error = ValueError(f"{field} must be timezone-aware")
            raise SourceSnapshotError(path, str(timezone_error)) from timezone_error
        return timestamp

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temporary = Path(temporary_name)
        try:
            try:
                stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _create_text_if_absent(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass
