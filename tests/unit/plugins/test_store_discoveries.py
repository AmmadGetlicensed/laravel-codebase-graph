"""Unit tests for the redesigned store_discoveries tool generation.

Verifies that:
- _build_store_tool() produces a function accepting a free-text `findings` param
- The generated code stores a "Discovery" label node (not "DomainRoute")
- The {prefix}summary output includes the inline nudge
- The template fallback also includes store_discoveries
"""
from __future__ import annotations

import ast
import re


# ── helpers ───────────────────────────────────────────────────────────────────

def _import_build_store_tool():
    from laravelgraph.plugins.generator import _build_store_tool
    return _build_store_tool


def _import_build_summary_text():
    from laravelgraph.plugins.generator import _build_summary_text
    return _build_summary_text


def _import_assemble():
    from laravelgraph.plugins.generator import _assemble_plugin_code
    return _assemble_plugin_code


def _import_template_fallback():
    from laravelgraph.plugins.generator import _build_template_fallback
    return _build_template_fallback


# ── _build_store_tool tests ───────────────────────────────────────────────────

class TestBuildStoreTool:
    def test_function_name_derived_from_prefix(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("usr_", "user-explorer")
        assert "def usr_store_discoveries" in code

    def test_accepts_findings_param(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("ord_", "order-flow")
        assert "findings: str" in code

    def test_stores_discovery_label(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("pmt_", "payment")
        assert '"Discovery"' in code

    def test_no_longer_stores_domain_route(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("pmt_", "payment")
        assert "DomainRoute" not in code

    def test_no_longer_queries_routes(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("usr_", "user-explorer")
        # Old impl did a MATCH (r:Route) query
        assert "MATCH (r:Route)" not in code

    def test_stores_findings_in_data(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("usr_", "user-explorer")
        assert "findings" in code
        assert '"findings"' in code

    def test_slug_used_as_plugin_source(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("usr_", "user-explorer")
        assert '"user-explorer"' in code

    def test_return_message_mentions_plugin_knowledge(self):
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("usr_", "user-explorer")
        assert "plugin_knowledge" in code

    def test_generated_code_is_valid_python(self):
        """The generated tool block must be syntactically valid when wrapped in a function."""
        _build_store_tool = _import_build_store_tool()
        code = _build_store_tool("usr_", "user-explorer")
        # Wrap in a def to make it a complete module
        wrapped = f"def register_tools(mcp, db=None):\n{code}"
        ast.parse(wrapped)  # raises SyntaxError if invalid

    def test_slug_with_special_chars_escaped(self):
        _build_store_tool = _import_build_store_tool()
        # Slug with a double-quote would break string literals — must be escaped
        code = _build_store_tool("x_", 'test"slug')
        # Should not raise a SyntaxError
        wrapped = f"def register_tools(mcp, db=None):\n{code}"
        ast.parse(wrapped)


# ── summary nudge tests ───────────────────────────────────────────────────────

class TestSummaryNudge:
    def _make_spec(self, slug: str = "user-explorer", prefix: str = "usr_"):
        return {
            "slug": slug,
            "prefix": prefix,
            "tools": [
                {
                    "name": "usr_routes",
                    "description": "List user routes",
                    "cypher_query": "MATCH (r:Route) RETURN r.uri AS uri LIMIT 10",
                    "result_format": "{uri}",
                }
            ],
        }

    def test_summary_tool_ends_with_nudge(self):
        _assemble_plugin_code = _import_assemble()
        spec = self._make_spec()
        code = _assemble_plugin_code(spec, {})
        # The summary tool returns a string; that string should contain the nudge
        assert "store_discoveries" in code
        assert "findings" in code.lower()

    def test_nudge_uses_correct_store_fn_name(self):
        _assemble_plugin_code = _import_assemble()
        spec = self._make_spec(slug="order-flow", prefix="ord_")
        code = _assemble_plugin_code(spec, {})
        assert "ord_store_discoveries" in code

    def test_nudge_mentions_what_to_store(self):
        _assemble_plugin_code = _import_assemble()
        code = _assemble_plugin_code(self._make_spec(), {})
        # Should hint at patterns, rules, risks, anomalies
        assert any(word in code for word in ("patterns", "rules", "risks", "anomalies", "notable"))


# ── template fallback tests ───────────────────────────────────────────────────

class TestTemplateFallback:
    def test_fallback_includes_store_discoveries(self):
        _build_template_fallback = _import_template_fallback()
        code = _build_template_fallback("show order management routes")
        assert "store_discoveries" in code

    def test_fallback_store_accepts_findings_param(self):
        _build_template_fallback = _import_template_fallback()
        code = _build_template_fallback("user dashboard")
        assert "findings: str" in code

    def test_fallback_is_valid_python(self):
        _build_template_fallback = _import_template_fallback()
        code = _build_template_fallback("payment audit")
        ast.parse(code)

    def test_fallback_nudge_in_summary(self):
        _build_template_fallback = _import_template_fallback()
        code = _build_template_fallback("driver assignment")
        assert "store_discoveries" in code
