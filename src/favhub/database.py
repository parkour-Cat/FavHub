import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from favhub.fts_text import fts_text

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sync_jobs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('full', 'incremental')),
    status TEXT NOT NULL,
    options_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    capture_finished_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_platform_runs (
    job_id TEXT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL,
    error_json TEXT,
    observed_end INTEGER NOT NULL DEFAULT 0,
    max_scan_reached INTEGER NOT NULL DEFAULT 0,
    visible_total INTEGER,
    PRIMARY KEY (job_id, platform)
);
CREATE TABLE IF NOT EXISTS sync_batches (
    receipt_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES sync_jobs(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, platform, idempotency_key)
);
CREATE TABLE IF NOT EXISTS sync_frontiers (
    platform TEXT PRIMARY KEY,
    source_ids_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    platform TEXT NOT NULL,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    item_dir TEXT NOT NULL,
    published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_full_synced_at TEXT,
    access_status TEXT NOT NULL DEFAULT 'available',
    PRIMARY KEY (platform, source_id)
);
CREATE TABLE IF NOT EXISTS enrichment_tasks (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (platform, source_id, kind, input_hash)
);
"""

SCHEMA_V2_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS content_chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        source_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        relative_path TEXT NOT NULL,
        line_start INTEGER NOT NULL,
        line_end INTEGER NOT NULL,
        heading TEXT,
        text TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (platform, source_id, ordinal, input_hash),
        FOREIGN KEY (platform, source_id)
            REFERENCES items(platform, source_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS content_chunks_fts USING fts5(
        text,
        heading,
        content='content_chunks',
        content_rowid='id',
        tokenize='unicode61'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS content_chunks_after_insert
    AFTER INSERT ON content_chunks BEGIN
        INSERT INTO content_chunks_fts(rowid, text, heading)
        VALUES (new.id, new.text, new.heading);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS content_chunks_after_delete
    AFTER DELETE ON content_chunks BEGIN
        INSERT INTO content_chunks_fts(content_chunks_fts, rowid, text, heading)
        VALUES ('delete', old.id, old.text, old.heading);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS content_chunks_after_update
    AFTER UPDATE ON content_chunks BEGIN
        INSERT INTO content_chunks_fts(content_chunks_fts, rowid, text, heading)
        VALUES ('delete', old.id, old.text, old.heading);
        INSERT INTO content_chunks_fts(rowid, text, heading)
        VALUES (new.id, new.text, new.heading);
    END
    """,
)

SCHEMA_V2 = "\n;\n".join(SCHEMA_V2_STATEMENTS)

SCHEMA_V3_STATEMENTS = ("ALTER TABLE items ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'",)

SCHEMA_V4_STATEMENTS = ("ALTER TABLE items ADD COLUMN index_input_hash TEXT",)

