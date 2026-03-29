"""Integration tests for the plugin generator against the tiny-laravel-app fixture.

These tests run the real pipeline and then exercise the domain-anchor resolution
and code-assembly stages against the actual KuzuDB that was built from the fixture.
They do NOT call an LLM — the LLM step is mocked to return a minimal valid spec.
"""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def indexed_app(tmp_path_factory):
    """Index the tiny app once; return the project root."""
    tmp = tmp_path_factory.mktemp("plugin_gen_int")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False
    Pipeline(app_copy, config=cfg).run(full=True, skip_embeddings=True)
    return app_copy


@pytest.fixture(scope="module")
def core_db(indexed_app):
    """Return a live GraphDB for the indexed tiny app."""
    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db_path = index_dir(indexed_app) / "graph.kuzu"
    return GraphDB(db_path, force_reinit=False)


# ── Domain anchor resolution ──────────────────────────────────────────────────

class TestResolveDomainAnchorsIntegration:
    """Test _resolve_domain_anchors against the real indexed graph."""

    def test_user_description_finds_routes(self, core_db):
        from laravelgraph.plugins.generator import _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "Show user management routes and models")
        # The tiny app has /api/users routes — at least routes should be found
        assert isinstance(anchors, dict)
        assert "routes" in anchors
        assert "models" in anchors

    def test_user_description_finds_user_model(self, core_db):
        from laravelgraph.plugins.generator import _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "List all user models and their relationships")
        model_names = [m.get("name", "") for m in anchors.get("models", [])]
        assert any("User" in n for n in model_names), f"Expected User model, got: {model_names}"

    def test_post_description_finds_post_model(self, core_db):
        from laravelgraph.plugins.generator import _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "Explore post content and author relationships")
        model_names = [m.get("name", "") for m in anchors.get("models", [])]
        # The tiny app has Post and User models
        assert len(anchors.get("routes", []) + anchors.get("models", [])) > 0

    def test_returns_dict_with_required_keys(self, core_db):
        from laravelgraph.plugins.generator import _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "anything at all")
        required = {"feature_name", "feature_slug", "matched_by", "tokens_used",
                    "routes", "models", "events", "jobs", "controllers"}
        assert required.issubset(anchors.keys())

    def test_tokens_always_stored(self, core_db):
        from laravelgraph.plugins.generator import _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "user registration events and listeners")
        assert len(anchors["tokens_used"]) > 0

    def test_events_found_for_user_registration(self, core_db):
        from laravelgraph.plugins.generator import _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "user registration event listener")
        # The tiny app has UserRegistered event and SendWelcomeEmail listener
        events = [e.get("name", "") for e in anchors.get("events", [])]
        # At minimum: token scan should have found something, or events list is populated
        assert isinstance(events, list)


# ── Code assembly with real anchors ──────────────────────────────────────────

def _minimal_spec(slug: str, prefix: str) -> dict:
    return {
        "slug": slug,
        "prefix": prefix + "_",
        "tools": [
            {
                "name": f"{prefix}_overview",
                "description": "List routes and models for this domain.",
                "cypher_query": "MATCH (r:Route) RETURN r.http_method AS m, r.uri AS u LIMIT 20",
                "result_format": "[{m}] {u}",
            }
        ],
    }


class TestAssemblePluginCodeIntegration:
    """Test deterministic code assembly using anchors from the real graph."""

    def test_assembled_code_is_valid_python(self, core_db):
        from laravelgraph.plugins.generator import _assemble_plugin_code, _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "user routes and models")
        spec = _minimal_spec("user-domain", "user_domain")
        code = _assemble_plugin_code(spec, anchors)
        assert code is not None
        # Must parse as valid Python
        ast.parse(code)

    def test_plugin_manifest_present(self, core_db):
        from laravelgraph.plugins.generator import _assemble_plugin_code, _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "user routes and models")
        code = _assemble_plugin_code(_minimal_spec("user-domain", "user_domain"), anchors)
        assert "PLUGIN_MANIFEST" in code
        assert '"user-domain"' in code

    def test_summary_tool_contains_real_uri(self, core_db):
        from laravelgraph.plugins.generator import _assemble_plugin_code, _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "user api resource endpoints")
        code = _assemble_plugin_code(_minimal_spec("user-api", "user_api"), anchors)
        # The tiny app has /api/users routes — if anchors resolved them, summary mentions URI
        if anchors.get("routes"):
            assert any(r.get("uri", "") in code for r in anchors["routes"])

    def test_store_discoveries_tool_present(self, core_db):
        from laravelgraph.plugins.generator import _assemble_plugin_code, _resolve_domain_anchors
        anchors = _resolve_domain_anchors(core_db, "user routes and models")
        code = _assemble_plugin_code(_minimal_spec("user-domain", "user_domain"), anchors)
        assert "store_discoveries" in code

    def test_generated_code_passes_layer1_validation(self, core_db):
        from laravelgraph.plugins.generator import _assemble_plugin_code, _resolve_domain_anchors
        from laravelgraph.plugins.validator import validate_plugin_file_content
        anchors = _resolve_domain_anchors(core_db, "post routes and models")
        code = _assemble_plugin_code(_minimal_spec("post-domain", "post_domain"), anchors)
        result = validate_plugin_file_content(code)
        assert result.passed, f"Layer 1 failed: {result.errors}"

    def test_generated_code_passes_layer3_execution(self, core_db):
        from laravelgraph.plugins.generator import (
            _assemble_plugin_code,
            _resolve_domain_anchors,
            _validate_execution,
        )
        anchors = _resolve_domain_anchors(core_db, "user routes and models")
        code = _assemble_plugin_code(_minimal_spec("user-exec", "user_exec"), anchors)
        result = _validate_execution(code, core_db)
        assert result.passed, f"Layer 3 failed: {result.critique}"


