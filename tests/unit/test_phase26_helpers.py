"""Unit tests for phase_26 pure helper functions (no DB required)."""

from __future__ import annotations

import pytest

from laravelgraph.pipeline.phase_26_db_access import (
    _classify_operation,
    _infer_rhs,
    _method_at_line,
    _sql_operation,
)


# ── _classify_operation ───────────────────────────────────────────────────────

class TestClassifyOperation:
    def test_read_methods(self):
        for m in ("where", "find", "findOrFail", "get", "first", "all",
                  "count", "exists", "paginate", "pluck", "select"):
            assert _classify_operation(m) == "read", f"Expected 'read' for {m}"

    def test_write_methods(self):
        for m in ("create", "insert", "update", "delete", "destroy",
                  "forceDelete", "restore", "truncate", "upsert"):
            assert _classify_operation(m) == "write", f"Expected 'write' for {m}"

    def test_unknown_method_is_readwrite(self):
        assert _classify_operation("someCustomMethod") == "readwrite"
        assert _classify_operation("tap") == "readwrite"

    def test_first_or_create_is_write(self):
        assert _classify_operation("firstOrCreate") == "write"

    def test_chunk_is_read(self):
        assert _classify_operation("chunk") == "read"


class TestSqlOperation:
    def test_select_is_read(self):
        assert _sql_operation("select") == "read"

    def test_insert_is_write(self):
        assert _sql_operation("insert") == "write"

    def test_delete_is_write(self):
        assert _sql_operation("delete") == "write"

    def test_update_is_write(self):
        assert _sql_operation("update") == "write"

    def test_statement_is_readwrite(self):
        assert _sql_operation("statement") == "readwrite"


# ── _method_at_line ───────────────────────────────────────────────────────────

class TestMethodAtLine:
    def _methods(self):
        # (line_start, line_end, node_id, fqn)
        return [
            (10, 30, "method:Foo::bar",   "Foo::bar"),
            (40, 60, "method:Foo::baz",   "Foo::baz"),
            (70, 90, "method:Foo::qux",   "Foo::qux"),
        ]

    def test_line_inside_first_method(self):
        result = _method_at_line(self._methods(), 15)
        assert result == ("method:Foo::bar", "Foo::bar")

    def test_line_at_method_start(self):
        result = _method_at_line(self._methods(), 10)
        assert result == ("method:Foo::bar", "Foo::bar")

    def test_line_at_method_end(self):
        result = _method_at_line(self._methods(), 30)
        assert result == ("method:Foo::bar", "Foo::bar")

    def test_line_inside_second_method(self):
        result = _method_at_line(self._methods(), 50)
        assert result == ("method:Foo::baz", "Foo::baz")

    def test_line_inside_last_method(self):
        result = _method_at_line(self._methods(), 85)
        assert result == ("method:Foo::qux", "Foo::qux")

    def test_line_between_methods_returns_none(self):
        result = _method_at_line(self._methods(), 35)
        assert result is None

    def test_line_before_all_methods_returns_none(self):
        result = _method_at_line(self._methods(), 5)
        assert result is None

    def test_line_after_all_methods_returns_none(self):
        result = _method_at_line(self._methods(), 100)
        assert result is None

    def test_empty_list_returns_none(self):
        assert _method_at_line([], 15) is None

    def test_single_method_inside(self):
        methods = [(1, 100, "method:A::foo", "A::foo")]
        assert _method_at_line(methods, 50) == ("method:A::foo", "A::foo")

    def test_single_method_outside(self):
        methods = [(50, 100, "method:A::foo", "A::foo")]
        assert _method_at_line(methods, 10) is None


# ── _infer_rhs ────────────────────────────────────────────────────────────────

class TestInferRhs:
    def _model_lookup(self):
        return {
            "Order": {"nid": "model:Order", "fqn": "App\\Models\\Order", "db_table": "orders"},
            "User":  {"nid": "model:User",  "fqn": "App\\Models\\User",  "db_table": "users"},
        }

    # ── Literals ──────────────────────────────────────────────────────────────

    def test_null_literal(self):
        r = _infer_rhs("null", {})
        assert r["type"] == "literal"
        assert r["confidence"] == 1.0

    def test_zero_literal(self):
        r = _infer_rhs("0", {})
        assert r["type"] == "literal"

    def test_empty_string_literal(self):
        r = _infer_rhs("''", {})
        assert r["type"] == "literal"

    def test_true_literal(self):
        r = _infer_rhs("true", {})
        assert r["type"] == "literal"

    # ── Model property ($order->id) ───────────────────────────────────────────

    def test_model_property_known_model(self):
        r = _infer_rhs("$order->id", self._model_lookup())
        assert r["type"] == "model_property"
        assert r["target_table"] == "orders"
        assert r["target_column"] == "id"
        assert r["confidence"] == 0.85

    def test_model_property_different_prop(self):
        r = _infer_rhs("$user->uuid", self._model_lookup())
        assert r["type"] == "model_property"
        assert r["target_table"] == "users"
        assert r["target_column"] == "uuid"

    def test_model_property_unknown_variable(self):
        """Variable name doesn't match any known model — still detected as model_property pattern."""
        r = _infer_rhs("$invoice->id", self._model_lookup())
        # Should still parse as model_prop type but without resolved target_table
        assert r["type"] in ("model_property", "unknown")

    # ── Trailing semicolons stripped ──────────────────────────────────────────

    def test_trailing_semicolon_stripped(self):
        r = _infer_rhs("$order->id;", self._model_lookup())
        assert r["type"] == "model_property"

    # ── Confidence returned ───────────────────────────────────────────────────

    def test_all_results_have_confidence(self):
        for rhs in ("null", "$order->id", "$request->input()", "SomeClass::CONST"):
            r = _infer_rhs(rhs, self._model_lookup())
            assert "confidence" in r, f"Missing confidence for rhs={rhs!r}"
            assert 0.0 <= r["confidence"] <= 1.0

    def test_literal_has_high_confidence(self):
        r = _infer_rhs("null", {})
        assert r["confidence"] == 1.0

    def test_request_input_has_low_confidence(self):
        r = _infer_rhs("$request->input('name')", self._model_lookup())
        assert r["confidence"] < 0.5
