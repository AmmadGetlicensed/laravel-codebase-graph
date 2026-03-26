"""Unit tests for QueryResultCache and SQL safety validator."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from laravelgraph.mcp.query_cache import QueryResultCache, validate_sql


# ── validate_sql ──────────────────────────────────────────────────────────────

class TestValidateSql:
    def test_select_allowed(self):
        assert validate_sql("SELECT * FROM users") is None

    def test_select_with_where(self):
        assert validate_sql("SELECT id, name FROM orders WHERE status = 'active'") is None

    def test_show_allowed(self):
        assert validate_sql("SHOW TABLES") is None
        assert validate_sql("SHOW CREATE TABLE users") is None

    def test_describe_allowed(self):
        assert validate_sql("DESCRIBE users") is None
        assert validate_sql("DESC orders") is None

    def test_explain_allowed(self):
        assert validate_sql("EXPLAIN SELECT * FROM users") is None

    def test_case_insensitive(self):
        assert validate_sql("select * from users") is None
        assert validate_sql("  SELECT id FROM plans  ") is None

    def test_insert_rejected(self):
        assert validate_sql("INSERT INTO users VALUES (1, 'x')") is not None

    def test_update_rejected(self):
        assert validate_sql("UPDATE users SET name='x' WHERE id=1") is not None

    def test_delete_rejected(self):
        assert validate_sql("DELETE FROM users WHERE id=1") is not None

    def test_drop_rejected(self):
        assert validate_sql("DROP TABLE users") is not None

    def test_truncate_rejected(self):
        assert validate_sql("TRUNCATE TABLE users") is not None

    def test_alter_rejected(self):
        assert validate_sql("ALTER TABLE users ADD COLUMN foo INT") is not None

    def test_create_rejected(self):
        assert validate_sql("CREATE TABLE foo (id INT)") is not None

    def test_empty_rejected(self):
        assert validate_sql("") is not None
        assert validate_sql("   ") is not None

    def test_call_rejected(self):
        assert validate_sql("CALL my_procedure()") is not None

    def test_dangerous_keyword_in_subquery_rejected(self):
        # Should still be caught even if it starts with SELECT
        assert validate_sql("SELECT * FROM users; DELETE FROM users") is not None


# ── QueryResultCache ──────────────────────────────────────────────────────────

_SAMPLE_COLS = ["id", "name", "value"]
_SAMPLE_ROWS = [
    {"id": 1, "name": "active", "value": "1"},
    {"id": 2, "name": "inactive", "value": "0"},
]


class TestQueryResultCache:
    @pytest.fixture
    def cache(self, tmp_path):
        return QueryResultCache(tmp_path)

    def test_make_key_deterministic(self, cache):
        k1 = cache.make_key("mydb", "SELECT * FROM users")
        k2 = cache.make_key("mydb", "SELECT * FROM users")
        assert k1 == k2

    def test_make_key_normalises_whitespace(self, cache):
        k1 = cache.make_key("mydb", "SELECT  *  FROM  users")
        k2 = cache.make_key("mydb", "SELECT * FROM users")
        assert k1 == k2

    def test_make_key_case_insensitive(self, cache):
        k1 = cache.make_key("mydb", "select * from users")
        k2 = cache.make_key("mydb", "SELECT * FROM USERS")
        assert k1 == k2

    def test_make_key_different_connections_differ(self, cache):
        k1 = cache.make_key("db1", "SELECT * FROM users")
        k2 = cache.make_key("db2", "SELECT * FROM users")
        assert k1 != k2

    def test_set_and_get(self, cache):
        key = cache.make_key("mydb", "SELECT * FROM plans")
        cache.set(key, "SELECT * FROM plans", "mydb", _SAMPLE_COLS, _SAMPLE_ROWS, ttl=300)
        result = cache.get(key)
        assert result is not None
        assert result["columns"] == _SAMPLE_COLS
        assert result["rows"] == _SAMPLE_ROWS
        assert result["row_count"] == 2
        assert result["connection"] == "mydb"

    def test_get_returns_none_for_missing(self, cache):
        assert cache.get("qcache:mydb:nonexistent") is None

    def test_ttl_expiry(self, cache):
        key = cache.make_key("mydb", "SELECT * FROM plans")
        cache.set(key, "SELECT * FROM plans", "mydb", _SAMPLE_COLS, _SAMPLE_ROWS, ttl=1)
        assert cache.get(key, ttl=1) is not None
        time.sleep(1.1)
        assert cache.get(key, ttl=1) is None

    def test_ttl_zero_disables_expiry_check(self, cache):
        # ttl=0 on the connection means bypass caching — but the cache class
        # itself does not enforce this; the caller decides. So stored entries
        # with ttl=0 should still be retrievable (age > 0 > ttl is always True,
        # but callers skip cache.get when ttl=0 anyway).
        key = cache.make_key("mydb", "SELECT 1")
        cache.set(key, "SELECT 1", "mydb", ["x"], [{"x": 1}], ttl=0)
        # With ttl=0 override: 0 seconds means immediately expired
        assert cache.get(key, ttl=0) is None

    def test_invalidate_connection(self, cache):
        k1 = cache.make_key("db1", "SELECT * FROM a")
        k2 = cache.make_key("db1", "SELECT * FROM b")
        k3 = cache.make_key("db2", "SELECT * FROM a")
        for k in (k1, k2, k3):
            cache.set(k, "SELECT *", k.split(":")[1], ["x"], [], ttl=300)
        removed = cache.invalidate_connection("db1")
        assert removed == 2
        assert cache.get(k1) is None
        assert cache.get(k2) is None
        assert cache.get(k3) is not None

    def test_clear_all(self, cache):
        for i in range(5):
            k = cache.make_key("mydb", f"SELECT {i}")
            cache.set(k, f"SELECT {i}", "mydb", ["x"], [], ttl=300)
        removed = cache.clear_all()
        assert removed == 5
        assert cache.stats()["cached_entries"] == 0

    def test_evict_expired(self, cache):
        k_live = cache.make_key("mydb", "SELECT 1")
        k_expired = cache.make_key("mydb", "SELECT 2")
        cache.set(k_live, "SELECT 1", "mydb", ["x"], [], ttl=300)
        cache.set(k_expired, "SELECT 2", "mydb", ["x"], [], ttl=1)
        time.sleep(1.1)
        removed = cache.evict_expired()
        assert removed == 1
        assert cache.get(k_live) is not None

    def test_persists_to_disk(self, tmp_path):
        c1 = QueryResultCache(tmp_path)
        key = c1.make_key("mydb", "SELECT * FROM status_types")
        c1.set(key, "SELECT * FROM status_types", "mydb", ["id", "label"], [{"id": 1, "label": "active"}], ttl=300)

        c2 = QueryResultCache(tmp_path)  # fresh load from same dir
        result = c2.get(key)
        assert result is not None
        assert result["rows"] == [{"id": 1, "label": "active"}]

    def test_stats(self, cache):
        k1 = cache.make_key("db1", "SELECT 1")
        k2 = cache.make_key("db2", "SELECT 2")
        cache.set(k1, "SELECT 1", "db1", ["x"], [], ttl=300)
        cache.set(k2, "SELECT 2", "db2", ["x"], [], ttl=300)
        stats = cache.stats()
        assert stats["cached_entries"] == 2
        assert stats["live_entries"] == 2
        assert stats["by_connection"]["db1"] == 1
        assert stats["by_connection"]["db2"] == 1
