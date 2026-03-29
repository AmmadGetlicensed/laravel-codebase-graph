"""Unit tests for migrate_plugin_store_tool().

Verifies that the migration function:
- Detects old-style store_discoveries (no `findings` param)
- Replaces it with the new signature (findings: str)
- Is idempotent (does not modify already-new-style plugins)
- Handles edge cases (missing function, template plugins)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _import_migrate():
    from laravelgraph.plugins.generator import migrate_plugin_store_tool
    return migrate_plugin_store_tool


_OLD_PLUGIN_TEMPLATE = """\
PLUGIN_MANIFEST = {{
    "name": "{slug}",
    "version": "1.0.0",
    "description": "Test plugin.",
    "tool_prefix": "{prefix}",
}}


def register_tools(mcp, db=None, sql_db=None):
    @mcp.tool()
    def {stem}_summary() -> str:
        "Summary."
        return "summary"

    @mcp.tool()
    def {stem}_list() -> str:
        "List."
        rows = db().execute("MATCH (r:Route) RETURN r.method AS m LIMIT 10")
        return "\\n".join(str(r) for r in rows)

    @mcp.tool()
    def {stem}_store_discoveries() -> str:
        "Old store_discoveries with no findings param."
        rows = db().execute("MATCH (r:Route) RETURN r.method AS m LIMIT 5")
        return "\\n".join(str(r) for r in rows)
"""

_NEW_PLUGIN_TEMPLATE = """\
PLUGIN_MANIFEST = {{
    "name": "{slug}",
    "version": "1.0.0",
    "description": "Test plugin.",
    "tool_prefix": "{prefix}",
}}


def register_tools(mcp, db=None, sql_db=None):
    @mcp.tool()
    def {stem}_summary() -> str:
        "Summary."
        return "summary"

    @mcp.tool()
    def {stem}_store_discoveries(findings: str) -> str:
        "New store_discoveries with findings param."
        return "stored: " + findings
"""


def _write_plugin(tmp_path: Path, slug: str, prefix: str, old: bool = True) -> Path:
    stem = prefix.rstrip("_")
    template = _OLD_PLUGIN_TEMPLATE if old else _NEW_PLUGIN_TEMPLATE
    code = template.format(slug=slug, prefix=prefix, stem=stem)
    p = tmp_path / f"{slug}.py"
    p.write_text(code, encoding="utf-8")
    return p


# ── detection tests ───────────────────────────────────────────────────────────

class TestMigrateDetection:
    def test_old_plugin_is_detected(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=True)
        result = migrate(p, "usr_", "user-explorer")
        assert result is True

    def test_new_plugin_is_not_migrated(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=False)
        result = migrate(p, "usr_", "user-explorer")
        assert result is False

    def test_file_unchanged_when_already_new_style(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=False)
        content_before = p.read_text()
        migrate(p, "usr_", "user-explorer")
        assert p.read_text() == content_before

    def test_idempotent_second_call_returns_false(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "order-flow", "ord_", old=True)
        assert migrate(p, "ord_", "order-flow") is True
        assert migrate(p, "ord_", "order-flow") is False  # Already migrated


# ── replacement correctness tests ─────────────────────────────────────────────

class TestMigrateReplacement:
    def test_migrated_file_has_findings_param(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=True)
        migrate(p, "usr_", "user-explorer")
        updated = p.read_text()
        assert "def usr_store_discoveries(findings: str)" in updated

    def test_migrated_file_no_longer_has_old_signature(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=True)
        migrate(p, "usr_", "user-explorer")
        updated = p.read_text()
        assert "def usr_store_discoveries() -> str:" not in updated

    def test_migrated_file_stores_discovery_label(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "courses", "crs_", old=True)
        migrate(p, "crs_", "courses")
        assert '"Discovery"' in p.read_text()

    def test_migrated_file_has_correct_slug(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "my-domain", "myd_", old=True)
        migrate(p, "myd_", "my-domain")
        assert '"my-domain"' in p.read_text()

    def test_migrated_file_is_valid_python(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "order-flow", "ord_", old=True)
        migrate(p, "ord_", "order-flow")
        ast.parse(p.read_text())  # raises SyntaxError if invalid

    def test_other_tools_preserved_after_migration(self, tmp_path):
        """The summary and list tools must not be touched by migration."""
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=True)
        migrate(p, "usr_", "user-explorer")
        updated = p.read_text()
        assert "def usr_summary() -> str:" in updated
        assert "def usr_list() -> str:" in updated

    def test_plugin_manifest_preserved_after_migration(self, tmp_path):
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "user-explorer", "usr_", old=True)
        migrate(p, "usr_", "user-explorer")
        updated = p.read_text()
        assert 'PLUGIN_MANIFEST' in updated
        assert '"name": "user-explorer"' in updated


# ── edge cases ────────────────────────────────────────────────────────────────

class TestMigrateEdgeCases:
    def test_returns_false_when_function_not_found(self, tmp_path):
        """Plugin with no store_discoveries at all — should not error."""
        migrate = _import_migrate()
        p = tmp_path / "no-store.py"
        p.write_text("PLUGIN_MANIFEST = {}\ndef register_tools(mcp): pass\n")
        result = migrate(p, "x_", "no-store")
        assert result is False

    def test_slug_with_hyphens_produces_valid_python(self, tmp_path):
        """Slug with hyphens (most common form) must produce valid Python after migration."""
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "booking-and-order", "bo_", old=True)
        migrate(p, "bo_", "booking-and-order")
        ast.parse(p.read_text())

    def test_different_prefixes_are_handled(self, tmp_path):
        """Migration must use the correct function name for each prefix."""
        migrate = _import_migrate()
        p = _write_plugin(tmp_path, "webhook", "web_", old=True)
        migrate(p, "web_", "webhook")
        updated = p.read_text()
        assert "def web_store_discoveries(findings: str)" in updated
        assert "def web_store_discoveries() -> str:" not in updated


# ── _build_query_tool error handling tests ────────────────────────────────────

class TestQueryToolErrorHandling:
    """Verify the generated query tool catches Cypher binder errors gracefully."""

    def _make_spec(self, cypher: str = "MATCH (r:Route) RETURN r.http_method AS m LIMIT 5"):
        return {
            "name": "tst_routes",
            "description": "Test routes.",
            "cypher_query": cypher,
            "result_format": "{m}",
        }

    def test_generated_code_has_try_except(self):
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec(), slug="test-plugin")
        assert "try:" in code
        assert "except Exception as _e:" in code

    def test_generated_code_checks_binder_exception(self):
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec(), slug="test-plugin")
        assert "Binder exception" in code

    def test_generated_code_checks_cannot_find_property(self):
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec(), slug="test-plugin")
        assert "Cannot find property" in code

    def test_generated_code_suggests_update_plugin(self):
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec(), slug="test-plugin")
        assert "laravelgraph_update_plugin" in code

    def test_generated_code_includes_slug_in_error_message(self):
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec(), slug="my-domain")
        assert "my-domain" in code

    def test_generated_code_is_valid_python(self):
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec(), slug="test-plugin")
        wrapped = f"def register_tools(mcp, db=None):\n{code}"
        ast.parse(wrapped)

    def test_generated_code_without_slug_is_still_valid(self):
        """slug is optional — empty slug still produces valid code."""
        from laravelgraph.plugins.generator import _build_query_tool
        code = _build_query_tool("tst_", self._make_spec())
        wrapped = f"def register_tools(mcp, db=None):\n{code}"
        ast.parse(wrapped)
