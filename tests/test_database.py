import sqlite3
from pathlib import Path

import pytest

import favhub.database as database_module
from favhub.database import (
    SCHEMA_V1,
    SCHEMA_V2_STATEMENTS,
    SCHEMA_V3_STATEMENTS,
    SCHEMA_V4_STATEMENTS,
    SCHEMA_V5_STATEMENTS,
    Database,
)


def _seed_platform_run(database: Database, job_id: str, platform: str) -> None:
    timestamp = "2026-07-18T00:00:00Z"
    database.connection.execute(
        """INSERT INTO sync_jobs(id, mode, status, options_json, created_at, updated_at)
           VALUES (?, 'incremental', 'running', '{}', ?, ?)""",
        (job_id, timestamp, timestamp),
    )
    database.connection.execute(
        """INSERT INTO sync_platform_runs(job_id, platform, status, counts_json, error_json)
           VALUES (?, ?, 'running', '{}', NULL)""",
        (job_id, platform),
    )


def test_open_creates_versioned_schema(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "state" / "favhub.sqlite3")
    try:
        names = {
            row[0]
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "schema_migrations",
            "sync_jobs",
            "sync_platform_runs",
            "sync_batches",
            "sync_frontiers",
            "items",
            "enrichment_tasks",
            "content_chunks",
            "content_chunks_fts",
            "sync_frontier_scopes",
            "sync_scope_runs",
        } <= names
        versions = [
            row[0]
            for row in database.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    finally:
        database.close()


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        with pytest.raises(RuntimeError, match="stop"), database.transaction():
            database.connection.execute("INSERT INTO schema_migrations(version) VALUES (99)")
            raise RuntimeError("stop")
        row = database.connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 99"
        ).fetchone()
        assert row is None
    finally:
        database.close()


def test_transaction_rejects_nesting_and_outer_can_commit(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:

        def open_nested_transaction() -> None:
            with database.transaction():
                pass

        with database.transaction():
            with pytest.raises(RuntimeError, match="nested transactions are not supported"):
                open_nested_transaction()
            database.connection.execute("INSERT INTO schema_migrations(version) VALUES (88)")
        row = database.connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 88"
        ).fetchone()
        assert row is not None
    finally:
        database.close()


def test_successful_transaction_commits(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        with database.transaction():
            database.connection.execute("INSERT INTO schema_migrations(version) VALUES (87)")
        row = database.connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 87"
        ).fetchone()
        assert row is not None
    finally:
        database.close()


def test_opening_same_database_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "favhub.sqlite3"
    first = Database.open(path)
    first.close()
    second = Database.open(path)
    try:
        count = second.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()[0]
        assert count == 1
        assert (
            second.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
            ).fetchone()[0]
            == 1
        )
        assert (
            second.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 3"
            ).fetchone()[0]
            == 1
        )
        assert (
            second.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
            ).fetchone()[0]
            == 1
        )
        assert (
            second.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
            ).fetchone()[0]
            == 1
        )
        assert (
            second.connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
            ).fetchone()[0]
            == 1
        )
    finally:
        second.close()


def test_v5_creates_embedding_schema_and_records_migration(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "state" / "favhub.sqlite3")
    try:
        names = {
            str(row["name"])
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('embedding_profiles','chunk_embeddings','embedding_build_runs',"
                "'one_active_embedding_profile')"
            )
        }
        assert names == {
            "embedding_profiles",
            "chunk_embeddings",
            "embedding_build_runs",
            "one_active_embedding_profile",
        }
        assert (
            database.connection.execute(
                "SELECT version FROM schema_migrations WHERE version = 5"
            ).fetchone()
            is not None
        )
    finally:
        database.close()


