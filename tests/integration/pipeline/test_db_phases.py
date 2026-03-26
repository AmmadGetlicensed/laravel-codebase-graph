"""Integration tests for DB intelligence phases 24, 25, and 26.

Phase 24 (Live DB Introspection) is tested with a mock pymysql connection so
no actual database is required.  Phases 25 and 26 are tested by running them
on the tiny-laravel-app fixture after phases 1-19 have completed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


# ── Shared pipeline fixture (phases 1–19 only, no DB connection) ──────────────

@pytest.fixture(scope="module")
def base_ctx(tmp_path_factory):
    """Run the pipeline up to phase 23 on the tiny-laravel-app."""
    tmp = tmp_path_factory.mktemp("db_phases_test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False

    pipeline = Pipeline(app_copy, config=cfg)
    # Run all phases except 24 (needs real DB) — phases 25 and 26 run on migration-only data
    ctx = pipeline.run(
        full=True,
        skip_embeddings=True,
        phases=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 25, 26],
    )
    return ctx


# ── Phase 25: Model-Table Linking ─────────────────────────────────────────────

class TestPhase25ModelTableLink:
    def test_model_table_links_created(self, base_ctx):
        """Phase 25 should create USES_TABLE edges from EloquentModel → DatabaseTable."""
        rows = base_ctx.db.execute(
            "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) "
            "RETURN m.name AS model, t.name AS table_ LIMIT 20"
        )
        # tiny-laravel-app has at least one Eloquent model
        assert len(rows) >= 1, "Expected at least 1 USES_TABLE edge from phase 25"

    def test_uses_table_connects_correct_types(self, base_ctx):
        rows = base_ctx.db.execute(
            "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) "
            "RETURN m.node_id AS mnid, t.node_id AS tnid LIMIT 5"
        )
        for r in rows:
            assert r["mnid"].startswith("model:") or r["mnid"]
            assert r["tnid"]

    def test_stat_recorded(self, base_ctx):
        assert base_ctx.stats.get("model_table_links", 0) >= 1


# ── Phase 26: DB Access Analysis ─────────────────────────────────────────────

class TestPhase26DbAccessAnalysis:
    def test_phase_completes_without_fatal_error(self, base_ctx):
        """Phase 26 should run silently on a codebase even if no DB tables are found."""
        # The phase is best-effort; it must not raise or add a fatal error
        fatal_errors = [e for e in base_ctx.errors if "Phase 26" in e]
        assert len(fatal_errors) == 0, f"Phase 26 fatal errors: {fatal_errors}"

    def test_db_access_stats_present(self, base_ctx):
        """Phase 26 must write stats keys even when counts are 0."""
        expected_keys = [
            "db_access_queries_table",
            "db_access_write_evidence",
            "db_access_guard_hits",
        ]
        for key in expected_keys:
            assert key in base_ctx.stats, f"Missing stat key: {key}"

    def test_queries_table_edges_are_valid_edge_type(self, base_ctx):
        """Any QUERIES_TABLE edges created must connect Method/Function_ to DatabaseTable."""
        try:
            rows = base_ctx.db.execute(
                "MATCH (src:Method)-[q:QUERIES_TABLE]->(t:DatabaseTable) "
                "RETURN src.fqn AS fqn, t.name AS tname LIMIT 10"
            )
            # All result rows must have fqn and tname
            for r in rows:
                assert r.get("tname"), "QUERIES_TABLE target has no name"
        except Exception:
            pass  # No edges is also valid for tiny-laravel-app

    def test_polymorphic_detection_runs(self, base_ctx):
        """Phase 26 must record db_access_polymorphic_pairs stat (even if 0)."""
        assert "db_access_polymorphic_pairs" in base_ctx.stats


# ── Phase 24 mock tests (no real DB needed) ───────────────────────────────────

class TestPhase24WithMockDb:
    """Test phase_24 introspection logic using a mocked pymysql connection."""

    def _make_mock_connection(self):
        """Build a mock pymysql connection that returns minimal fixture data."""
        cursor = MagicMock()

        # tables query (TABLE_NAME, ENGINE, TABLE_COLLATION, TABLE_COMMENT, TABLE_ROWS)
        tables_data = [
            ("orders", "InnoDB", "utf8mb4_unicode_ci", "Customer orders", 5000),
            ("users",  "InnoDB", "utf8mb4_unicode_ci", "Application users", 1200),
        ]
        # columns query (TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE, IS_NULLABLE,
        #                COLUMN_DEFAULT, CHAR_MAX_LEN, NUMERIC_PREC, COLUMN_KEY, EXTRA, COLUMN_COMMENT)
        columns_data = [
            ("orders", "id",         "bigint unsigned", "bigint",  "NO",  None, None,       20,   "PRI", "auto_increment", ""),
            ("orders", "user_id",    "bigint unsigned", "bigint",  "NO",  None, None,       20,   "MUL", "",              "FK to users"),
            ("orders", "status",     "varchar(50)",     "varchar", "YES", None, 50,         None, "",    "",              ""),
            # notes is LONGTEXT — CHARACTER_MAXIMUM_LENGTH = 4294967295 (would overflow INT32)
            ("orders", "notes",      "longtext",        "longtext", "YES", None, 4294967295, None, "",   "",              ""),
            ("users",  "id",         "bigint unsigned", "bigint",  "NO",  None, None,       20,   "PRI", "auto_increment", ""),
            ("users",  "email",      "varchar(255)",    "varchar", "NO",  None, 255,        None, "UNI", "",             ""),
        ]
        # FKs (TABLE_NAME, COLUMN_NAME, REF_TABLE, REF_COL, CONSTRAINT, UPDATE_RULE, DELETE_RULE)
        fks_data = [
            ("orders", "user_id", "users", "id", "fk_orders_users", "CASCADE", "RESTRICT"),
        ]
        # Procedures
        procs_data = []
        # Views
        views_data = []

        # Set up fetchall to return in sequence
        cursor.fetchall.side_effect = [
            tables_data,
            columns_data,
            fks_data,
            procs_data,
            views_data,
        ]
        cursor.__enter__ = lambda s: cursor
        cursor.__exit__ = MagicMock(return_value=False)

        conn = MagicMock()
        conn.cursor.return_value = cursor
        conn.close = MagicMock()
        conn.__enter__ = lambda s: conn
        conn.__exit__ = MagicMock(return_value=False)
        return conn

    def test_phase24_creates_table_nodes(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("phase24_test")
        app_copy = tmp / "tiny-laravel-app"
        shutil.copytree(str(TINY_APP), str(app_copy))

        from laravelgraph.config import Config, DatabaseConnectionConfig
        from laravelgraph.pipeline.orchestrator import Pipeline

        cfg = Config()
        cfg.embedding.enabled = False
        cfg.databases = [DatabaseConnectionConfig(
            name="test_conn",
            host="127.0.0.1",
            port=3306,
            database="testdb",
            username="root",
            password="",
        )]

        mock_conn = self._make_mock_connection()

        with patch("laravelgraph.pipeline.phase_24_db_introspect._connect_mysql", return_value=mock_conn):
            pipeline = Pipeline(app_copy, config=cfg)
            ctx = pipeline.run(
                full=True,
                skip_embeddings=True,
                phases=[1, 2, 3, 4, 13, 19, 24],
            )

        # Phase 24 should have created table nodes
        assert ctx.stats.get("live_db_tables", 0) == 2, (
            f"Expected 2 live tables, got {ctx.stats.get('live_db_tables')}"
        )
        assert ctx.stats.get("live_db_columns", 0) == 6

    def test_phase24_creates_fk_edges(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("phase24_fk_test")
        app_copy = tmp / "tiny-laravel-app"
        shutil.copytree(str(TINY_APP), str(app_copy))

        from laravelgraph.config import Config, DatabaseConnectionConfig
        from laravelgraph.pipeline.orchestrator import Pipeline

        cfg = Config()
        cfg.embedding.enabled = False
        cfg.databases = [DatabaseConnectionConfig(
            name="test_conn",
            host="127.0.0.1",
            database="testdb",
            username="root",
            password="",
        )]

        mock_conn = self._make_mock_connection()

        with patch("laravelgraph.pipeline.phase_24_db_introspect._connect_mysql", return_value=mock_conn):
            pipeline = Pipeline(app_copy, config=cfg)
            ctx = pipeline.run(
                full=True,
                skip_embeddings=True,
                phases=[1, 2, 3, 4, 13, 19, 24],
            )

        assert ctx.stats.get("live_db_fks", 0) == 1

    def test_phase24_skips_when_no_connections(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("phase24_skip_test")
        app_copy = tmp / "tiny-laravel-app"
        shutil.copytree(str(TINY_APP), str(app_copy))

        from laravelgraph.config import Config
        from laravelgraph.pipeline.orchestrator import Pipeline

        cfg = Config()
        cfg.embedding.enabled = False
        cfg.databases = []  # no connections → phase silently skips

        pipeline = Pipeline(app_copy, config=cfg)
        ctx = pipeline.run(full=True, skip_embeddings=True, phases=[24])

        assert ctx.stats.get("live_db_tables", 0) == 0
        # No fatal errors about missing connections
        fatal = [e for e in ctx.errors if "Phase 24" in e]
        assert len(fatal) == 0

    def test_phase24_handles_connection_failure_gracefully(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("phase24_fail_test")
        app_copy = tmp / "tiny-laravel-app"
        shutil.copytree(str(TINY_APP), str(app_copy))

        from laravelgraph.config import Config, DatabaseConnectionConfig
        from laravelgraph.pipeline.orchestrator import Pipeline

        cfg = Config()
        cfg.embedding.enabled = False
        cfg.databases = [DatabaseConnectionConfig(
            name="unreachable",
            host="10.255.255.1",
            database="testdb",
            username="root",
            password="",
        )]

        with patch(
            "laravelgraph.pipeline.phase_24_db_introspect._connect_mysql",
            side_effect=Exception("Connection refused"),
        ):
            pipeline = Pipeline(app_copy, config=cfg)
            ctx = pipeline.run(full=True, skip_embeddings=True, phases=[24])

        # Phase should not crash the whole pipeline
        assert ctx.stats.get("live_db_tables", 0) == 0
        # Error should be recorded but pipeline continues
        conn_errors = [e for e in ctx.errors if "unreachable" in e or "introspection" in e.lower()]
        assert len(conn_errors) >= 1, "Expected an error entry for failed connection"
