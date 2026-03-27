"""Unit tests for _resolve_symbol disambiguation logic.

Tests that when multiple classes share the same short name (e.g. BookingController
exists in both App\\Http\\Controllers and App\\Http\\Controllers\\Reseller), the
canonical top-level namespace is preferred.

These tests use a mock GraphDB that returns scripted query results, so no real
database or pipeline is required.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, call, patch


def _make_db(query_results: dict[str, list[dict]]) -> MagicMock:
    """Build a MagicMock GraphDB whose execute() returns values from a dict
    keyed by a substring of the query string.  Falls back to [] for unknown queries.
    """
    def execute_side_effect(query: str, params=None):
        for key, rows in query_results.items():
            if key in query:
                return rows
        return []

    db = MagicMock()
    db.execute.side_effect = execute_side_effect
    return db


def _call_resolve(db, symbol: str):
    """Import and call _resolve_symbol with an injected db."""
    from laravelgraph.mcp.server import _resolve_symbol
    return _resolve_symbol(db, symbol)


# ── Exact node_id match ───────────────────────────────────────────────────────

class TestResolveSymbolExactNodeId:
    def test_exact_node_id_returns_immediately(self):
        db = _make_db({"n.node_id = $s": [{"n.node_id": "class:App\\Http\\Controllers\\UserController", "n.fqn": "App\\Http\\Controllers\\UserController", "_label": "Class_"}]})
        result = _call_resolve(db, "class:App\\Http\\Controllers\\UserController")
        assert result is not None
        assert result.get("fqn") == "App\\Http\\Controllers\\UserController"


# ── Exact FQN match ───────────────────────────────────────────────────────────

class TestResolveSymbolExactFqn:
    def test_exact_fqn_match(self):
        db = _make_db({"n.fqn = $s": [{"n.fqn": "App\\Http\\Controllers\\UserController", "n.name": "UserController", "_label": "Class_"}]})
        result = _call_resolve(db, "App\\Http\\Controllers\\UserController")
        assert result is not None
        assert result.get("fqn") == "App\\Http\\Controllers\\UserController"


# ── Class name disambiguation ─────────────────────────────────────────────────

class TestResolveSymbolClassNameDisambiguation:
    """When multiple classes share a short name, prefer the canonical (shorter FQN) one."""

    def _make_multi_match_db(self):
        """Return a db that simulates two BookingController matches."""
        canonical = {
            "n.fqn": "App\\Http\\Controllers\\BookingController",
            "n.name": "BookingController",
            "n.file_path": "app/Http/Controllers/BookingController.php",
            "_label": "Class_",
        }
        reseller = {
            "n.fqn": "App\\Http\\Controllers\\Reseller\\BookingController",
            "n.name": "BookingController",
            "n.file_path": "app/Http/Controllers/Reseller/BookingController.php",
            "_label": "Class_",
        }

        def execute_side_effect(query: str, params=None):
            if "n.node_id = $s" in query:
                return []
            if "n.fqn = $s" in query:
                return []
            if "n.name = $s" in query and "Class_" in query:
                return [reseller, canonical]  # reseller returned first — canonical should still win
            if "n.name = $s" in query:
                return []
            return []

        db = MagicMock()
        db.execute.side_effect = execute_side_effect
        return db

    def test_prefers_canonical_over_reseller_namespace(self):
        db = self._make_multi_match_db()
        result = _call_resolve(db, "BookingController")
        assert result is not None
        fqn = result.get("fqn", "")
        assert "Reseller" not in fqn, (
            f"Expected canonical BookingController, got {fqn}"
        )
        assert "App\\Http\\Controllers\\BookingController" == fqn

    def test_prefers_shorter_fqn_when_no_http_controllers_path(self):
        """When there's no Http\\Controllers path, shorter FQN wins."""
        short_fqn = {
            "n.fqn": "App\\Services\\UserService",
            "n.name": "UserService",
            "n.file_path": "app/Services/UserService.php",
            "_label": "Class_",
        }
        deep_fqn = {
            "n.fqn": "App\\Services\\Admin\\Reports\\UserService",
            "n.name": "UserService",
            "n.file_path": "app/Services/Admin/Reports/UserService.php",
            "_label": "Class_",
        }

        def execute_side_effect(query: str, params=None):
            if "n.node_id = $s" in query:
                return []
            if "n.fqn = $s" in query:
                return []
            if "n.name = $s" in query and "Class_" in query:
                return [deep_fqn, short_fqn]
            return []

        db = MagicMock()
        db.execute.side_effect = execute_side_effect
        result = _call_resolve(db, "UserService")
        assert result is not None
        assert result.get("fqn") == "App\\Services\\UserService"

    def test_single_match_returned_directly(self):
        """With only one candidate, no ranking needed."""
        single = {
            "n.fqn": "App\\Http\\Controllers\\PostController",
            "n.name": "PostController",
            "n.file_path": "app/Http/Controllers/PostController.php",
            "_label": "Class_",
        }

        def execute_side_effect(query: str, params=None):
            if "n.node_id = $s" in query:
                return []
            if "n.fqn = $s" in query:
                return []
            if "n.name = $s" in query and "Class_" in query:
                return [single]
            return []

        db = MagicMock()
        db.execute.side_effect = execute_side_effect
        result = _call_resolve(db, "PostController")
        assert result is not None
        assert result.get("fqn") == "App\\Http\\Controllers\\PostController"


