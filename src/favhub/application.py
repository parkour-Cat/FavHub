from pathlib import Path
from typing import cast

from favhub.browser_capture import BrowserCaptureStore
from favhub.browser_gateway import BrowserGateway
from favhub.browser_ingest import BrowserIngestor
from favhub.browser_launcher import open_collection_page
from favhub.config import FavHubPaths, packaged_extension_version
from favhub.database import Database
from favhub.embedding_indexing import EmbeddingIndexer, ProviderLoader
from favhub.embedding_profiles import EmbeddingProfileStore
from favhub.embedding_runtime import EmbeddingRuntime
from favhub.embedding_service import EmbeddingService
from favhub.enrichment_queue import EnrichmentQueue
from favhub.indexing import ContentIndexer
from favhub.item_store import ItemStore
from favhub.library import LibraryModule
from favhub.maintenance import StartupMaintenance
from favhub.retrieval import RetrievalService
from favhub.root_lock import DataRootLock
from favhub.sync_gateway import SyncGateway
from favhub.sync_module import SyncModule


class Application:
    """Own the lifetime of the local FavHub modules.

    The application is deliberately a small composition root.  Feature modules
    receive their dependencies explicitly, while callers only need to manage
    one object and its database connection.
    """

    def __init__(
        self,
        paths: FavHubPaths,
        database: Database,
        store: ItemStore,
        queue: EnrichmentQueue,
        library: LibraryModule,
        maintenance: StartupMaintenance,
        sync: SyncModule,
        root_lock: DataRootLock,
        indexer: ContentIndexer | None = None,
        retrieval: RetrievalService | None = None,
        embedding_profiles: EmbeddingProfileStore | None = None,
        embedding_runtime: EmbeddingRuntime | None = None,
        embedding_service: EmbeddingService | None = None,
        browser_sessions: BrowserCaptureStore | None = None,
        browser_gateway: BrowserGateway | None = None,
    ) -> None:
        self.paths = paths
        self.database = database
        self.store = store
        self.queue = queue
        self.library = library
        self.maintenance = maintenance
        self.sync = sync
        self.indexer = indexer
        self.retrieval = retrieval
        self.embedding_profiles = embedding_profiles
        self.embedding_runtime = embedding_runtime
        self.embedding_service = embedding_service
        self.browser_sessions = browser_sessions
        self.browser_gateway = browser_gateway
        self._root_lock = root_lock
        self._closed = False

    @classmethod
    def open(cls, root: Path) -> "Application":
        paths = FavHubPaths.from_root(root)
        paths.ensure()
        root_lock = DataRootLock.acquire(paths.state / "favhub.lock")
        database: Database | None = None
        try:
            database = Database.open(paths.database)
            embedding_profiles = EmbeddingProfileStore(database)
            # Construction is intentionally lazy: importing/initializing the
            # optional FastEmbed dependency happens only on explicit init/load.
            embedding_runtime = EmbeddingRuntime(paths, embedding_profiles)
            store = ItemStore(paths.items)
            queue = EnrichmentQueue(database)
            library = LibraryModule(database, store, queue)
            maintenance = StartupMaintenance(database, store, queue)
            sync = SyncModule(database, library)
            indexer = ContentIndexer(database, store, queue)
            retrieval = RetrievalService(
                database,
                store,
                indexer,
                embedding_profiles=embedding_profiles,
                embedding_runtime=embedding_runtime,
            )
            embedding_indexer = EmbeddingIndexer(
                database,
                queue,
                embedding_profiles,
                provider_loader=cast(
                    ProviderLoader,
                    lambda: embedding_runtime.load_active(local_only=True),
                ),
            )
            embedding_service = EmbeddingService(
                database,
                embedding_runtime,
                embedding_profiles,
                queue,
                indexer,
                embedding_indexer,
            )
            browser_sessions = BrowserCaptureStore(database)
            browser_ingestor = BrowserIngestor(sync, browser_sessions)
            browser_gateway = BrowserGateway(
                SyncGateway(sync),
                sync,
                browser_sessions,
                browser_ingestor,
                # The one place that may open a window on the user's desktop.
                open_page=open_collection_page,
                # Compared against what Chrome reports it is running, so an
                # upgrade that was never reloaded is refused rather than
                # collected with stale adapter code.
                installed_version=packaged_extension_version,
            )
            embedding_service.recover_interrupted_builds()
            maintenance.run()
        except BaseException:
            # Startup maintenance validates published snapshots and may fail
            # before the application is returned.  Never leak the connection
            # in this path (which would leave a SQLite lock behind).
            try:
                if database is not None:
                    database.close()
            finally:
                root_lock.release()
            raise
        return cls(
            paths,
            database,
            store,
            queue,
            library,
            maintenance,
            sync,
            root_lock,
            indexer,
            retrieval,
            embedding_profiles,
            embedding_runtime,
            embedding_service,
            browser_sessions,
            browser_gateway,
        )

    def close(self) -> None:
        if not self._closed:
            try:
                self.database.close()
            finally:
                self._root_lock.release()
                self._closed = True

    def __enter__(self) -> "Application":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


__all__ = ["Application"]