def test_open_migrates_v4_database_to_v5(tmp_path: Path) -> None:
    path = tmp_path / "v4.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        for statement in (*SCHEMA_V2_STATEMENTS, *SCHEMA_V3_STATEMENTS, *SCHEMA_V4_STATEMENTS):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            ((1,), (2,), (3,), (4,)),
        )
        connection.commit()
    finally:
        connection.close()

    database = Database.open(path)
    try:
        assert [
            row[0]
            for row in database.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='embedding_profiles'"
            ).fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_failed_v5_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken-v4.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        for statement in (*SCHEMA_V2_STATEMENTS, *SCHEMA_V3_STATEMENTS, *SCHEMA_V4_STATEMENTS):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            ((1,), (2,), (3,), (4,)),
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            (
                5,
                (
                    "CREATE TABLE embedding_profiles (id TEXT PRIMARY KEY)",
                    "CREATE TABLE broken syntax",
                ),
            ),
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        Database.open(path)

    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='embedding_profiles'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def _insert_embedding_profile(database: Database, profile_id: str, is_active: int) -> None:
    database.connection.execute(
        """INSERT INTO embedding_profiles(
            id, provider, provider_version, model, dimensions, normalization,
            max_input_tokens, segment_tokens, overlap_tokens, artifact_digest,
            config_json, is_active, initialized_at
        ) VALUES (
            ?, 'provider', '1', 'model', 2, 'l2', 128, 64, 8,
            'digest', '{}', ?, '2026-01-01'
        )""",
        (profile_id, is_active),
    )


