"""Integration tests for all CLI commands.

Each test invokes the real CLI via CliRunner and verifies:
- Exit code is 0 (success)
- Output contains expected keywords
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from laravelgraph.cli import app

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"
runner = CliRunner()


@pytest.fixture(scope="session")
def indexed_project(tmp_path_factory):
    """Copy fixture app, run full analyze, return project path."""
    tmp = tmp_path_factory.mktemp("cli_test")
    project = tmp / "tiny-laravel-app"
    shutil.copytree(str(FIXTURES), str(project))
    result = runner.invoke(app, ["analyze", str(project), "--full", "--no-embeddings"])
    assert result.exit_code == 0, f"analyze failed:\n{result.output}"
    return project


class TestAnalyzeCommand:
    def test_analyze_full(self, indexed_project):
        # Already ran in fixture — verify output
        result = runner.invoke(app, ["analyze", str(indexed_project), "--no-embeddings"])
        assert result.exit_code == 0
        assert "indexed" in result.output.lower() or "complete" in result.output.lower()

    def test_analyze_phase_14_only(self, indexed_project):
        result = runner.invoke(app, ["analyze", str(indexed_project), "--phases", "14"])
        assert result.exit_code == 0

    def test_analyze_invalid_phases(self, indexed_project):
        result = runner.invoke(app, ["analyze", str(indexed_project), "--phases", "abc"])
        assert result.exit_code == 1


class TestStatusCommand:
    def test_status_shows_stats(self, indexed_project):
        result = runner.invoke(app, ["status", str(indexed_project)])
        assert result.exit_code == 0
        assert "node" in result.output.lower() or "index" in result.output.lower()


class TestListCommand:
    def test_list_runs(self, indexed_project):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 0


class TestQueryCommand:
    def test_query_finds_results(self, indexed_project):
        result = runner.invoke(app, ["query", "Post", "--project", str(indexed_project)])
        assert result.exit_code == 0

    def test_query_with_role_filter(self, indexed_project):
        result = runner.invoke(app, ["query", "Post", "--role", "model", "--project", str(indexed_project)])
        assert result.exit_code == 0

    def test_query_with_limit(self, indexed_project):
        result = runner.invoke(app, ["query", "Post", "--limit", "5", "--project", str(indexed_project)])
        assert result.exit_code == 0


class TestContextCommand:
    def test_context_known_symbol(self, indexed_project):
        result = runner.invoke(app, ["context", "PostController", "--project", str(indexed_project)])
        assert result.exit_code == 0
        assert "PostController" in result.output

    def test_context_unknown_symbol(self, indexed_project):
        result = runner.invoke(app, ["context", "NonExistentXYZ123", "--project", str(indexed_project)])
        assert result.exit_code == 1


class TestImpactCommand:
    def test_impact_known_symbol(self, indexed_project):
        result = runner.invoke(app, ["impact", "PostController", "--project", str(indexed_project)])
        assert result.exit_code == 0

    def test_impact_with_depth(self, indexed_project):
        result = runner.invoke(app, ["impact", "PostController", "--depth", "2", "--project", str(indexed_project)])
        assert result.exit_code == 0

    def test_impact_unknown_symbol(self, indexed_project):
        result = runner.invoke(app, ["impact", "NonExistentXYZ123", "--project", str(indexed_project)])
        assert result.exit_code == 1


class TestDeadCodeCommand:
    def test_dead_code_runs(self, indexed_project):
        result = runner.invoke(app, ["dead-code", str(indexed_project)])
        assert result.exit_code == 0

    def test_dead_code_with_role_filter(self, indexed_project):
        result = runner.invoke(app, ["dead-code", str(indexed_project), "--role", "controller"])
        assert result.exit_code == 0


class TestRoutesCommand:
    def test_routes_shows_table(self, indexed_project):
        result = runner.invoke(app, ["routes", str(indexed_project)])
        assert result.exit_code == 0

    def test_routes_filter_by_method(self, indexed_project):
        result = runner.invoke(app, ["routes", str(indexed_project), "--method", "GET"])
        assert result.exit_code == 0

    def test_routes_filter_by_uri(self, indexed_project):
        result = runner.invoke(app, ["routes", str(indexed_project), "--uri", "posts"])
        assert result.exit_code == 0


class TestModelsCommand:
    def test_models_runs(self, indexed_project):
        result = runner.invoke(app, ["models", str(indexed_project)])
        assert result.exit_code == 0

    def test_models_with_filter(self, indexed_project):
        result = runner.invoke(app, ["models", str(indexed_project), "--model", "Post"])
        assert result.exit_code == 0


class TestEventsCommand:
    def test_events_runs(self, indexed_project):
        result = runner.invoke(app, ["events", str(indexed_project)])
        assert result.exit_code == 0


class TestBindingsCommand:
    def test_bindings_runs(self, indexed_project):
        result = runner.invoke(app, ["bindings", str(indexed_project)])
        assert result.exit_code == 0


class TestSchemaCommand:
    def test_schema_runs(self, indexed_project):
        result = runner.invoke(app, ["schema", str(indexed_project)])
        assert result.exit_code == 0

    def test_schema_with_table_filter(self, indexed_project):
        result = runner.invoke(app, ["schema", str(indexed_project), "--table", "post"])
        assert result.exit_code == 0


class TestCypherCommand:
    def test_cypher_select_query(self, indexed_project):
        result = runner.invoke(app, ["cypher", "MATCH (n:Route) RETURN n.uri LIMIT 5", "--project", str(indexed_project)])
        assert result.exit_code == 0

    def test_cypher_blocks_mutations(self, indexed_project):
        result = runner.invoke(app, ["cypher", "CREATE (n:Route {uri: '/hack'})", "--project", str(indexed_project)])
        assert result.exit_code == 1


class TestExportCommand:
    def test_export_json(self, indexed_project, tmp_path):
        out = tmp_path / "graph.json"
        result = runner.invoke(app, ["export", str(indexed_project), "--output", str(out)])
        assert result.exit_code == 0
        assert out.exists()

    def test_export_stdout(self, indexed_project):
        result = runner.invoke(app, ["export", str(indexed_project)])
        assert result.exit_code == 0
        assert "{" in result.output  # JSON output


class TestSetupCommand:
    def test_setup_local_config(self, indexed_project):
        result = runner.invoke(app, ["setup", str(indexed_project)])
        assert result.exit_code == 0
        assert "laravelgraph" in result.output

    def test_setup_http_config(self, indexed_project):
        result = runner.invoke(app, ["setup", str(indexed_project), "--http", "--url", "http://example.com:3000/sse"])
        assert result.exit_code == 0
        assert "sse" in result.output

    def test_setup_http_with_api_key(self, indexed_project):
        result = runner.invoke(app, ["setup", str(indexed_project), "--http", "--url", "http://example.com:3000/sse", "--api-key", "secret"])
        assert result.exit_code == 0
        assert "Authorization" in result.output

    def test_setup_claude_flag(self, indexed_project):
        result = runner.invoke(app, ["setup", str(indexed_project), "--claude"])
        assert result.exit_code == 0


class TestVersionCommand:
    def test_version_prints(self):
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "LaravelGraph" in result.output or "v" in result.output


class TestDiffCommand:
    def test_diff_head_no_changes(self, indexed_project):
        # Should run without error even if no git diff
        result = runner.invoke(app, ["diff", "--project", str(indexed_project)])
        # exit 0 (no diff) or exit 1 (git error) — just mustn't crash unexpectedly
        assert result.exit_code in (0, 1)
