"""Tests for plugin loader utilities: scan_plugin_manifests and _ToolCollector."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from laravelgraph.plugins.loader import _ToolCollector, scan_plugin_manifests

# ── Helpers ───────────────────────────────────────────────────────────────────

_PLUGIN_TEMPLATE = """\
PLUGIN_MANIFEST = {{
    "name": "{name}",
    "version": "1.0.0",
    "description": "{description}",
    "tool_prefix": "{prefix}",
}}


def register_tools(mcp, db=None, sql_db=None):
    @mcp.tool()
    def {prefix_stem}_summary() -> str:
        "Summary tool."
        return "summary"

    @mcp.tool()
    def {prefix_stem}_list() -> str:
        "List tool."
        return "list"

    @mcp.tool()
    def {prefix_stem}_store_discoveries() -> str:
        "Store tool."
        return "stored"
"""


def _write_plugin(tmp_path: Path, name: str, description: str, prefix: str) -> Path:
    stem = prefix.rstrip("_")
    code = _PLUGIN_TEMPLATE.format(
        name=name,
        description=description,
        prefix=prefix,
        prefix_stem=stem,
    )
    p = tmp_path / f"{name}.py"
    p.write_text(code, encoding="utf-8")
    return p


# ── scan_plugin_manifests ─────────────────────────────────────────────────────

class TestScanPluginManifests:
    def test_empty_dir_returns_empty_list(self, tmp_path):
        results = scan_plugin_manifests(tmp_path)
        assert results == []

    def test_nonexistent_dir_returns_empty_list(self, tmp_path):
        results = scan_plugin_manifests(tmp_path / "does_not_exist")
        assert results == []

    def test_single_plugin_found(self, tmp_path):
        _write_plugin(tmp_path, "user-explorer", "Explore user routes.", "usr_")
        results = scan_plugin_manifests(tmp_path)
        assert len(results) == 1

    def test_manifest_fields_populated(self, tmp_path):
        _write_plugin(tmp_path, "user-explorer", "Explore user routes.", "usr_")
        r = scan_plugin_manifests(tmp_path)[0]
        assert r["name"] == "user-explorer"
        assert r["description"] == "Explore user routes."
        assert r["tool_prefix"] == "usr_"

    def test_tool_names_extracted(self, tmp_path):
        _write_plugin(tmp_path, "user-explorer", "Explore user routes.", "usr_")
        r = scan_plugin_manifests(tmp_path)[0]
        assert "usr_summary" in r["tool_names"]
        assert "usr_list" in r["tool_names"]
        assert "usr_store_discoveries" in r["tool_names"]

    def test_multiple_plugins_all_found(self, tmp_path):
        _write_plugin(tmp_path, "user-explorer", "Users.", "usr_")
        _write_plugin(tmp_path, "order-lifecycle", "Orders.", "ord_")
        results = scan_plugin_manifests(tmp_path)
        names = {r["name"] for r in results}
        assert "user-explorer" in names
        assert "order-lifecycle" in names

    def test_path_field_is_absolute(self, tmp_path):
        _write_plugin(tmp_path, "user-explorer", "Users.", "usr_")
        r = scan_plugin_manifests(tmp_path)[0]
        assert r["path"].is_absolute()
        assert r["path"].exists()

    def test_file_without_manifest_skipped(self, tmp_path):
        # A .py file with no PLUGIN_MANIFEST should be silently skipped
        (tmp_path / "not_a_plugin.py").write_text("x = 1\n")
        results = scan_plugin_manifests(tmp_path)
        assert results == []

    def test_file_with_syntax_error_skipped(self, tmp_path):
        (tmp_path / "broken.py").write_text("PLUGIN_MANIFEST = {{{broken syntax")
        results = scan_plugin_manifests(tmp_path)
        assert results == []

    def test_plugins_sorted_alphabetically(self, tmp_path):
        _write_plugin(tmp_path, "z-last", "Z.", "z_")
        _write_plugin(tmp_path, "a-first", "A.", "a_")
        results = scan_plugin_manifests(tmp_path)
        assert results[0]["name"] == "a-first"
        assert results[1]["name"] == "z-last"


# ── _ToolCollector ────────────────────────────────────────────────────────────

class TestToolCollector:
    def test_collects_decorated_functions(self):
        collector = _ToolCollector()

        @collector.tool()
        def my_summary() -> str:
            return "summary"

        assert "my_summary" in collector.tools

    def test_collected_function_is_callable(self):
        collector = _ToolCollector()

        @collector.tool()
        def my_tool() -> str:
            return "result"

        assert collector.tools["my_tool"]() == "result"

    def test_multiple_tools_collected(self):
        collector = _ToolCollector()

        @collector.tool()
        def tool_a() -> str:
            return "a"

        @collector.tool()
        def tool_b() -> str:
            return "b"

        assert "tool_a" in collector.tools
        assert "tool_b" in collector.tools

    def test_unknown_attribute_does_not_raise(self):
        collector = _ToolCollector()
        # Plugins may call mcp.resource() or other methods — must not raise
        collector.resource("some://path")(lambda: "x")

    def test_tool_decorator_returns_original_function(self):
        collector = _ToolCollector()

        @collector.tool()
        def original() -> str:
            return "original"

        assert original() == "original"

    def test_register_tools_integration(self, tmp_path):
        """Full round-trip: write a plugin, collect its tools, call them."""
        _write_plugin(tmp_path, "test-plugin", "Test.", "tst_")
        from laravelgraph.plugins.loader import _import_plugin_module

        module = _import_plugin_module(
            tmp_path / "test-plugin.py",
            "laravelgraph_plugin_test_plugin",
        )
        collector = _ToolCollector()
        module.register_tools(collector)

        assert "tst_summary" in collector.tools
        assert "tst_list" in collector.tools
        assert "tst_store_discoveries" in collector.tools
        assert collector.tools["tst_summary"]() == "summary"
        assert collector.tools["tst_list"]() == "list"


# ── _ToolCollector with kwargs (tool_args hot-dispatch path) ──────────────────

class TestToolCollectorWithArgs:
    """Verify that tools accepting parameters can be called with keyword args."""

    def test_tool_with_string_param_callable_with_kwargs(self):
        collector = _ToolCollector()

        @collector.tool()
        def my_store(findings: str) -> str:
            return f"stored: {findings}"

        result = collector.tools["my_store"](**{"findings": "something notable"})
        assert result == "stored: something notable"

    def test_tool_called_with_empty_kwargs_dict(self):
        collector = _ToolCollector()

        @collector.tool()
        def no_args_tool() -> str:
            return "ok"

        # Passing an empty dict is equivalent to no args
        result = collector.tools["no_args_tool"](**({}))
        assert result == "ok"

    def test_tool_with_multiple_params(self):
        collector = _ToolCollector()

        @collector.tool()
        def multi_param(a: str, b: int = 0) -> str:
            return f"{a}:{b}"

        result = collector.tools["multi_param"](**{"a": "hello", "b": 42})
        assert result == "hello:42"

    def test_unexpected_kwarg_raises_type_error(self):
        """Passing unknown kwargs to the tool should raise TypeError (correct behavior)."""
        collector = _ToolCollector()

        @collector.tool()
        def simple() -> str:
            return "x"

        import pytest
        with pytest.raises(TypeError):
            collector.tools["simple"](**{"unknown_param": "value"})

    def test_store_discoveries_plugin_template_callable_with_findings(self):
        """Full round-trip: a plugin with store_discoveries(findings: str) is callable via kwargs."""
        _STORE_PLUGIN = """\
PLUGIN_MANIFEST = {
    "name": "test-store",
    "version": "1.0.0",
    "description": "Test store plugin.",
    "tool_prefix": "ts_",
}

def register_tools(mcp, db=None, sql_db=None):
    @mcp.tool()
    def ts_store_discoveries(findings: str) -> str:
        return f"stored: {findings}"
"""
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "test-store.py"
            p.write_text(_STORE_PLUGIN, encoding="utf-8")

            from laravelgraph.plugins.loader import _import_plugin_module
            module = _import_plugin_module(p, "laravelgraph_plugin_test_store")
            collector = _ToolCollector()
            module.register_tools(collector)

            result = collector.tools["ts_store_discoveries"](**{"findings": "5 routes have no auth"})
            assert result == "stored: 5 routes have no auth"