def test_embedding_profiles_allow_only_one_active_profile(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _insert_embedding_profile(database, "active-1", 1)
        _insert_embedding_profile(database, "inactive", 0)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_embedding_profile(database, "active-2", 1)
    finally:
        database.close()


def test_chunk_embeddings_cascade_when_chunk_or_profile_deleted(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute(
            """INSERT INTO items(
                platform, source_id, content_hash, item_dir, published_at, first_seen_at
            ) VALUES ('demo', 'item-1', 'hash', '/tmp/item-1', '2026-01-01', '2026-01-01')"""
        )
        chunk_id = database.connection.execute(
            """INSERT INTO content_chunks(
                platform, source_id, ordinal, relative_path, line_start, line_end,
                heading, text, input_hash, created_at
            ) VALUES ('demo', 'item-1', 0, 'content.md', 1, 1, NULL, 'text', 'hash', '2026-01-01')
            RETURNING id"""
        ).fetchone()[0]
        _insert_embedding_profile(database, "profile-1", 1)
        database.connection.execute(
            """INSERT INTO chunk_embeddings(
                chunk_id, profile_id, segment_ordinal, token_start, token_end, vector, created_at
            ) VALUES (?, 'profile-1', 0, 0, 2, ?, '2026-01-01')""",
            (chunk_id, b"\x00\x00\x80?" * 2),
        )
        database.connection.execute("DELETE FROM embedding_profiles WHERE id='profile-1'")
        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
        )

        _insert_embedding_profile(database, "profile-1", 1)
        database.connection.execute(
            """INSERT INTO chunk_embeddings(
                chunk_id, profile_id, segment_ordinal, token_start, token_end, vector, created_at
            ) VALUES (?, 'profile-1', 0, 0, 2, ?, '2026-01-01')""",
            (chunk_id, b"\x00\x00\x80?" * 2),
        )
        database.connection.execute("DELETE FROM content_chunks WHERE id=?", (chunk_id,))
        assert (
            database.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
        )
    finally:
        database.close()


def test_open_configures_sqlite_pragmas(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        foreign_keys = database.connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = database.connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert foreign_keys == 1
        assert journal_mode == "wal"
    finally:
        database.close()


def test_transaction_rolls_back_on_base_exception(tmp_path: Path) -> None:
    class StopSignal(BaseException):
        pass

    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        with pytest.raises(StopSignal), database.transaction():
            database.connection.execute("INSERT INTO schema_migrations(version) VALUES (86)")
            raise StopSignal
        row = database.connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 86"
        ).fetchone()
        assert row is None
    finally:
        database.close()


def test_open_migrates_v1_database_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.commit()
    finally:
        connection.close()

    first = Database.open(path)
    first.close()
    second = Database.open(path)
    try:
        versions = [
            row[0]
            for row in second.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert (
            second.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'content_chunks'"
            ).fetchone()[0]
            == 1
        )
    finally:
        second.close()


def test_open_migrates_existing_v2_database_with_content_type(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        for statement in SCHEMA_V2_STATEMENTS:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        connection.execute(
            """INSERT INTO items(
                   platform, source_id, content_hash, item_dir, published_at, first_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "x",
                "legacy",
                "hash",
                "items/x/legacy",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    database = Database.open(path)
    try:
        versions = [
            row[0]
            for row in database.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(items)")}
        assert "content_type" in columns
        assert (
            database.connection.execute(
                "SELECT content_type FROM items WHERE platform='x' AND source_id='legacy'"
            ).fetchone()[0]
            == "text"
        )
        assert (
            database.connection.execute(
                "SELECT index_input_hash FROM items WHERE platform='x' AND source_id='legacy'"
            ).fetchone()[0]
            is None
        )
    finally:
        database.close()


def test_open_migrates_existing_v3_database_with_nullable_index_hash(tmp_path: Path) -> None:
    path = tmp_path / "v3.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        for statement in (*SCHEMA_V2_STATEMENTS, *SCHEMA_V3_STATEMENTS):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            ((1,), (2,), (3,)),
        )
        connection.execute(
            """INSERT INTO items(
                   platform, source_id, content_hash, item_dir, published_at, first_seen_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "x",
                "legacy-v3",
                "hash",
                "items/x/legacy-v3",
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    database = Database.open(path)
    try:
        versions = [
            row[0]
            for row in database.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert (
            database.connection.execute(
                "SELECT index_input_hash FROM items WHERE source_id='legacy-v3'"
            ).fetchone()[0]
            is None
        )
    finally:
        database.close()


def test_failed_v2_migration_does_not_record_version_or_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken-v1.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            (
                2,
                (*database_module.SCHEMA_V2_STATEMENTS, "CREATE TABLE invalid syntax"),
            ),
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        Database.open(path)

    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name = 'content_chunks'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'content_chunks_fts%'
            """
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'trigger' AND name LIKE 'content_chunks_after_%'
            """
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_content_chunk_triggers_make_fts_searchable(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute(
            """
            INSERT INTO items(
                platform, source_id, content_hash, item_dir, published_at, first_seen_at
            ) VALUES ('demo', 'item-1', 'hash', '/tmp/item-1', '2026-01-01', '2026-01-01')
            """
        )
        database.connection.execute(
            """
            INSERT INTO content_chunks(
                platform, source_id, ordinal, relative_path, line_start, line_end,
                heading, text, input_hash, created_at
            ) VALUES ('demo', 'item-1', 0, 'content.md', 1, 1,
                      'Heading', 'SQLite external content search', 'hash', '2026-01-01')
            """
        )
        rows = database.connection.execute(
            """
            SELECT content_chunks_fts.rowid
            FROM content_chunks_fts
            WHERE content_chunks_fts MATCH 'external'
            """
        ).fetchall()
        assert [row[0] for row in rows] == [1]
    finally:
        database.close()


def test_content_chunk_update_and_delete_keep_fts_in_sync(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute(
            """
            INSERT INTO items(
                platform, source_id, content_hash, item_dir, published_at, first_seen_at
            ) VALUES ('demo', 'item-1', 'hash', '/tmp/item-1', '2026-01-01', '2026-01-01')
            """
        )
        database.connection.execute(
            """
            INSERT INTO content_chunks(
                platform, source_id, ordinal, relative_path, line_start, line_end,
                heading, text, input_hash, created_at
            ) VALUES ('demo', 'item-1', 0, 'content.md', 1, 1,
                      'Heading', 'original phrase', 'hash', '2026-01-01')
            """
        )

        database.connection.execute(
            "UPDATE content_chunks SET text = 'replacement phrase' WHERE id = 1"
        )
        assert (
            database.connection.execute(
                "SELECT rowid FROM content_chunks_fts WHERE content_chunks_fts MATCH 'original'"
            ).fetchone()
            is None
        )
        assert (
            database.connection.execute(
                "SELECT rowid FROM content_chunks_fts WHERE content_chunks_fts MATCH 'replacement'"
            ).fetchone()[0]
            == 1
        )

        database.connection.execute("DELETE FROM content_chunks WHERE id = 1")
        assert (
            database.connection.execute(
                "SELECT rowid FROM content_chunks_fts WHERE content_chunks_fts MATCH 'replacement'"
            ).fetchone()
            is None
        )
    finally:
        database.close()


def test_rebuild_fts_restores_external_content_index(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute(
            """
            INSERT INTO items(
                platform, source_id, content_hash, item_dir, published_at, first_seen_at
            ) VALUES ('demo', 'item-1', 'hash', '/tmp/item-1', '2026-01-01', '2026-01-01')
            """
        )
        database.connection.execute(
            """
            INSERT INTO content_chunks(
                platform, source_id, ordinal, relative_path, line_start, line_end,
                heading, text, input_hash, created_at
            ) VALUES ('demo', 'item-1', 0, 'content.md', 1, 1,
                      'Heading', 'rebuildable index', 'hash', '2026-01-01')
            """
        )
        database.connection.execute(
            """
            INSERT INTO content_chunks_fts(content_chunks_fts, rowid, text, heading)
            VALUES ('delete', 1, 'rebuildable index', 'Heading')
            """
        )
        assert (
            database.connection.execute(
                "SELECT rowid FROM content_chunks_fts WHERE content_chunks_fts MATCH 'rebuildable'"
            ).fetchone()
            is None
        )

        database.rebuild_fts()

        assert (
            database.connection.execute(
                "SELECT rowid FROM content_chunks_fts WHERE content_chunks_fts MATCH 'rebuildable'"
            ).fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_fts_availability_checks_virtual_table_and_all_maintenance_triggers(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        assert database.is_fts_available()

        database.connection.execute("DROP TRIGGER content_chunks_after_update")
        assert not database.is_fts_available()

        database.connection.execute(SCHEMA_V2_STATEMENTS[4])
        assert database.is_fts_available()

        database.connection.execute("DROP TABLE content_chunks_fts")
        assert not database.is_fts_available()
    finally:
        database.close()


def test_repair_fts_recreates_missing_schema_and_rebuilds_existing_chunks(
    tmp_path: Path,
) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute(
            """
            INSERT INTO items(
                platform, source_id, content_hash, item_dir, published_at, first_seen_at
            ) VALUES ('demo', 'item-1', 'hash', '/tmp/item-1', '2026-01-01', '2026-01-01')
            """
        )
        database.connection.execute(
            """
            INSERT INTO content_chunks(
                platform, source_id, ordinal, relative_path, line_start, line_end,
                heading, text, input_hash, created_at
            ) VALUES ('demo', 'item-1', 0, 'content.md', 1, 1,
                      'Heading', 'recoverable existing chunk', 'hash', '2026-01-01')
            """
        )
        database.connection.execute("DROP TABLE content_chunks_fts")
        database.connection.execute("DROP TRIGGER content_chunks_after_update")

        database.repair_fts()

        assert database.is_fts_available()
        assert (
            database.connection.execute(
                "SELECT rowid FROM content_chunks_fts WHERE content_chunks_fts MATCH 'recoverable'"
            ).fetchone()[0]
            == 1
        )
        assert database.connection.execute("SELECT COUNT(*) FROM content_chunks").fetchone()[0] == 1

        database.repair_fts()

        assert database.is_fts_available()
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM content_chunks_fts "
                "WHERE content_chunks_fts MATCH 'recoverable'"
            ).fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_repair_fts_rolls_back_partial_schema_changes_on_failure(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute("DROP TABLE content_chunks_fts")
        database.connection.execute("DROP TRIGGER content_chunks_after_insert")

        def deny_trigger_creation(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _database_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_CREATE_TRIGGER:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        database.connection.set_authorizer(deny_trigger_creation)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            database.repair_fts()
        database.connection.set_authorizer(None)

        assert not database.is_fts_available()
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='content_chunks_fts'"
            ).fetchone()[0]
            == 0
        )
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='content_chunks'"
            ).fetchone()[0]
            == 1
        )
    finally:
        database.connection.set_authorizer(None)
        database.close()


def test_v6_creates_scope_tables_and_records_migration(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "state" / "favhub.sqlite3")
    try:
        names = {
            str(row["name"])
            for row in database.connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('sync_frontier_scopes','sync_scope_runs')"
            )
        }
        assert names == {"sync_frontier_scopes", "sync_scope_runs"}
        assert (
            database.connection.execute(
                "SELECT version FROM schema_migrations WHERE version = 6"
            ).fetchone()
            is not None
        )
    finally:
        database.close()


def test_open_migrates_v5_database_to_v6(tmp_path: Path) -> None:
    path = tmp_path / "v5.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        for statement in (
            *SCHEMA_V2_STATEMENTS,
            *SCHEMA_V3_STATEMENTS,
            *SCHEMA_V4_STATEMENTS,
            *SCHEMA_V5_STATEMENTS,
        ):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            ((1,), (2,), (3,), (4,), (5,)),
        )
        connection.commit()
    finally:
        connection.close()

    database = Database.open(path)
    try:
        assert [
            row[0]
            for row in database.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert (
            database.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='sync_scope_runs'"
            ).fetchone()[0]
            == 1
        )
    finally:
        database.close()


def test_failed_v6_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken-v5.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_V1)
        for statement in (
            *SCHEMA_V2_STATEMENTS,
            *SCHEMA_V3_STATEMENTS,
            *SCHEMA_V4_STATEMENTS,
            *SCHEMA_V5_STATEMENTS,
        ):
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO schema_migrations(version) VALUES (?)",
            ((1,), (2,), (3,), (4,), (5,)),
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(
        database_module,
        "MIGRATIONS",
        (
            (
                6,
                (
                    "CREATE TABLE sync_frontier_scopes (platform TEXT PRIMARY KEY)",
                    "CREATE TABLE broken syntax",
                ),
            ),
        ),
    )
    with pytest.raises(sqlite3.OperationalError):
        Database.open(path)

    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='sync_frontier_scopes'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_scope_runs_cascade_when_platform_run_deleted(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _seed_platform_run(database, "job-1", "bilibili")
        database.connection.execute(
            """INSERT INTO sync_scope_runs(
                job_id, platform, scope_id, scope_name, status, counts_json
            ) VALUES ('job-1', 'bilibili', '100001', '默认收藏夹', 'running', '{}')"""
        )
        database.connection.execute(
            "DELETE FROM sync_platform_runs WHERE job_id='job-1' AND platform='bilibili'"
        )
        assert (
            database.connection.execute("SELECT COUNT(*) FROM sync_scope_runs").fetchone()[0] == 0
        )
    finally:
        database.close()


def test_scope_frontier_is_keyed_by_platform_and_scope(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        database.connection.execute(
            """INSERT INTO sync_frontier_scopes(platform, scope_id, source_ids_json, updated_at)
               VALUES ('bilibili', '100001', '["BV1"]', '2026-07-18T00:00:00Z')"""
        )
        database.connection.execute(
            """INSERT INTO sync_frontier_scopes(platform, scope_id, source_ids_json, updated_at)
               VALUES ('bilibili', '100002', '["BV2"]', '2026-07-18T00:00:00Z')"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            database.connection.execute(
                """INSERT INTO sync_frontier_scopes(
                    platform, scope_id, source_ids_json, updated_at
                ) VALUES ('bilibili', '100001', '["BV3"]', '2026-07-18T00:00:00Z')"""
            )
        rows = {
            (str(row["scope_id"]), str(row["source_ids_json"]))
            for row in database.connection.execute(
                "SELECT scope_id, source_ids_json FROM sync_frontier_scopes ORDER BY scope_id"
            )
        }
        assert rows == {("100001", '["BV1"]'), ("100002", '["BV2"]')}
    finally:
        database.close()


def test_v7_adds_nullable_favorited_at_column(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        columns = {row[1] for row in database.connection.execute("PRAGMA table_info(items)")}
        assert "favorited_at" in columns
        database.connection.execute(
            """INSERT INTO items(
                platform, source_id, content_hash, item_dir, published_at, first_seen_at
            ) VALUES ('x', 'v7', 'hash', 'items/x/v7', '2026-01-01', '2026-01-01')"""
        )
        assert (
            database.connection.execute(
                "SELECT favorited_at FROM items WHERE source_id='v7'"
            ).fetchone()[0]
            is None
        )
    finally:
        database.close()


def _seed_browser_session(
    database: Database,
    session_id: str,
    job_id: str,
    platform: str,
    status: str,
) -> None:
    timestamp = "2026-08-02T00:00:00Z"
    database.connection.execute(
        """INSERT INTO browser_capture_sessions(
            id, job_id, platform, status, protocol_version,
            extension_version, lease_expires_at, error_json,
            created_at, updated_at, finished_at
        ) VALUES (?, ?, ?, ?, 1, NULL, NULL, NULL, ?, ?, NULL)""",
        (session_id, job_id, platform, status, timestamp, timestamp),
    )


def test_v9_creates_browser_capture_sessions(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        version = database.connection.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0]
        columns = {
            row["name"]
            for row in database.connection.execute("PRAGMA table_info(browser_capture_sessions)")
        }
        assert version == 10
        assert columns == {
            "id",
            "job_id",
            "platform",
            "status",
            "protocol_version",
            "extension_version",
            "lease_expires_at",
            "error_json",
            "created_at",
            "updated_at",
            "finished_at",
        }
    finally:
        database.close()


def test_v9_rejects_a_second_open_session_for_one_platform(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _seed_platform_run(database, "job-a", "x")
        _seed_platform_run(database, "job-b", "x")
        _seed_browser_session(database, "s1", "job-a", "x", "capturing")
        with pytest.raises(sqlite3.IntegrityError):
            _seed_browser_session(database, "s2", "job-b", "x", "awaiting_browser")
    finally:
        database.close()


def test_v9_allows_a_new_session_once_the_previous_one_is_terminal(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _seed_platform_run(database, "job-a", "x")
        _seed_platform_run(database, "job-b", "x")
        _seed_browser_session(database, "s1", "job-a", "x", "completed")
        _seed_browser_session(database, "s2", "job-b", "x", "awaiting_browser")
        statuses = {
            (str(row["id"]), str(row["status"]))
            for row in database.connection.execute(
                "SELECT id, status FROM browser_capture_sessions"
            )
        }
        assert statuses == {("s1", "completed"), ("s2", "awaiting_browser")}
    finally:
        database.close()


def test_v9_keeps_paused_sessions_exclusive_too(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _seed_platform_run(database, "job-a", "zhihu")
        _seed_platform_run(database, "job-b", "zhihu")
        _seed_browser_session(database, "s1", "job-a", "zhihu", "paused")
        with pytest.raises(sqlite3.IntegrityError):
            _seed_browser_session(database, "s2", "job-b", "zhihu", "awaiting_browser")
    finally:
        database.close()


def test_v9_rejects_unknown_session_status(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _seed_platform_run(database, "job-a", "x")
        with pytest.raises(sqlite3.IntegrityError):
            _seed_browser_session(database, "s1", "job-a", "x", "running")
    finally:
        database.close()


def test_v9_cascades_sessions_when_the_platform_run_is_removed(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "favhub.sqlite3")
    try:
        _seed_platform_run(database, "job-a", "x")
        _seed_browser_session(database, "s1", "job-a", "x", "capturing")
        database.connection.execute("DELETE FROM sync_jobs WHERE id = 'job-a'")
        assert (
            database.connection.execute("SELECT COUNT(*) FROM browser_capture_sessions").fetchone()[
                0
            ]
            == 0
        )
    finally:
        database.close()
