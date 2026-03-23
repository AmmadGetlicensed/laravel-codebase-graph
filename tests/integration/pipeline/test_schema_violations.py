"""Integration regression tests: schema violations in pipeline output.

These tests run the full pipeline on the tiny-laravel-app fixture and verify
that no relationship edge in the resulting graph uses an invalid source/target
node label — specifically the bugs we fixed:

  1. Phase 18 (Blade): RENDERS_TEMPLATE edges must come from Method or Class_,
     never from File nodes.
  2. Phase 20 (Config): USES_CONFIG / USES_ENV edges are allowed from File
     nodes (this is correct behaviour — kept as a positive regression check).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    """Run the full pipeline once and reuse the context for all tests here."""
    tmp = tmp_path_factory.mktemp("schema_violation_test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False
    pipeline = Pipeline(app_copy, config=cfg)
    return pipeline.run(full=True, skip_embeddings=True)


# ── RENDERS_TEMPLATE ──────────────────────────────────────────────────────────

class TestRendersTemplatePipelineOutput:
    """Phase 18 must link view() calls to Method or Class_ nodes, not File nodes."""

    def test_renders_template_edges_exist(self, ctx):
        """PostController calls view('posts.index', ...) — at least one edge must be created."""
        rows = ctx.db.execute(
            "MATCH (m:Method)-[:RENDERS_TEMPLATE]->(t:BladeTemplate) "
            "RETURN m.node_id AS mnid, t.node_id AS tnid"
        )
        assert len(rows) >= 1, (
            "Expected at least one Method→BladeTemplate RENDERS_TEMPLATE edge. "
            "PostController.index() calls view('posts.index') in the fixture."
        )

    def test_renders_template_links_post_controller_index(self, ctx):
        """Specifically verify PostController::index → posts.index template."""
        rows = ctx.db.execute(
            "MATCH (m:Method)-[:RENDERS_TEMPLATE]->(t:BladeTemplate) "
            "WHERE m.fqn CONTAINS 'PostController' "
            "RETURN m.fqn AS fqn, t.name AS view"
        )
        assert len(rows) >= 1, (
            "Expected RENDERS_TEMPLATE edges from PostController methods. "
            "PostController has view('posts.index') and view('posts.show') calls."
        )

    def test_no_file_nodes_are_renders_template_sources(self, ctx):
        """Regression: phase 18 was passing File node IDs as source for RENDERS_TEMPLATE.

        KuzuDB rejects File→BladeTemplate at runtime with a Binder exception.
        This test ensures no such attempt is made (i.e. the phase correctly
        resolves to Method or Class_ before calling upsert_rel).
        """
        # Check ctx.errors for RENDERS_TEMPLATE warnings with file: prefix
        renders_file_errors = [
            e for e in ctx.errors
            if "RENDERS_TEMPLATE" in e and "file:" in e
        ]
        assert renders_file_errors == [], (
            f"Found {len(renders_file_errors)} RENDERS_TEMPLATE errors involving "
            f"File nodes:\n" + "\n".join(renders_file_errors[:5])
        )

    def test_renders_template_no_warnings_in_logs_for_file_source(self, ctx):
        """No 'Relationship creation failed' warnings for RENDERS_TEMPLATE with File sources."""
        bad_errors = [
            e for e in ctx.errors
            if "RENDERS_TEMPLATE" in e
            and ("violates schema" in e or "Expected labels" in e)
        ]
        assert bad_errors == [], (
            "RENDERS_TEMPLATE schema violation errors found in pipeline output:\n"
            + "\n".join(bad_errors[:10])
        )


# ── USES_CONFIG / USES_ENV ────────────────────────────────────────────────────

class TestConfigEnvPipelineOutput:
    """Phase 20 may link config keys from File nodes — this is valid behaviour."""

    def test_no_uses_config_schema_errors(self, ctx):
        """USES_CONFIG must not produce Binder/schema violation errors."""
        bad = [
            e for e in ctx.errors
            if "USES_CONFIG" in e
            and ("violates schema" in e or "Expected labels" in e or "Binder" in e)
        ]
        assert bad == [], (
            "USES_CONFIG schema violation errors:\n" + "\n".join(bad[:10])
        )

    def test_no_uses_env_schema_errors(self, ctx):
        """USES_ENV must not produce Binder/schema violation errors."""
        bad = [
            e for e in ctx.errors
            if "USES_ENV" in e
            and ("violates schema" in e or "Expected labels" in e or "Binder" in e)
        ]
        assert bad == [], (
            "USES_ENV schema violation errors:\n" + "\n".join(bad[:10])
        )


# ── General: no Binder exceptions in ctx.errors ───────────────────────────────

class TestNoPipelineSchemaErrors:
    """The pipeline must complete without any KuzuDB schema violation errors."""

    def test_no_binder_exceptions_in_errors(self, ctx):
        """No 'Binder exception: ... violates schema' errors anywhere in pipeline output."""
        binder_errors = [e for e in ctx.errors if "violates schema" in e]
        assert binder_errors == [], (
            f"Found {len(binder_errors)} schema violation error(s) in pipeline output:\n"
            + "\n".join(binder_errors[:10])
        )

    def test_no_expected_labels_errors(self, ctx):
        """No 'Expected labels are ...' errors anywhere — these are KuzuDB schema rejections."""
        label_errors = [e for e in ctx.errors if "Expected labels" in e]
        assert label_errors == [], (
            f"Found {len(label_errors)} 'Expected labels' error(s):\n"
            + "\n".join(label_errors[:10])
        )
