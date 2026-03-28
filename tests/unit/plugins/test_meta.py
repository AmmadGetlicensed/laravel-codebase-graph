"""Unit tests for PluginMetaStore from laravelgraph.plugins.meta."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest


class TestPluginMetaStoreInit:
    def test_meta_store_init_empty(self, tmp_path):
        """PluginMetaStore starts empty."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        assert store.all() == []

    def test_get_nonexistent_returns_none(self, tmp_path):
        """get() returns None for a plugin that has not been set."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        assert store.get("nonexistent-plugin") is None


class TestPluginMetaStoreSetGet:
    def test_set_and_get(self, tmp_path):
        """set() persists and get() retrieves."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        meta = PluginMeta(name="test-plugin", status="active")
        store.set(meta)
        result = store.get("test-plugin")
        assert result is not None
        assert result.name == "test-plugin"
        assert result.status == "active"

    def test_set_overwrites_existing(self, tmp_path):
        """set() replaces an existing entry for the same plugin name."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", status="active"))
        store.set(PluginMeta(name="p", status="disabled"))
        assert store.get("p").status == "disabled"

    def test_all_returns_all_entries(self, tmp_path):
        """all() returns all stored plugin metadata."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="plugin-a"))
        store.set(PluginMeta(name="plugin-b"))
        names = [m.name for m in store.all()]
        assert "plugin-a" in names
        assert "plugin-b" in names


class TestPluginMetaStorePersistence:
    def test_persistence_across_instances(self, tmp_path):
        """Data persists when a new PluginMetaStore instance is created."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store1 = PluginMetaStore(tmp_path)
        store1.set(PluginMeta(name="persistent"))
        store2 = PluginMetaStore(tmp_path)
        assert store2.get("persistent") is not None

    def test_meta_file_created_on_set(self, tmp_path):
        """A backing file is created on the first set() call."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p"))
        # Some file should exist in tmp_path as the backing store
        files = list(tmp_path.iterdir())
        assert len(files) >= 1


class TestPluginMetaStoreLogCall:
    def test_log_call_increments_call_count(self, tmp_path):
        """log_call() increments call_count."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", call_count=0))
        store.log_call("p", empty=False, error=False)
        assert store.get("p").call_count == 1

    def test_log_call_increments_empty_result_count(self, tmp_path):
        """log_call() increments empty_result_count when result is empty."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", call_count=0, empty_result_count=0))
        store.log_call("p", empty=True, error=False)
        result = store.get("p")
        assert result.call_count == 1
        assert result.empty_result_count == 1

    def test_log_call_increments_error_count(self, tmp_path):
        """log_call() increments error_count when error is True."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", call_count=0, error_count=0))
        store.log_call("p", empty=False, error=True)
        result = store.get("p")
        assert result.call_count == 1
        assert result.error_count == 1

    def test_log_call_noop_for_unknown_plugin(self, tmp_path):
        """log_call() does not raise for an unknown plugin name."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        # Should not raise
        store.log_call("nonexistent", empty=False, error=False)


class TestPluginMetaStoreEnableDisable:
    def test_enable_sets_status_active(self, tmp_path):
        """enable() changes status to active."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", status="disabled"))
        store.enable("p")
        assert store.get("p").status == "active"

    def test_disable_sets_status_disabled(self, tmp_path):
        """disable() changes status to disabled."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", status="active"))
        store.disable("p")
        assert store.get("p").status == "disabled"

    def test_is_active_true_for_active_status(self, tmp_path):
        """is_active() returns True only for active plugins."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="active-p", status="active"))
        store.set(PluginMeta(name="disabled-p", status="disabled"))
        assert store.is_active("active-p") is True
        assert store.is_active("disabled-p") is False

    def test_is_active_false_for_unknown_plugin(self, tmp_path):
        """is_active() returns False for a plugin that does not exist."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        assert store.is_active("ghost") is False


