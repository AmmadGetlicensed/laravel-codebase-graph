"""Unit tests for phase_24 pure helper functions (no DB, no network)."""

from __future__ import annotations

import pytest

from laravelgraph.pipeline.phase_24_db_introspect import (
    _normalise_type,
    _parse_sql_tables,
    _resolve_env,
)


class TestNormaliseType:
    def test_bigint_to_biginteger(self):
        assert _normalise_type("bigint") == "biginteger"

    def test_int_to_integer(self):
        assert _normalise_type("int") == "integer"

    def test_tinyint_to_tinyinteger(self):
        assert _normalise_type("tinyint") == "tinyinteger"

    def test_varchar_to_string(self):
        assert _normalise_type("varchar") == "string"

    def test_char_to_string(self):
        assert _normalise_type("char") == "string"

    def test_text_variants_to_text(self):
        for t in ("text", "tinytext", "mediumtext", "longtext"):
            assert _normalise_type(t) == "text", f"Expected 'text' for {t}"

    def test_datetime_preserved(self):
        assert _normalise_type("datetime") == "datetime"

    def test_timestamp_preserved(self):
        assert _normalise_type("timestamp") == "timestamp"

    def test_json_preserved(self):
        assert _normalise_type("json") == "json"

    def test_enum_preserved(self):
        assert _normalise_type("enum") == "enum"

    def test_boolean_variants(self):
        assert _normalise_type("bool") == "boolean"
        assert _normalise_type("boolean") == "boolean"
        assert _normalise_type("bit") == "boolean"

    def test_float_preserved(self):
        assert _normalise_type("float") == "float"

    def test_decimal_preserved(self):
        assert _normalise_type("decimal") == "decimal"
        assert _normalise_type("numeric") == "decimal"

    def test_case_insensitive(self):
        assert _normalise_type("BIGINT") == "biginteger"
        assert _normalise_type("VARCHAR") == "string"
        assert _normalise_type("DateTime") == "datetime"

    def test_unknown_type_passthrough(self):
        assert _normalise_type("geometry") == "geometry"
        assert _normalise_type("point") == "point"

    def test_uuid_to_string(self):
        assert _normalise_type("uuid") == "string"

    def test_double_to_double(self):
        assert _normalise_type("double") == "double"


class TestParseSqlTables:
    def test_simple_select(self):
        body = "SELECT * FROM orders WHERE id = 1"
        reads, writes = _parse_sql_tables(body)
        assert "orders" in reads
        assert writes == []

    def test_insert_into(self):
        body = "INSERT INTO audit_logs (user_id, action) VALUES (1, 'login')"
        reads, writes = _parse_sql_tables(body)
        assert "audit_logs" in writes

    def test_update_statement(self):
        body = "UPDATE users SET status = 'active' WHERE id = 5"
        reads, writes = _parse_sql_tables(body)
        assert "users" in writes

    def test_delete_from(self):
        body = "DELETE FROM sessions WHERE expires_at < NOW()"
        reads, writes = _parse_sql_tables(body)
        assert "sessions" in writes

    def test_join_detected_as_read(self):
        body = "SELECT o.id, u.name FROM orders o JOIN users u ON o.user_id = u.id"
        reads, writes = _parse_sql_tables(body)
        assert "orders" in reads
        assert "users" in reads

    def test_complex_procedure_body(self):
        body = """
        BEGIN
            SELECT id, total FROM orders WHERE status = 'pending';
            INSERT INTO order_archive SELECT * FROM orders WHERE created_at < DATE_SUB(NOW(), INTERVAL 1 YEAR);
            DELETE FROM orders WHERE created_at < DATE_SUB(NOW(), INTERVAL 1 YEAR);
        END
        """
        reads, writes = _parse_sql_tables(body)
        assert "orders" in reads
        assert "order_archive" in writes
        assert "orders" in writes  # DELETE

    def test_sql_keywords_not_in_results(self):
        """SQL keywords like VALUES, SET, NULL should not appear as table names."""
        body = "INSERT INTO logs (key, value) VALUES (NULL, 'test')"
        reads, writes = _parse_sql_tables(body)
        assert "values" not in reads
        assert "null" not in reads
        assert "set" not in reads

    def test_empty_body(self):
        reads, writes = _parse_sql_tables("")
        assert reads == []
        assert writes == []

    def test_case_insensitive_sql(self):
        body = "select * from users where id = 1"
        reads, writes = _parse_sql_tables(body)
        assert "users" in reads

    def test_replace_into_is_write(self):
        body = "REPLACE INTO cache (key, value) VALUES ('foo', 'bar')"
        reads, writes = _parse_sql_tables(body)
        assert "cache" in writes

    def test_truncate_table_is_write(self):
        body = "TRUNCATE TABLE temp_results"
        reads, writes = _parse_sql_tables(body)
        assert "temp_results" in writes

    def test_deduplication(self):
        """Same table name mentioned multiple times should appear only once."""
        body = "SELECT * FROM orders; SELECT count(*) FROM orders"
        reads, writes = _parse_sql_tables(body)
        assert reads.count("orders") == 1


class TestResolveEnv:
    def test_no_env_var_unchanged(self):
        assert _resolve_env("plain_password") == "plain_password"

    def test_env_var_expanded(self, monkeypatch):
        monkeypatch.setenv("DB_PASSWORD", "secret123")
        assert _resolve_env("${DB_PASSWORD}") == "secret123"

    def test_missing_env_var_returns_empty(self):
        # LARAVELGRAPH_TEST_MISSING should not be set in CI
        import os
        os.environ.pop("LARAVELGRAPH_TEST_MISSING", None)
        assert _resolve_env("${LARAVELGRAPH_TEST_MISSING}") == ""

    def test_multiple_env_vars(self, monkeypatch):
        monkeypatch.setenv("HOST", "mydb.rds.amazonaws.com")
        monkeypatch.setenv("PORT", "3306")
        result = _resolve_env("${HOST}:${PORT}")
        assert result == "mydb.rds.amazonaws.com:3306"

    def test_mixed_literal_and_env_var(self, monkeypatch):
        monkeypatch.setenv("DB_NAME", "myapp")
        result = _resolve_env("prefix_${DB_NAME}_suffix")
        assert result == "prefix_myapp_suffix"
