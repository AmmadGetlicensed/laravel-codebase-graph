"""Unit tests for validation layers in laravelgraph.plugins.generator."""

from __future__ import annotations

import pytest


class TestValidateSchemaLayer:
    def test_validate_schema_valid_cypher(self):
        """_validate_schema passes when Cypher uses valid node labels."""
        from laravelgraph.plugins.generator import _validate_schema

        code = '''
PLUGIN_MANIFEST = {"name": "test", "version": "1.0.0", "description": "test", "tool_prefix": "test_"}
def register_tools(mcp, db=None):
    @mcp.tool()
    def test_query() -> str:
        db().execute("MATCH (m:EloquentModel) RETURN m.name LIMIT 10")
        return "result"
'''
        result = _validate_schema(code)
        assert result.passed is True

    def test_validate_schema_invalid_label(self):
        """_validate_schema fails when Cypher uses unknown node label."""
        from laravelgraph.plugins.generator import _validate_schema

        code = 'db().execute("MATCH (x:NonExistentLabel) RETURN x")'
        result = _validate_schema(code)
        assert result.passed is False
        assert "NonExistentLabel" in result.critique

    def test_validate_schema_multiple_valid_labels(self):
        """_validate_schema passes for multiple well-known node labels."""
        from laravelgraph.plugins.generator import _validate_schema

        code = '''
db().execute("MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) RETURN m.name, t.name")
db().execute("MATCH (r:Route)-[:ROUTES_TO]->(c:Class_) RETURN r.uri")
'''
        result = _validate_schema(code)
        assert result.passed is True

    def test_validate_schema_no_cypher_passes(self):
        """_validate_schema passes when there are no Cypher queries."""
        from laravelgraph.plugins.generator import _validate_schema

        code = '''
PLUGIN_MANIFEST = {"name": "t", "version": "1.0.0", "description": "t", "tool_prefix": "t_"}
def register_tools(mcp, db=None):
    @mcp.tool()
    def t_info() -> str:
        return "static response"
'''
        result = _validate_schema(code)
        assert result.passed is True


class TestValidateExecutionLayer:
    def test_validate_execution_no_register_tools(self):
        """_validate_execution fails if plugin has no register_tools."""
        from laravelgraph.plugins.generator import _validate_execution

        code = "PLUGIN_MANIFEST = {}\ndef run(ctx): pass"
        result = _validate_execution(code, None)
        assert result.passed is False

    def test_validate_execution_valid_plugin(self):
        """_validate_execution passes for a well-formed plugin."""
        from laravelgraph.plugins.generator import _validate_execution

        code = '''
PLUGIN_MANIFEST = {"name": "t", "version": "1.0.0", "description": "t", "tool_prefix": "t_"}
def register_tools(mcp, db=None):
    @mcp.tool()
    def t_query() -> str:
        return "result data here"
'''
        result = _validate_execution(code, None)
        assert result.passed is True

    def test_validate_execution_syntax_error(self):
        """_validate_execution fails on syntax error."""
        from laravelgraph.plugins.generator import _validate_execution

        code = "def broken(: pass"
        result = _validate_execution(code, None)
        assert result.passed is False

    def test_validate_execution_returns_critique_on_failure(self):
        """_validate_execution result includes a critique string on failure."""
        from laravelgraph.plugins.generator import _validate_execution

        code = "not valid python {{{"
        result = _validate_execution(code, None)
        assert result.passed is False
        assert isinstance(result.critique, str)
        assert len(result.critique) > 0

    def test_validate_execution_empty_code(self):
        """_validate_execution fails for an empty string."""
        from laravelgraph.plugins.generator import _validate_execution

        result = _validate_execution("", None)
        assert result.passed is False


class TestValidationResult:
    def test_validation_result_passed_has_empty_critique(self):
        """A passing ValidationResult has an empty or None critique."""
        from laravelgraph.plugins.generator import _validate_schema

        code = '''
PLUGIN_MANIFEST = {"name": "t", "version": "1.0.0", "description": "t", "tool_prefix": "t_"}
def register_tools(mcp, db=None):
    @mcp.tool()
    def t_query() -> str:
        db().execute("MATCH (m:EloquentModel) RETURN m.name LIMIT 5")
        return "ok"
'''
        result = _validate_schema(code)
        assert result.passed is True
        # critique should be empty or None when passed
        assert not result.critique

    def test_validation_result_has_passed_attribute(self):
        """ValidationResult objects expose a .passed boolean attribute."""
        from laravelgraph.plugins.generator import _validate_execution

        code = "def register_tools(mcp, db=None): pass"
        result = _validate_execution(code, None)
        assert hasattr(result, "passed")
        assert isinstance(result.passed, bool)

    def test_validation_result_has_critique_attribute(self):
        """ValidationResult objects expose a .critique string attribute."""
        from laravelgraph.plugins.generator import _validate_execution

        code = "def register_tools(mcp, db=None): pass"
        result = _validate_execution(code, None)
        assert hasattr(result, "critique")