SCHEMA_V5_STATEMENTS = (
    """
    CREATE TABLE embedding_profiles (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        provider_version TEXT NOT NULL,
        model TEXT NOT NULL,
        dimensions INTEGER NOT NULL CHECK (dimensions > 0),
        normalization TEXT NOT NULL CHECK (normalization = 'l2'),
        max_input_tokens INTEGER NOT NULL CHECK (max_input_tokens > 0),
        segment_tokens INTEGER NOT NULL CHECK (segment_tokens > 0),
        overlap_tokens INTEGER NOT NULL CHECK (
            overlap_tokens >= 0 AND overlap_tokens < segment_tokens
        ),
        artifact_digest TEXT NOT NULL,
        config_json TEXT NOT NULL,
        is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
        initialized_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX one_active_embedding_profile
    ON embedding_profiles(is_active) WHERE is_active = 1
    """,
    """
    CREATE TABLE chunk_embeddings (
        chunk_id INTEGER NOT NULL
            REFERENCES content_chunks(id) ON DELETE CASCADE,
        profile_id TEXT NOT NULL
            REFERENCES embedding_profiles(id) ON DELETE CASCADE,
        segment_ordinal INTEGER NOT NULL CHECK (segment_ordinal >= 0),
        token_start INTEGER NOT NULL CHECK (token_start >= 0),
        token_end INTEGER NOT NULL CHECK (token_end > token_start),
        vector BLOB NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (chunk_id, profile_id, segment_ordinal)
    )
    """,
    """
    CREATE TABLE embedding_build_runs (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL REFERENCES embedding_profiles(id),
        status TEXT NOT NULL,
        max_items INTEGER,
        counts_json TEXT NOT NULL,
        error_json TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
)

SCHEMA_V6_STATEMENTS = (
    """
    CREATE TABLE sync_frontier_scopes (
        platform TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        source_ids_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (platform, scope_id)
    )
    """,
    """
    CREATE TABLE sync_scope_runs (
        job_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        scope_id TEXT NOT NULL,
        scope_name TEXT NOT NULL,
        status TEXT NOT NULL,
        counts_json TEXT NOT NULL,
        error_json TEXT,
        observed_end INTEGER NOT NULL DEFAULT 0,
        max_scan_reached INTEGER NOT NULL DEFAULT 0,
        visible_total INTEGER,
        PRIMARY KEY (job_id, platform, scope_id),
        FOREIGN KEY (job_id, platform) REFERENCES sync_platform_runs(job_id, platform)
            ON DELETE CASCADE
    )
    """,
)

SCHEMA_V7_STATEMENTS = ("ALTER TABLE items ADD COLUMN favorited_at TEXT",)

FTS_CREATE_STATEMENT = """
    CREATE VIRTUAL TABLE IF NOT EXISTS content_chunks_fts USING fts5(
        text,
        heading,
        fts_text,
        content='content_chunks',
        content_rowid='id',
        tokenize='unicode61'
    )
    """

FTS_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER IF NOT EXISTS content_chunks_after_insert
    AFTER INSERT ON content_chunks BEGIN
        INSERT INTO content_chunks_fts(rowid, text, heading, fts_text)
        VALUES (new.id, new.text, new.heading, new.fts_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS content_chunks_after_delete
    AFTER DELETE ON content_chunks BEGIN
        INSERT INTO content_chunks_fts(content_chunks_fts, rowid, text, heading, fts_text)
        VALUES ('delete', old.id, old.text, old.heading, old.fts_text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS content_chunks_after_update
    AFTER UPDATE ON content_chunks BEGIN
        INSERT INTO content_chunks_fts(content_chunks_fts, rowid, text, heading, fts_text)
        VALUES ('delete', old.id, old.text, old.heading, old.fts_text);
        INSERT INTO content_chunks_fts(rowid, text, heading, fts_text)
        VALUES (new.id, new.text, new.heading, new.fts_text);
    END
    """,
)

# v8: CJK bigram shadow column. The FTS table is rebuilt with a third indexed
# column; existing rows are backfilled in Python on the next open (see
# Database._backfill_fts_text), because the transform is not expressible in SQL.
SCHEMA_V8_STATEMENTS = (
    "ALTER TABLE content_chunks ADD COLUMN fts_text TEXT",
    "DROP TRIGGER IF EXISTS content_chunks_after_insert",
    "DROP TRIGGER IF EXISTS content_chunks_after_delete",
    "DROP TRIGGER IF EXISTS content_chunks_after_update",
    "DROP TABLE IF EXISTS content_chunks_fts",
    FTS_CREATE_STATEMENT,
    *FTS_TRIGGER_STATEMENTS,
)

# v9: durable browser capture sessions. One session tracks what the browser is
# doing for a platform run; the sync tables stay the record of what actually
# landed. The partial unique index enforces "one open session per platform"
# without blocking a new run once the previous one reached a terminal state.
SCHEMA_V9_STATEMENTS = (
    """
    CREATE TABLE browser_capture_sessions (
        id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN (
            'awaiting_browser', 'capturing', 'paused',
            'completed', 'failed', 'cancelled'
        )),
        protocol_version INTEGER NOT NULL,
        extension_version TEXT,
        lease_expires_at TEXT,
        error_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (job_id, platform),
        FOREIGN KEY (job_id, platform)
            REFERENCES sync_platform_runs(job_id, platform) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX one_open_browser_capture_per_platform
    ON browser_capture_sessions(platform)
    WHERE status IN ('awaiting_browser', 'capturing', 'paused')
    """,
)

