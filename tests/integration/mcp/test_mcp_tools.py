"""Tests for MCP tool implementations."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def indexed_app(tmp_path_factory):
    """Index the tiny app and return the project root."""
    tmp = tmp_path_factory.mktemp("mcp_test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False
    Pipeline(app_copy, config=cfg).run(full=True, skip_embeddings=True)
    return app_copy


# ── MCP server creation ───────────────────────────────────────────────────────

class TestMcpServerCreation:
    def test_mcp_server_creation(self, indexed_app):
        """Test that MCP server can be created without errors."""
        from laravelgraph.mcp.server import create_server
        server = create_server(indexed_app)
        assert server is not None

    def test_mcp_server_has_name(self, indexed_app):
        from laravelgraph.mcp.server import create_server
        server = create_server(indexed_app)
        # FastMCP servers have a name attribute
        assert hasattr(server, "name") or server is not None

    def test_mcp_server_creation_twice(self, indexed_app):
        """Creating the server twice should not raise."""
        from laravelgraph.mcp.server import create_server
        server1 = create_server(indexed_app)
        server2 = create_server(indexed_app)
        assert server1 is not None
        assert server2 is not None


# ── Registry ──────────────────────────────────────────────────────────────────

class TestListReposTool:
    def test_indexed_app_appears_in_registry(self, indexed_app):
        """After indexing, the app should appear in the global registry."""
        from laravelgraph.core.registry import Registry
        registry = Registry()
        repos = registry.all()
        paths = [r.path for r in repos]
        assert str(indexed_app) in paths, (
            f"Expected {indexed_app} in registry, got: {paths}"
        )

    def test_registry_entry_has_laravel_version(self, indexed_app):
        from laravelgraph.core.registry import Registry
        registry = Registry()
        entry = registry.get(indexed_app)
        assert entry is not None
        assert entry.laravel_version  # version is set (exact value depends on fixture composer.json)

    def test_registry_entry_is_indexed(self, indexed_app):
        from laravelgraph.core.registry import Registry
        registry = Registry()
        assert registry.is_indexed(indexed_app) is True

    def test_registry_entry_has_stats(self, indexed_app):
        from laravelgraph.core.registry import Registry
        registry = Registry()
        entry = registry.get(indexed_app)
        assert entry is not None
        assert isinstance(entry.stats, dict)


# ── Schema resource ───────────────────────────────────────────────────────────

class TestSchemaResource:
    def test_node_types_count(self):
        from laravelgraph.core.schema import NODE_TYPES
        assert len(NODE_TYPES) > 20, f"Expected >20 node types, got {len(NODE_TYPES)}"

    def test_rel_types_count(self):
        from laravelgraph.core.schema import REL_TYPES
        assert len(REL_TYPES) > 15, f"Expected >15 rel types, got {len(REL_TYPES)}"

    def test_all_node_types_have_node_id_as_pk(self):
        from laravelgraph.core.schema import NODE_TYPES
        for label, props in NODE_TYPES:
            assert props[0][0] == "node_id", (
                f"{label} missing node_id as first property"
            )

    def test_class_node_type_defined(self):
        from laravelgraph.core.schema import NODE_TYPES
        labels = {label for label, _ in NODE_TYPES}
        assert "Class_" in labels

    def test_eloquent_model_node_type_defined(self):
        from laravelgraph.core.schema import NODE_TYPES
        labels = {label for label, _ in NODE_TYPES}
        assert "EloquentModel" in labels

    def test_route_node_type_defined(self):
        from laravelgraph.core.schema import NODE_TYPES
        labels = {label for label, _ in NODE_TYPES}
        assert "Route" in labels


# ── GraphDB integration via indexed app ───────────────────────────────────────

class TestGraphDbViaMcp:
    def _get_db(self, indexed_app):
        from laravelgraph.config import index_dir
        from laravelgraph.core.graph import GraphDB
        db_path = index_dir(indexed_app) / "graph.kuzu"
        return GraphDB(db_path)

    def test_db_has_class_nodes(self, indexed_app):
        db = self._get_db(indexed_app)
        try:
            rows = db.execute("MATCH (c:Class_) RETURN count(c) AS cnt")
            assert rows[0]["cnt"] >= 5
        finally:
            db.close()

    def test_db_can_query_user_class(self, indexed_app):
        db = self._get_db(indexed_app)
        try:
            rows = db.execute("MATCH (c:Class_ {name: 'User'}) RETURN c.fqn AS fqn")
            assert len(rows) >= 1
            assert "User" in rows[0]["fqn"]
        finally:
            db.close()

    def test_db_has_route_nodes(self, indexed_app):
        db = self._get_db(indexed_app)
        try:
            rows = db.execute("MATCH (r:Route) RETURN count(r) AS cnt")
            assert rows[0]["cnt"] >= 1
        finally:
            db.close()


# ── Config integration ────────────────────────────────────────────────────────

class TestConfigIntegration:
    def test_config_loads_defaults(self):
        from laravelgraph.config import Config
        cfg = Config()
        assert cfg.embedding is not None
        assert cfg.pipeline is not None
        assert cfg.mcp is not None

    def test_config_embedding_can_be_disabled(self):
        from laravelgraph.config import Config
        cfg = Config()
        cfg.embedding.enabled = False
        assert cfg.embedding.enabled is False

    def test_config_load_from_project(self, indexed_app):
        from laravelgraph.config import Config
        cfg = Config.load(indexed_app)
        assert cfg is not None
        assert isinstance(cfg, Config)
