"""Unit tests for PluginGraphDB and DualDB from laravelgraph.plugins.plugin_graph."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestPluginGraphDBInit:
    def test_plugin_graph_init(self, tmp_path):
        """PluginGraphDB initializes and creates schema tables."""
        from laravelgraph.plugins.plugin_graph import PluginGraphDB

        db = PluginGraphDB(tmp_path / "plugin_graph.kuzu")
        # Schema tables exist — execute a query against them
        rows = db.execute("MATCH (n:PluginNode) RETURN n.node_id LIMIT 1")
        assert isinstance(rows, list)
        db.close()

    def test_init_plugin_graph_creates_db(self, tmp_path):
        """init_plugin_graph creates a new db at index_dir/plugin_graph.kuzu."""
        from laravelgraph.plugins.plugin_graph import init_plugin_graph

        db = init_plugin_graph(tmp_path)
        assert (tmp_path / "plugin_graph.kuzu").exists()
        db.close()


class TestPluginGraphDBUpsert:
    def test_upsert_plugin_node_creates_node(self, tmp_path):
        """upsert_plugin_node creates a node and can be retrieved."""
        from laravelgraph.plugins.plugin_graph import PluginGraphDB

        db = PluginGraphDB(tmp_path / "plugin_graph.kuzu")
        db.upsert_plugin_node(
            plugin_source="test-plugin",
            node_id="node:test-plugin:001",
            label="TestLabel",
            data={"key": "value"},
        )
        rows = db.execute(
            "MATCH (n:PluginNode) WHERE n.node_id = 'node:test-plugin:001' RETURN n.node_id"
        )
        assert len(rows) >= 1
        db.close()

    def test_upsert_plugin_node_updates_existing(self, tmp_path):
        """upsert_plugin_node updates a node on second call (upsert semantics)."""
        from laravelgraph.plugins.plugin_graph import PluginGraphDB
        import time

        db = PluginGraphDB(tmp_path / "plugin_graph.kuzu")

        db.upsert_plugin_node(
            plugin_source="test-plugin",
            node_id="node:test-plugin:002",
            label="TestLabel",
            data={"value": "first"},
        )
        time.sleep(0.01)
        db.upsert_plugin_node(
            plugin_source="test-plugin",
            node_id="node:test-plugin:002",
            label="TestLabel",
            data={"value": "second"},
        )

        rows = db.execute(
            "MATCH (n:PluginNode) WHERE n.node_id = 'node:test-plugin:002' RETURN n.node_id"
        )
        assert len(rows) == 1  # No duplicates — upsert replaced
        db.close()


class TestPluginGraphDBDelete:
    def test_delete_plugin_data_removes_only_named_plugin(self, tmp_path):
        """delete_plugin_data removes only the named plugin's nodes."""
        from laravelgraph.plugins.plugin_graph import PluginGraphDB

        db = PluginGraphDB(tmp_path / "plugin_graph.kuzu")

        db.upsert_plugin_node(
            plugin_source="plugin-a",
            node_id="node:plugin-a:001",
            label="TestLabel",
            data={},
        )
        db.upsert_plugin_node(
            plugin_source="plugin-b",
            node_id="node:plugin-b:001",
            label="TestLabel",
            data={},
        )

        db.delete_plugin_data("plugin-a")

        rows_a = db.execute(
            "MATCH (n:PluginNode) WHERE n.plugin_source = 'plugin-a' RETURN n.node_id"
        )
        rows_b = db.execute(
            "MATCH (n:PluginNode) WHERE n.plugin_source = 'plugin-b' RETURN n.node_id"
        )

        assert len(rows_a) == 0
        assert len(rows_b) == 1
        db.close()


class TestPluginGraphDBCount:
    def test_get_plugin_node_count_returns_correct_count(self, tmp_path):
        """get_plugin_node_count returns the number of nodes for a plugin."""
        from laravelgraph.plugins.plugin_graph import PluginGraphDB

        db = PluginGraphDB(tmp_path / "plugin_graph.kuzu")

        assert db.get_plugin_node_count("counting-plugin") == 0

        for i in range(3):
            db.upsert_plugin_node(
                plugin_source="counting-plugin",
                node_id=f"node:counting-plugin:{i:03d}",
                label="TestLabel",
                data={},
            )

        assert db.get_plugin_node_count("counting-plugin") == 3
        db.close()


class TestDualDB:
    def test_dual_db_execute_proxies_to_core(self, tmp_path):
        """DualDB.execute() proxies to core graph (backwards compat)."""
        from laravelgraph.plugins.plugin_graph import DualDB, PluginGraphDB

        mock_core_db = MagicMock()
        mock_core_db.execute.return_value = [{"result": 1}]

        plugin_db = PluginGraphDB(tmp_path / "p.kuzu")
        dual = DualDB(lambda: mock_core_db, plugin_db)

        result = dual.execute("MATCH (n:EloquentModel) RETURN n LIMIT 1")

        mock_core_db.execute.assert_called_once()
        assert result == [{"result": 1}]
        plugin_db.close()

    def test_dual_db_callable_returns_self(self, tmp_path):
        """DualDB() called as function returns itself (backwards compat)."""
        from laravelgraph.plugins.plugin_graph import DualDB, PluginGraphDB

        plugin_db = PluginGraphDB(tmp_path / "p.kuzu")
        called_results = []

        def mock_core():
            called_results.append(1)
            return None

        dual = DualDB(mock_core, plugin_db)
        result = dual()  # calling the DualDB as a function
        assert result is dual
        plugin_db.close()

    def test_dual_db_plugin_returns_plugin_db(self, tmp_path):
        """DualDB.plugin() returns the PluginGraphDB."""
        from laravelgraph.plugins.plugin_graph import DualDB, PluginGraphDB

        plugin_db = PluginGraphDB(tmp_path / "p.kuzu")
        dual = DualDB(lambda: None, plugin_db)

        assert dual.plugin() is plugin_db
        plugin_db.close()

    def test_dual_db_core_returns_core_db(self, tmp_path):
        """DualDB.core() returns the core GraphDB via factory."""
        from laravelgraph.plugins.plugin_graph import DualDB, PluginGraphDB

        mock_core_db = MagicMock()
        plugin_db = PluginGraphDB(tmp_path / "p.kuzu")
        dual = DualDB(lambda: mock_core_db, plugin_db)

        assert dual.core() is mock_core_db
        plugin_db.close()