SCHEMA_V10_STATEMENTS = (
    # Which of the user's own folders an item was found in. Already carried on
    # every CapturedItem and written into source.json; held in no table until
    # now, so nothing could filter or rank by it.
    """
    CREATE TABLE item_collections (
        platform TEXT NOT NULL,
        source_id TEXT NOT NULL,
        name TEXT NOT NULL,
        PRIMARY KEY (platform, source_id, name),
        FOREIGN KEY (platform, source_id)
            REFERENCES items(platform, source_id) ON DELETE CASCADE
    )
    """,
    # Answering "what is in this folder" is the whole point, so it gets the
    # index rather than relying on the primary key's leading columns.
    "CREATE INDEX item_collections_by_name ON item_collections(platform, name)",
)

FTS_TABLE = "content_chunks_fts"
FTS_TRIGGERS = frozenset(
    {
        "content_chunks_after_insert",
        "content_chunks_after_delete",
        "content_chunks_after_update",
    }
)


def _statements(script: str) -> tuple[str, ...]:
    return tuple(statement.strip() for statement in script.split(";") if statement.strip())


MIGRATIONS = (
    (1, _statements(SCHEMA_V1)),
    (2, SCHEMA_V2_STATEMENTS),
    (3, SCHEMA_V3_STATEMENTS),
    (4, SCHEMA_V4_STATEMENTS),
    (5, SCHEMA_V5_STATEMENTS),
    (6, SCHEMA_V6_STATEMENTS),
    (7, SCHEMA_V7_STATEMENTS),
    (8, SCHEMA_V8_STATEMENTS),
    (9, SCHEMA_V9_STATEMENTS),
    (10, SCHEMA_V10_STATEMENTS),
)


class Database:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, path: Path) -> "Database":
        path.parent.mkdir(parents=True, exist_ok=True)
        # The MCP process serves stdio JSON-RPC and the browser named pipe from
        # two threads. Disabling the same-thread check is what makes that
        # possible; it is NOT permission to call concurrently. Every caller must
        # hold ApplicationDispatcher.operation() — see application_dispatcher.py.
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied_versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, statements in sorted(MIGRATIONS, key=lambda migration: migration[0]):
                if version in applied_versions:
                    continue
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            cls._backfill_fts_text(connection)
        except BaseException:
            connection.close()
            raise
        return cls(connection)

    @staticmethod
    def _backfill_fts_text(connection: sqlite3.Connection) -> None:
        """Fill the CJK bigram column for chunks written before schema v8.

        Triggers are dropped for the duration so the backfill never issues
        FTS deletes for rows the freshly created index has never contained;
        a full rebuild afterwards makes the index authoritative again.
        """
        rows = connection.execute(
            "SELECT id, text FROM content_chunks WHERE fts_text IS NULL"
        ).fetchall()
        if not rows:
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for name in sorted(FTS_TRIGGERS):
                connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            connection.executemany(
                "UPDATE content_chunks SET fts_text = ? WHERE id = ?",
                [(fts_text(str(row["text"])), row["id"]) for row in rows],
            )
            for statement in FTS_TRIGGER_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO content_chunks_fts(content_chunks_fts) VALUES ('rebuild')"
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.connection.in_transaction:
            raise RuntimeError("nested transactions are not supported")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def rebuild_fts(self) -> None:
        with self.transaction():
            self.connection.execute(
                "INSERT INTO content_chunks_fts(content_chunks_fts) VALUES ('rebuild')"
            )

    def is_fts_available(self) -> bool:
        rows = self.connection.execute(
            """SELECT type, name, sql FROM sqlite_master
               WHERE name = ? OR name IN (?, ?, ?)""",
            (FTS_TABLE, *sorted(FTS_TRIGGERS)),
        ).fetchall()
        objects = {str(row["name"]): row for row in rows}
        table = objects.get(FTS_TABLE)
        if table is None or table["type"] != "table" or table["sql"] is None:
            return False
        table_sql = " ".join(str(table["sql"]).casefold().split())
        if not table_sql.startswith("create virtual table") or "using fts5" not in table_sql:
            return False
        return all(name in objects and objects[name]["type"] == "trigger" for name in FTS_TRIGGERS)

    def repair_fts(self) -> None:
        with self.transaction():
            self.connection.execute(FTS_CREATE_STATEMENT)
            for statement in FTS_TRIGGER_STATEMENTS:
                self.connection.execute(statement)
            self.connection.execute(
                "INSERT INTO content_chunks_fts(content_chunks_fts) VALUES ('rebuild')"
            )