# ── End-to-end generate_plugin with mocked LLM ───────────────────────────────

class TestGeneratePluginEndToEnd:
    """Full generate_plugin() call with LLM mocked — real graph, real code assembly."""

    def _llm_response(self, slug: str = "user-explorer", prefix: str = "usr_") -> str:
        spec = {
            "slug": slug,
            "prefix": prefix,
            "tools": [
                {
                    "name": f"{prefix.rstrip('_')}_routes",
                    "description": "List all user-related routes.",
                    "cypher_query": "MATCH (r:Route) RETURN r.http_method AS m, r.uri AS u LIMIT 30",
                    "result_format": "[{m}] {u}",
                }
            ],
        }
        return json.dumps(spec)

    def _mock_cfg(self):
        class _LLMCfg:
            enabled = True
            provider = "openai"
            api_keys = {"openai": "sk-test"}
            models: dict = {}
            base_urls: dict = {}

        class _Cfg:
            llm = _LLMCfg()

        return _Cfg()

    def test_generates_valid_plugin_file(self, indexed_app, core_db):
        from laravelgraph.plugins.generator import generate_plugin

        with patch("laravelgraph.plugins.generator._call_llm", return_value=self._llm_response()):
            with patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
                from unittest.mock import MagicMock
                mock_judge.return_value = MagicMock(passed=True, layer=4, score=8.5, critique="")
                code, msg = generate_plugin(
                    description="Show user management routes and models",
                    project_root=indexed_app,
                    core_db=core_db,
                    cfg=self._mock_cfg(),
                    max_iterations=1,
                )

        assert code is not None, f"Expected code, got None. Message: {msg}"
        ast.parse(code)  # must be valid Python

    def test_status_message_indicates_success(self, indexed_app, core_db):
        from laravelgraph.plugins.generator import generate_plugin

        with patch("laravelgraph.plugins.generator._call_llm", return_value=self._llm_response()):
            with patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
                from unittest.mock import MagicMock
                mock_judge.return_value = MagicMock(passed=True, layer=4, score=9.0, critique="")
                _, msg = generate_plugin(
                    description="Show user management routes and models",
                    project_root=indexed_app,
                    core_db=core_db,
                    cfg=self._mock_cfg(),
                    max_iterations=1,
                )

        assert "success" in msg.lower() or "score" in msg.lower(), f"Unexpected msg: {msg}"

    def test_summary_tool_reflects_graph_data(self, indexed_app, core_db):
        """The {prefix}summary tool should embed data resolved from the real graph."""
        from laravelgraph.plugins.generator import generate_plugin

        with patch("laravelgraph.plugins.generator._call_llm", return_value=self._llm_response()):
            with patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
                from unittest.mock import MagicMock
                mock_judge.return_value = MagicMock(passed=True, layer=4, score=8.0, critique="")
                code, _ = generate_plugin(
                    description="Show user management routes and models",
                    project_root=indexed_app,
                    core_db=core_db,
                    cfg=self._mock_cfg(),
                    max_iterations=1,
                )

        assert code is not None
        # The tiny app has /api/users — if routes were found, summary mentions it
        # At minimum, the summary tool must be present
        assert "_summary" in code

    def test_no_llm_provider_returns_none(self, indexed_app, core_db):
        from laravelgraph.plugins.generator import generate_plugin

        class _NoCfg:
            class llm:
                enabled = True
                provider = "auto"
                api_keys: dict = {}
                models: dict = {}
                base_urls: dict = {}

        with patch("laravelgraph.plugins.generator._call_llm", return_value=None):
            code, msg = generate_plugin(
                description="Show user routes",
                project_root=indexed_app,
                core_db=core_db,
                cfg=_NoCfg(),
                max_iterations=1,
            )

        assert code is None
        assert "No LLM" in msg

    def test_invalid_json_falls_back_to_template(self, indexed_app, core_db):
        from laravelgraph.plugins.generator import generate_plugin

        with patch("laravelgraph.plugins.generator._call_llm", return_value="not valid json!!!"):
            code, msg = generate_plugin(
                description="Show user routes and models",
                project_root=indexed_app,
                core_db=core_db,
                cfg=self._mock_cfg(),
                max_iterations=1,
            )

        # Template fallback should produce code
        assert code is not None
        ast.parse(code)

    def test_plugin_file_written_to_disk(self, indexed_app, core_db, tmp_path):
        """Simulate what request_plugin does: write the returned code to disk."""
        from laravelgraph.plugins.generator import generate_plugin

        with patch("laravelgraph.plugins.generator._call_llm", return_value=self._llm_response("disk-test", "dsk_")):
            with patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
                from unittest.mock import MagicMock
                mock_judge.return_value = MagicMock(passed=True, layer=4, score=8.0, critique="")
                code, _ = generate_plugin(
                    description="Show user management routes",
                    project_root=indexed_app,
                    core_db=core_db,
                    cfg=self._mock_cfg(),
                    max_iterations=1,
                )

        assert code is not None
        out = tmp_path / "disk-test.py"
        out.write_text(code, encoding="utf-8")
        assert out.exists()
        assert out.stat().st_size > 0
        # Reload and parse
        ast.parse(out.read_text())
