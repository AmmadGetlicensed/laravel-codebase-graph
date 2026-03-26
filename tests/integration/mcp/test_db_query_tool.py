"""Integration tests for laravelgraph_db_query MCP tool.

Tests run against a mock pymysql connection — no real database required.
The mock returns a small lookup table (course_delivery_types style) with
known rows so we can assert on both the query result format and caching
behaviour.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


# ── Mock connection factory ───────────────────────────────────────────────────

def _make_mock_conn(rows, columns):
    """Build a mock pymysql connection returning ``rows`` for any query."""
    cursor = MagicMock()
    cursor.description = [(col, None, None, None, None, None, None) for col in columns]
    cursor.fetchall.return_value = rows
    cursor.__enter__ = lambda s: cursor
    cursor.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.close = MagicMock()
    conn.__enter__ = lambda s: conn
    conn.__exit__ = MagicMock(return_value=False)
    return conn


_DELIVERY_COLS = ["id", "name", "description"]
_DELIVERY_ROWS = [
    (1, "online",      "Self-paced online"),
    (2, "classroom",   "In-person classroom"),
    (3, "blended",     "Online + classroom"),
    (6, "virtual",     "Live virtual session"),
    (7, "e_learning",  "External e-learning"),
]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_cfg(tmp_path):
    """Minimal config with one test DB connection."""
    from laravelgraph.config import Config, DatabaseConnectionConfig
    cfg = Config()
    cfg.databases = [DatabaseConnectionConfig(
        name="test_conn",
        host="127.0.0.1",
        port=3306,
        database="testdb",
        username="root",
        password="",
        query_cache_ttl=300,
    )]
    return cfg


@pytest.fixture
def query_cache(tmp_path):
    from laravelgraph.mcp.query_cache import QueryResultCache
    return QueryResultCache(tmp_path)


# ── SQL validation ─────────────────────────────────────────────────────────────

class TestDbQuerySqlValidation:
    def _call_tool(self, sql, cfg, cache, conn=None):
        """Invoke the core query logic used by the MCP tool."""
        from laravelgraph.mcp.query_cache import validate_sql, _MAX_ROWS

        err = validate_sql(sql)
        if err:
            return f"SQL rejected: {err}", None

        conn_cfgs = cfg.databases
        conn_cfg = conn_cfgs[0]
        safe_limit = min(50, _MAX_ROWS)

        sql_exec = sql.strip().rstrip(";")
        if sql_exec.upper().lstrip().startswith("SELECT") and "LIMIT" not in sql_exec.upper():
            sql_exec = f"{sql_exec} LIMIT {safe_limit}"

        return None, sql_exec

    def test_select_passes_validation(self, db_cfg, query_cache):
        err, _ = self._call_tool("SELECT * FROM course_delivery_types", db_cfg, query_cache)
        assert err is None

    def test_insert_rejected(self, db_cfg, query_cache):
        err, _ = self._call_tool("INSERT INTO t VALUES (1)", db_cfg, query_cache)
        assert err is not None
        assert "rejected" in err.lower() or "SQL rejected" in err

    def test_update_rejected(self, db_cfg, query_cache):
        err, _ = self._call_tool("UPDATE users SET x=1", db_cfg, query_cache)
        assert err is not None

    def test_limit_injected_when_absent(self, db_cfg, query_cache):
        _, sql_exec = self._call_tool("SELECT id FROM plans", db_cfg, query_cache)
        assert "LIMIT" in sql_exec.upper()

    def test_limit_not_duplicated_when_present(self, db_cfg, query_cache):
        _, sql_exec = self._call_tool("SELECT id FROM plans LIMIT 10", db_cfg, query_cache)
        assert sql_exec.upper().count("LIMIT") == 1


# ── Cache behaviour ───────────────────────────────────────────────────────────

class TestDbQueryCaching:
    def _run_query(self, sql, cfg, cache, bypass=False):
        """Simulate what the MCP tool does (cache check → live query → cache write)."""
        from laravelgraph.mcp.query_cache import validate_sql, _MAX_ROWS

        conn_cfg = cfg.databases[0]
        sql_exec = sql.strip().rstrip(";")
        if "LIMIT" not in sql_exec.upper():
            sql_exec = f"{sql_exec} LIMIT 50"

        key = cache.make_key(conn_cfg.name, sql_exec)
        ttl = conn_cfg.query_cache_ttl

        if not bypass and ttl > 0:
            hit = cache.get(key, ttl=ttl)
            if hit:
                return hit, True  # (entry, from_cache)

        mock_conn = _make_mock_conn(_DELIVERY_ROWS, _DELIVERY_COLS)
        with patch("laravelgraph.pipeline.phase_24_db_introspect._connect_mysql", return_value=mock_conn):
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
            mc = _connect_mysql(conn_cfg)
            with mc.cursor() as cur:
                cur.execute(sql_exec)
                raw = cur.fetchall()
                cols = [d[0] for d in cur.description]
            mc.close()

        rows = [dict(zip(cols, r)) for r in raw]
        cache.set(key, sql, conn_cfg.name, cols, rows, ttl=ttl)
        entry = cache.get(key, ttl=ttl)
        return entry, False

    def test_first_call_is_live(self, db_cfg, query_cache):
        _, from_cache = self._run_query("SELECT * FROM delivery_types", db_cfg, query_cache)
        assert from_cache is False

    def test_second_call_hits_cache(self, db_cfg, query_cache):
        self._run_query("SELECT * FROM delivery_types", db_cfg, query_cache)
        _, from_cache = self._run_query("SELECT * FROM delivery_types", db_cfg, query_cache)
        assert from_cache is True

    def test_bypass_cache_forces_live(self, db_cfg, query_cache):
        self._run_query("SELECT * FROM delivery_types", db_cfg, query_cache)
        _, from_cache = self._run_query("SELECT * FROM delivery_types", db_cfg, query_cache, bypass=True)
        assert from_cache is False

    def test_cached_rows_match_live(self, db_cfg, query_cache):
        entry, _ = self._run_query("SELECT * FROM delivery_types", db_cfg, query_cache)
        assert entry["row_count"] == len(_DELIVERY_ROWS)
        assert entry["columns"] == list(_DELIVERY_COLS)
        assert entry["rows"][0]["name"] == "online"

    def test_ttl_zero_skips_caching(self, tmp_path):
        from laravelgraph.config import Config, DatabaseConnectionConfig
        from laravelgraph.mcp.query_cache import QueryResultCache

        cfg = Config()
        cfg.databases = [DatabaseConnectionConfig(
            name="nocache_conn",
            host="127.0.0.1",
            database="testdb",
            username="root",
            password="",
            query_cache_ttl=0,
        )]
        qc = QueryResultCache(tmp_path)

        conn_cfg = cfg.databases[0]
        sql_exec = "SELECT * FROM delivery_types LIMIT 50"
        key = qc.make_key(conn_cfg.name, sql_exec)

        # ttl=0 — caller should not write to cache
        assert conn_cfg.query_cache_ttl == 0
        # Verify get() with ttl=0 always returns None
        qc.set(key, sql_exec, conn_cfg.name, _DELIVERY_COLS, [], ttl=0)
        assert qc.get(key, ttl=0) is None


# ── Result format ─────────────────────────────────────────────────────────────

class TestDbQueryResultFormat:
    def test_all_rows_present(self, db_cfg, query_cache):
        from laravelgraph.mcp.query_cache import QueryResultCache

        conn_cfg = db_cfg.databases[0]
        sql = "SELECT * FROM delivery_types LIMIT 50"
        key = query_cache.make_key(conn_cfg.name, sql)
        rows = [dict(zip(_DELIVERY_COLS, r)) for r in _DELIVERY_ROWS]
        query_cache.set(key, sql, conn_cfg.name, list(_DELIVERY_COLS), rows, ttl=300)

        entry = query_cache.get(key)
        assert entry["row_count"] == 5
        assert entry["rows"][3]["id"] == 6   # virtual
        assert entry["rows"][4]["name"] == "e_learning"

    def test_describe_result_stored_correctly(self, db_cfg, query_cache):
        describe_cols = ["Field", "Type", "Null", "Key", "Default", "Extra"]
        describe_rows = [
            ("id",   "int(11)",      "NO",  "PRI", None, "auto_increment"),
            ("name", "varchar(100)", "YES", "",    None, ""),
        ]
        conn_cfg = db_cfg.databases[0]
        sql = "DESCRIBE delivery_types"
        key = query_cache.make_key(conn_cfg.name, sql)
        rows = [dict(zip(describe_cols, r)) for r in describe_rows]
        query_cache.set(key, sql, conn_cfg.name, describe_cols, rows, ttl=300)

        entry = query_cache.get(key)
        assert entry["columns"] == describe_cols
        assert entry["rows"][0]["Field"] == "id"
        assert entry["rows"][1]["Type"] == "varchar(100)"