# ── Not found ─────────────────────────────────────────────────────────────────

class TestResolveSymbolNotFound:
    def test_returns_none_for_unknown_symbol(self):
        db = _make_db({})
        result = _call_resolve(db, "NonExistentClass")
        assert result is None

    def test_returns_none_for_empty_string(self):
        db = _make_db({})
        result = _call_resolve(db, "")
        assert result is None


# ── FQN contains (partial match) ──────────────────────────────────────────────

class TestResolveSymbolFqnContains:
    """Step 5: partial FQN match with shortest-FQN preference."""

    def test_partial_fqn_prefers_shorter(self):
        short = {
            "n.fqn": "App\\Services\\OrderService",
            "n.name": "OrderService",
            "_label": "Class_",
        }
        deep = {
            "n.fqn": "App\\Services\\Admin\\Reporting\\OrderService",
            "n.name": "ReportOrderService",
            "_label": "Class_",
        }

        def execute_side_effect(query: str, params=None):
            if "n.node_id = $s" in query:
                return []
            if "n.fqn = $s" in query:
                return []
            if "n.name = $s" in query:
                return []
            if "n.fqn CONTAINS $s" in query and "Class_" in query:
                return [deep, short]
            return []

        db = MagicMock()
        db.execute.side_effect = execute_side_effect
        result = _call_resolve(db, "OrderService")
        assert result is not None
        assert result.get("fqn") == "App\\Services\\OrderService"

    def test_partial_fqn_returns_none_when_no_candidates(self):
        db = _make_db({})
        result = _call_resolve(db, "CompletelyUnknown")
        assert result is None


# ── _normalize_node strips prefix ─────────────────────────────────────────────

class TestNormalizeNode:
    def test_normalize_strips_n_dot_prefix(self):
        from laravelgraph.mcp.server import _normalize_node
        raw = {"n.fqn": "Foo\\Bar", "n.name": "Bar", "n.node_id": "class:Foo\\Bar", "_label": "Class_"}
        normalized = _normalize_node(raw)
        assert normalized == {"fqn": "Foo\\Bar", "name": "Bar", "node_id": "class:Foo\\Bar", "_label": "Class_"}

    def test_normalize_leaves_non_n_dot_keys_unchanged(self):
        from laravelgraph.mcp.server import _normalize_node
        raw = {"_label": "Class_", "fqn": "already_clean"}
        normalized = _normalize_node(raw)
        assert normalized == {"_label": "Class_", "fqn": "already_clean"}