class TestPluginMetaStoreSystemPrompt:
    def test_set_system_prompt(self, tmp_path):
        """set_system_prompt() stores a prompt on the plugin meta."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", status="active"))
        store.set_system_prompt("p", "Always check payment status before processing.")
        result = store.get("p")
        assert result.system_prompt == "Always check payment status before processing."

    def test_get_all_system_prompts_only_active(self, tmp_path):
        """get_all_system_prompts() returns only prompts of active plugins."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="active-p", status="active", system_prompt="Active prompt."))
        store.set(PluginMeta(name="disabled-p", status="disabled", system_prompt="Hidden prompt."))
        prompts = store.get_all_system_prompts()
        assert "Active prompt." in prompts
        assert "Hidden prompt." not in prompts


class TestPluginMetaStoreImprovementCheck:
    def test_check_improvement_needed_false_below_threshold(self, tmp_path):
        """check_improvement_needed returns False when thresholds not crossed."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        meta = PluginMeta(name="p", call_count=5, empty_result_count=0, status="active")
        store.set(meta)
        assert store.check_improvement_needed("p") is False

    def test_check_improvement_needed_true_high_empty_rate(self, tmp_path):
        """check_improvement_needed returns True when empty rate > 25% and count > 20."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        meta = PluginMeta(name="p", call_count=25, empty_result_count=8, status="active")
        store.set(meta)
        assert store.check_improvement_needed("p") is True

    def test_improvement_cooldown_prevents_improvement(self, tmp_path):
        """check_improvement_needed returns False during cooldown period."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        meta = PluginMeta(
            name="p",
            call_count=25,
            empty_result_count=8,
            status="active",
            improvement_cooldown_until=future,
        )
        store.set(meta)
        assert store.check_improvement_needed("p") is False

    def test_check_improvement_needed_false_for_unknown(self, tmp_path):
        """check_improvement_needed returns False for an unknown plugin."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        assert store.check_improvement_needed("ghost") is False

    def test_check_improvement_needed_false_low_call_count(self, tmp_path):
        """check_improvement_needed returns False when call_count is too low even with high empty rate."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        # 90% empty rate but only 10 calls — not enough data
        meta = PluginMeta(name="p", call_count=10, empty_result_count=9, status="active")
        store.set(meta)
        assert store.check_improvement_needed("p") is False


class TestPluginMetaStoreDelete:
    def test_delete_removes_plugin(self, tmp_path):
        """delete() removes plugin from store."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="to-delete"))
        store.delete("to-delete")
        assert store.get("to-delete") is None

    def test_delete_noop_for_unknown(self, tmp_path):
        """delete() does not raise for a plugin that does not exist."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        # Should not raise
        store.delete("nonexistent")

    def test_delete_only_removes_named_plugin(self, tmp_path):
        """delete() only removes the named plugin, leaving others intact."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="a"))
        store.set(PluginMeta(name="b"))
        store.delete("a")
        assert store.get("a") is None
        assert store.get("b") is not None


class TestPluginMetaStoreContribution:
    def test_compute_contribution_basic(self, tmp_path):
        """compute_contribution returns 0-100 value."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        meta = PluginMeta(
            name="p",
            call_count=10,
            error_count=0,
            empty_result_count=0,
            plugin_node_count=5,
        )
        store.set(meta)
        score = store.compute_contribution("p", total_system_calls=100, total_plugin_nodes=50)
        assert 0.0 <= score <= 100.0

    def test_compute_contribution_zero_for_unknown_plugin(self, tmp_path):
        """compute_contribution returns 0 for an unknown plugin."""
        from laravelgraph.plugins.meta import PluginMetaStore

        store = PluginMetaStore(tmp_path)
        score = store.compute_contribution("ghost", total_system_calls=100, total_plugin_nodes=50)
        assert score == 0.0

    def test_compute_contribution_zero_when_no_system_calls(self, tmp_path):
        """compute_contribution does not raise when total_system_calls is 0 (avoid division by zero)."""
        from laravelgraph.plugins.meta import PluginMetaStore, PluginMeta

        store = PluginMetaStore(tmp_path)
        store.set(PluginMeta(name="p", call_count=5, plugin_node_count=10))
        score = store.compute_contribution("p", total_system_calls=0, total_plugin_nodes=10)
        assert 0.0 <= score <= 100.0
