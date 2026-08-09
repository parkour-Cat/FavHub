import argparse
import json
import os
import shutil
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from favhub.application import Application
from favhub.chrome_setup import FakeRegistry, Registry, WindowsRegistry
from favhub.config import InstallPaths, persisted_data_root
from favhub.doctor import run_doctor, running_favhub_pid
from favhub.domain import CapturedItem, SyncMode
from favhub.embedding_service import (
    DEFAULT_MODEL_LICENSE,
    EmbeddingBuildProgress,
    EmbeddingService,
)
from favhub.github_sync import TOKEN_ENV, run_sync
from favhub.retrieval import (
    GetItemRequest,
    ReindexRequest,
    RetrievalMode,
    RetrievalService,
    SearchRequest,
)
from favhub.root_lock import DataRootBusyError
from favhub.setup_service import AgentHost, run_command, run_setup, uninstall
from favhub.sync_module import StartSyncRequest

_CAPTURED_ITEM_FIELDS = frozenset(
    {
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
    }
)


def parse_datetime(value: Any, *, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _nonblank_query(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("query must not be blank")
    return value


def _search_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= limit <= 50:
        raise argparse.ArgumentTypeError("limit must be between 1 and 50")
    return limit


def _positive_max_items(value: str) -> int:
    try:
        max_items = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max-items must be a positive integer") from exc
    if max_items <= 0:
        raise argparse.ArgumentTypeError("max-items must be a positive integer")
    return max_items


def captured_item(value: Any, *, index: int = 0) -> CapturedItem:
    if not isinstance(value, dict):
        raise ValueError(f"fixture item {index} must be a JSON object")
    missing = _CAPTURED_ITEM_FIELDS.difference(value)
    if missing:
        fields = ", ".join(sorted(missing))
        raise ValueError(f"fixture item {index} is missing required fields: {fields}")
    collections = value["collections"]
    if not isinstance(collections, list) or not all(
        isinstance(collection, str) for collection in collections
    ):
        raise ValueError(f"fixture item {index} collections must be a list of strings")
    for field in (
        "platform",
        "source_id",
        "canonical_url",
        "title",
        "body",
        "extractor_version",
    ):
        if not isinstance(value[field], str):
            raise ValueError(f"fixture item {index} {field} must be a string")
    if value["author"] is not None and not isinstance(value["author"], str):
        raise ValueError(f"fixture item {index} author must be a string or null")
    try:
        return CapturedItem(
            platform=value["platform"],
            source_id=value["source_id"],
            canonical_url=value["canonical_url"],
            title=value["title"],
            author=value["author"],
            published_at=parse_datetime(value["published_at"], field="published_at"),
            observed_at=parse_datetime(value["observed_at"], field="observed_at"),
            body=value["body"],
            collections=tuple(collections),
            extractor_version=value["extractor_version"],
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid fixture item {index}: {exc}") from exc


def load_fixture(path: Path) -> list[CapturedItem]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"unable to read fixture {path}: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"fixture {path} is not valid JSON") from exc
    if not isinstance(payload, list):
        raise ValueError(f"fixture {path} must contain a JSON array")
    if not payload:
        raise ValueError(f"fixture {path} must contain at least one item")
    return [captured_item(item, index=index) for index, item in enumerate(payload)]


def _add_search_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--platform", dest="platforms", action="append")
    parser.add_argument("--content-type", dest="content_types", action="append")
    parser.add_argument("--collection", dest="collections", action="append")
    parser.add_argument("--published-since")
    parser.add_argument("--published-until")
    parser.add_argument("--favorited-since")
    parser.add_argument("--favorited-until")
    parser.add_argument("--limit", type=_search_limit, default=10)
    parser.add_argument(
        "--retrieval-mode",
        choices=[mode.value for mode in RetrievalMode],
        default=RetrievalMode.AUTO.value,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="favhub")
    # Optional at the parser level: setup/doctor/uninstall-browser work
    # without one, and data commands fall back to the persisted install root.
    parser.add_argument("--root", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup")
    setup.add_argument(
        "--replace",
        action="store_true",
        help="overwrite an existing MCP entry named favhub that FavHub did not write",
    )
    setup.add_argument(
        "--agent",
        action="append",
        choices=["codex", "claude"],
        help="limit setup to these Agent hosts (default: whichever are installed)",
    )
    commands.add_parser("doctor")
    commands.add_parser("uninstall-browser")

    import_fixture = commands.add_parser("import-fixture")
    import_fixture.add_argument("fixture", type=Path)
    import_fixture.add_argument("--mode", choices=[mode.value for mode in SyncMode], required=True)
    import_fixture.add_argument("--published-since")
    import_fixture.add_argument("--published-until")
    import_fixture.add_argument("--max-scan-items", type=int)

    status = commands.add_parser("status")
    status.add_argument("job_id", nargs="?")

    search = commands.add_parser("search")
    search.add_argument("query", type=_nonblank_query)
    _add_search_filters(search)

    search_batch = commands.add_parser("search-batch")
    search_batch.add_argument(
        "--query", dest="queries", action="append", required=True, type=_nonblank_query
    )
    _add_search_filters(search_batch)

    get_item = commands.add_parser("get-item")
    get_item.add_argument("platform")
    get_item.add_argument("source_id")
    get_item.add_argument("--include-content", action="store_true")

    commands.add_parser("collections")

    reindex = commands.add_parser("reindex")
    reindex.add_argument("--force", action="store_true")

    github = commands.add_parser("github-sync")
    github.add_argument("--user", required=True)
    github.add_argument("--mode", choices=[mode.value for mode in SyncMode], default="incremental")
    github.add_argument("--max-scan-items", type=_positive_max_items)

    commands.add_parser("enrich-backfill")

    enrich_redo = commands.add_parser("enrich-redo")
    redo_selector = enrich_redo.add_mutually_exclusive_group(required=True)
    redo_selector.add_argument("--model")
    redo_selector.add_argument("--declined", action="store_true")

    commands.add_parser("favtime-backfill")

    commands.add_parser("access-backfill")

    commands.add_parser("collections-backfill")

    embeddings = commands.add_parser("embeddings")
    embedding_commands = embeddings.add_subparsers(dest="embedding_command", required=True)
    embedding_commands.add_parser("init")
    embedding_build = embedding_commands.add_parser("build")
    embedding_build.add_argument("--max-items", type=_positive_max_items)
    embedding_build.add_argument("--force", action="store_true")
    embedding_build.add_argument("--quiet", action="store_true")
    return parser


def _import_fixture(arguments: argparse.Namespace) -> dict[str, Any]:
    items = load_fixture(arguments.fixture)
    grouped: dict[str, list[CapturedItem]] = defaultdict(list)
    for item in items:
        grouped[item.platform].append(item)

    published_since = (
        parse_datetime(arguments.published_since, field="published_since")
        if arguments.published_since is not None
        else None
    )
    published_until = (
        parse_datetime(arguments.published_until, field="published_until")
        if arguments.published_until is not None
        else None
    )
    request = StartSyncRequest(
        platforms=tuple(sorted(grouped)),
        mode=SyncMode(arguments.mode),
        published_since=published_since,
        published_until=published_until,
        max_scan_items=arguments.max_scan_items,
    )

    with Application.open(arguments.root) as application:
        started = application.sync.start_sync(request)
        multi_platform = len(grouped) > 1
        try:
            for platform in sorted(grouped):
                platform_items = grouped[platform]
                scan_items = _items_through_frontier(
                    platform_items,
                    started.frontiers[platform],
                )
                idempotency_key = (
                    f"fixture-batch-{platform}" if multi_platform else "fixture-batch-1"
                )
                application.sync.submit_batch(
                    started.job_id,
                    platform,
                    idempotency_key,
                    scan_items,
                )
                platform_status = next(
                    entry
                    for entry in application.sync.get_status(started.job_id)["platforms"]
                    if entry["platform"] == platform
                )
                scanned = platform_status["counts"]["scanned"]
                cap_reached = bool(platform_status["max_scan_reached"])
                application.sync.finish_scan(
                    started.job_id,
                    platform,
                    observed_end=not cap_reached,
                    max_scan_reached=cap_reached,
                    visible_total=len(platform_items),
                    frontier_ids=tuple(item.source_id for item in scan_items[:scanned]),
                )
            return application.sync.get_status(started.job_id)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            try:
                _fail_unfinished_platforms(
                    application,
                    started.job_id,
                    "fixture_import_failed",
                    message,
                )
            except Exception as failure_exc:
                exc.add_note(f"also unable to mark sync failed: {failure_exc}")
            raise


def _items_through_frontier(
    items: list[CapturedItem], frontier: tuple[str, ...]
) -> list[CapturedItem]:
    if not frontier:
        return items
    known = set(frontier)
    for index, item in enumerate(items):
        if item.source_id in known:
            return items[: index + 1]
    return items


def _fail_unfinished_platforms(
    application: Application,
    job_id: str,
    code: str,
    message: str,
) -> None:
    status = application.sync.get_status(job_id)
    for platform in status["platforms"]:
        if platform["status"] not in {"completed", "partial", "failed"}:
            application.sync.fail_sync(job_id, platform["platform"], code, message)


def _retrieval(application: Application) -> RetrievalService:
    if application.retrieval is None:
        raise RuntimeError("retrieval service is unavailable")
    return application.retrieval


def _status(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        if arguments.job_id is None:
            result: dict[str, Any] = _retrieval(application).status().as_dict()
            service = getattr(application, "embedding_service", None)
            if service is not None:
                result["embedding_summary"] = asdict(service.status())
            return result
        return application.sync.get_status(arguments.job_id)


def _search_request(arguments: argparse.Namespace, query: str) -> SearchRequest:
    published_since = (
        parse_datetime(arguments.published_since, field="published_since")
        if arguments.published_since is not None
        else None
    )
    published_until = (
        parse_datetime(arguments.published_until, field="published_until")
        if arguments.published_until is not None
        else None
    )
    if (
        published_since is not None
        and published_until is not None
        and published_since > published_until
    ):
        raise ValueError("published_since must not be later than published_until")
    favorited_since = (
        parse_datetime(arguments.favorited_since, field="favorited_since")
        if arguments.favorited_since is not None
        else None
    )
    favorited_until = (
        parse_datetime(arguments.favorited_until, field="favorited_until")
        if arguments.favorited_until is not None
        else None
    )
    if (
        favorited_since is not None
        and favorited_until is not None
        and favorited_since > favorited_until
    ):
        raise ValueError("favorited_since must not be later than favorited_until")
    return SearchRequest(
        query=query,
        platforms=tuple(arguments.platforms) if arguments.platforms else None,
        content_types=tuple(arguments.content_types) if arguments.content_types else None,
        collections=tuple(arguments.collections) if arguments.collections else None,
        published_since=published_since,
        published_until=published_until,
        favorited_since=favorited_since,
        favorited_until=favorited_until,
        limit=arguments.limit,
    )


def _search(arguments: argparse.Namespace) -> Any:
    request = _search_request(arguments, arguments.query)
    with Application.open(arguments.root) as application:
        return _retrieval(application).search(
            request,
            mode=RetrievalMode(arguments.retrieval_mode),
        )


def _search_batch(arguments: argparse.Namespace) -> dict[str, Any]:
    requests = [_search_request(arguments, query) for query in arguments.queries]
    with Application.open(arguments.root) as application:
        retrieval = _retrieval(application)
        results = []
        for query, request in zip(arguments.queries, requests, strict=True):
            response = retrieval.search(
                request,
                mode=RetrievalMode(arguments.retrieval_mode),
            )
            results.append({"query": query, "response": asdict(response)})
        return {"results": results}


def _get_item(arguments: argparse.Namespace) -> Any:
    request = GetItemRequest(
        platform=arguments.platform,
        source_id=arguments.source_id,
        include_content=arguments.include_content,
    )
    with Application.open(arguments.root) as application:
        return _retrieval(application).get_item(request)


def _collections(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        return _retrieval(application).collections().as_dict()


def _reindex(arguments: argparse.Namespace) -> Any:
    with Application.open(arguments.root) as application:
        return _retrieval(application).reindex(ReindexRequest(force=arguments.force))


def _enrich_backfill(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        return application.library.backfill_summarize()


def _enrich_redo(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        if arguments.declined:
            return application.library.redo_declined_enrichment()
        return application.library.redo_enrichment(arguments.model)


def _favtime_backfill(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        return application.library.backfill_favorited_at()


def _github_sync(arguments: argparse.Namespace) -> dict[str, Any]:
    """Collect public GitHub stars without an Agent and without a browser.

    The token, when present, is read here and handed straight to the collector.
    It is never returned in this function's result, which is printed to stdout.
    """
    token = os.environ.get(TOKEN_ENV) or None
    with Application.open(arguments.root) as application:
        return run_sync(
            application.sync,
            login=arguments.user,
            mode=arguments.mode,
            max_scan_items=arguments.max_scan_items,
            token=token,
        )


def _access_backfill(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        return application.library.backfill_access_status()


def _collections_backfill(arguments: argparse.Namespace) -> dict[str, Any]:
    with Application.open(arguments.root) as application:
        return application.library.backfill_collections()


def _embeddings(application: Application) -> EmbeddingService:
    service = application.embedding_service
    if service is None:
        raise RuntimeError("embedding service is unavailable")
    return service


def _format_duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _print_build_progress(progress: EmbeddingBuildProgress) -> None:
    """Write one heartbeat to stderr, leaving stdout a single JSON report.

    A build over a real library runs for hours, and until this existed the only
    output was that report, at the end. There is no way to tell a long job from
    a stuck one by watching nothing happen.
    """
    eta = progress.eta_seconds
    line = (
        f"[{progress.phase}] {progress.done} done, {progress.remaining} left, "
        f"{progress.vectors} vectors, {_format_duration(progress.elapsed_seconds)} elapsed"
    )
    if eta is not None and progress.remaining:
        line += f", ~{_format_duration(eta)} left"
    print(line, file=sys.stderr, flush=True)


def _embedding_command(arguments: argparse.Namespace) -> Any:
    with Application.open(arguments.root) as application:
        service = _embeddings(application)
        if arguments.embedding_command == "init":
            profile = service.initialize()
            return {
                "model": profile.model,
                "license": DEFAULT_MODEL_LICENSE,
                "cache_path": str(application.paths.models),
                "profile_id": profile.id,
                "artifact_digest": profile.artifact_digest,
            }
        if arguments.embedding_command == "build":
            return service.build(
                max_items=arguments.max_items,
                force=arguments.force,
                progress=None if arguments.quiet else _print_build_progress,
            )
        raise RuntimeError(f"unsupported embeddings command: {arguments.embedding_command}")


_BROWSER_COMMANDS = frozenset({"setup", "doctor", "uninstall-browser"})


def _install_paths() -> InstallPaths:
    return InstallPaths.from_local_app_data()


def _registry() -> Registry:
    # Only the real CLI ever reaches the real hive; tests inject a fake.
    return WindowsRegistry() if os.name == "nt" else FakeRegistry()


def _detect_hosts(selected: Sequence[str] | None = None) -> list[AgentHost]:
    """Only touch Agent hosts that already exist on this machine."""
    home = Path.home()
    candidates = [
        AgentHost(name="codex", skills_dir=home / ".codex" / "skills", cli="codex"),
        AgentHost(name="claude", skills_dir=home / ".claude" / "skills", cli="claude"),
    ]
    wanted = set(selected) if selected else None
    return [
        host
        for host in candidates
        if (wanted is None or host.name in wanted)
        and ((home / f".{host.name}").is_dir() or shutil.which(host.cli) is not None)
    ]


def _native_host_executable() -> Path:
    """Where Chrome should launch the relay from.

    Preferring the installed console script keeps the manifest valid after this
    process exits; falling back to the interpreter path keeps a development
    checkout usable.
    """
    found = shutil.which("favhub-native-host")
    if found:
        return Path(found)
    return Path(sys.executable).with_name("favhub-native-host.exe")


def _setup(arguments: argparse.Namespace) -> dict[str, Any]:
    paths = _install_paths()
    data_root = arguments.root or persisted_data_root(paths) or paths.default_data_root
    return run_setup(
        paths,
        data_root=data_root,
        hosts=_detect_hosts(arguments.agent),
        registry=_registry(),
        runner=run_command,
        native_host_executable=_native_host_executable(),
        replace=arguments.replace,
    )


def _doctor(_arguments: argparse.Namespace) -> dict[str, Any]:
    return run_doctor(
        _install_paths(),
        registry=_registry(),
        runner=run_command,
        hosts=_detect_hosts(),
    )


def _uninstall_browser(_arguments: argparse.Namespace) -> dict[str, Any]:
    paths = _install_paths()
    return uninstall(
        paths,
        hosts=_detect_hosts(),
        registry=_registry(),
        runner=run_command,
    )


def _resolve_root(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    """Data commands need a root; take the persisted one when none was given."""
    if arguments.command in _BROWSER_COMMANDS:
        return
    if arguments.root is not None:
        return
    persisted = persisted_data_root(_install_paths())
    if persisted is None:
        parser.error("--root is required (or run 'favhub setup' to select one)")
    arguments.root = persisted


def _who_holds_the_root(arguments: Any, exc: DataRootBusyError) -> str:
    """Turn "the root is busy" into the thing the reader has to do about it.

    Being turned away here is not a fault — one FavHub per data root is the
    design — but the bare message names a lock file and no way forward, and the
    way forward is not guessable: the holder is the Agent window that is open
    right now, and indexing, embedding and every backfill run through this same
    door. Naming the process is what makes that obvious instead of mysterious.
    """
    root = getattr(arguments, "root", None)
    pid = running_favhub_pid(Path(root)) if root is not None else None
    if pid is None:
        return f"{exc}. Another FavHub holds it; close that window and try again."
    return (
        f"{exc}. FavHub is running as pid {pid} — most likely the Agent window you "
        "have open. Close it and run this again."
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    _resolve_root(parser, arguments)
    try:
        if arguments.command == "import-fixture":
            result = _import_fixture(arguments)
        elif arguments.command == "status":
            result = _status(arguments)
        elif arguments.command == "search":
            result = _search(arguments)
        elif arguments.command == "search-batch":
            result = _search_batch(arguments)
        elif arguments.command == "get-item":
            result = _get_item(arguments)
        elif arguments.command == "collections":
            result = _collections(arguments)
        elif arguments.command == "reindex":
            result = _reindex(arguments)
        elif arguments.command == "enrich-backfill":
            result = _enrich_backfill(arguments)
        elif arguments.command == "enrich-redo":
            result = _enrich_redo(arguments)
        elif arguments.command == "favtime-backfill":
            result = _favtime_backfill(arguments)
        elif arguments.command == "github-sync":
            result = _github_sync(arguments)
        elif arguments.command == "access-backfill":
            result = _access_backfill(arguments)
        elif arguments.command == "collections-backfill":
            result = _collections_backfill(arguments)
        elif arguments.command == "embeddings":
            result = _embedding_command(arguments)
        elif arguments.command == "setup":
            result = _setup(arguments)
        elif arguments.command == "doctor":
            result = _doctor(arguments)
        elif arguments.command == "uninstall-browser":
            result = _uninstall_browser(arguments)
        else:
            raise RuntimeError(f"unsupported command: {arguments.command}")
    except DataRootBusyError as exc:
        parser.error(_who_holds_the_root(arguments, exc))
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    payload = asdict(result) if is_dataclass(result) else result
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = ["captured_item", "load_fixture", "main", "parse_datetime"]
