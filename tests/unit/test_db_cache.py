"""Unit tests for DBContextCache — hash-based lazy cache for DB annotations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from laravelgraph.mcp.db_cache import DBContextCache


class TestSchemaHash:
    def test_hash_is_string(self):
        h = DBContextCache.schema_hash([{"name": "id", "type": "bigint"}])
        assert isinstance(h, str)

    def test_hash_length_is_12(self):
        h = DBContextCache.schema_hash([])
        assert len(h) == 12

    def test_same_columns_same_hash(self):
        cols = [{"name": "id", "type": "bigint"}, {"name": "email", "type": "string"}]
        assert DBContextCache.schema_hash(cols) == DBContextCache.schema_hash(cols)

    def test_different_columns_different_hash(self):
        a = DBContextCache.schema_hash([{"name": "id", "type": "bigint"}])
        b = DBContextCache.schema_hash([{"name": "id", "type": "int"}])
        assert a != b

    def test_order_independent(self):
        """Hash should be identical regardless of column list ordering."""
        cols_a = [{"name": "id", "type": "bigint"}, {"name": "email", "type": "string"}]
        cols_b = [{"name": "email", "type": "string"}, {"name": "id", "type": "bigint"}]
        assert DBContextCache.schema_hash(cols_a) == DBContextCache.schema_hash(cols_b)

    def test_empty_list_hash(self):
        h = DBContextCache.schema_hash([])
        assert len(h) == 12

    def test_extra_fields_affect_hash(self):
        a = DBContextCache.schema_hash([{"name": "id", "type": "bigint"}])
        b = DBContextCache.schema_hash([{"name": "id", "type": "bigint", "nullable": True}])
        # Dicts serialise differently when extra keys are present
        assert a != b


class TestGetSet:
    def test_set_and_get_returns_annotation(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Stores order records.", "anthropic")
        assert cache.get("dbctx:table:main:orders") == "Stores order records."

    def test_get_missing_returns_none(self, tmp_path):
        cache = DBContextCache(tmp_path)
        assert cache.get("dbctx:table:main:nonexistent") is None

    def test_set_persists_across_instances(self, tmp_path):
        cache1 = DBContextCache(tmp_path)
        cache1.set("dbctx:table:main:users", "User accounts.", "groq", schema_hash="abc123")

        cache2 = DBContextCache(tmp_path)
        assert cache2.get("dbctx:table:main:users", current_hash="abc123") == "User accounts."

    def test_set_overwrites_existing(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Old annotation.", "anthropic")
        cache.set("dbctx:table:main:orders", "New annotation.", "openai")
        assert cache.get("dbctx:table:main:orders") == "New annotation."

    def test_multiple_entries_independent(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders table.", "anthropic")
        cache.set("dbctx:table:main:users", "Users table.", "anthropic")
        assert cache.get("dbctx:table:main:orders") == "Orders table."
        assert cache.get("dbctx:table:main:users") == "Users table."

    def test_empty_annotation_returns_none(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "", "anthropic")
        assert cache.get("dbctx:table:main:orders") is None

    def test_db_context_json_created(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        assert (tmp_path / "db_context.json").exists()

    def test_json_file_is_valid_json(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        data = json.loads((tmp_path / "db_context.json").read_text())
        assert isinstance(data, dict)

    def test_model_stored_in_json(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "groq", schema_hash="abc")
        data = json.loads((tmp_path / "db_context.json").read_text())
        assert data["dbctx:table:main:orders"]["model"] == "groq"
        assert data["dbctx:table:main:orders"]["schema_hash"] == "abc"


class TestHashBasedInvalidation:
    def test_correct_hash_returns_annotation(self, tmp_path):
        cache = DBContextCache(tmp_path)
        h = DBContextCache.schema_hash([{"name": "id", "type": "bigint"}])
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic", schema_hash=h)
        assert cache.get("dbctx:table:main:orders", current_hash=h) == "Orders."

    def test_stale_hash_returns_none(self, tmp_path):
        cache = DBContextCache(tmp_path)
        old_h = DBContextCache.schema_hash([{"name": "id", "type": "bigint"}])
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic", schema_hash=old_h)

        new_h = DBContextCache.schema_hash([{"name": "id", "type": "bigint"}, {"name": "status", "type": "string"}])
        assert cache.get("dbctx:table:main:orders", current_hash=new_h) is None

    def test_stale_entry_removed_after_miss(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic", schema_hash="old_hash")
        cache.get("dbctx:table:main:orders", current_hash="new_hash")  # triggers removal

        # Reload from disk and confirm gone
        cache2 = DBContextCache(tmp_path)
        assert cache2.get("dbctx:table:main:orders") is None

    def test_no_hash_provided_skips_invalidation(self, tmp_path):
        """When no current_hash is given, the entry is never invalidated."""
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic", schema_hash="stale_hash")
        # No current_hash passed → no invalidation check
        assert cache.get("dbctx:table:main:orders") == "Orders."

    def test_get_with_no_stored_hash_still_returns_annotation(self, tmp_path):
        """Entry stored without a hash is always valid."""
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        assert cache.get("dbctx:table:main:orders", current_hash="anything") == "Orders."


class TestInvalidateConnection:
    def test_removes_all_entries_for_connection(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        cache.set("dbctx:table:main:users", "Users.", "anthropic")
        cache.set("dbctx:column:main:orders.id", "PK.", "anthropic")
        cache.set("dbctx:proc:main:calc_totals", "Totals proc.", "anthropic")
        cache.set("dbctx:table:analytics:events", "Events.", "anthropic")  # different conn

        removed = cache.invalidate_connection("main")
        assert removed == 4

        assert cache.get("dbctx:table:main:orders") is None
        assert cache.get("dbctx:table:main:users") is None
        assert cache.get("dbctx:column:main:orders.id") is None
        assert cache.get("dbctx:proc:main:calc_totals") is None
        # Different connection untouched
        assert cache.get("dbctx:table:analytics:events") == "Events."

    def test_invalidate_nonexistent_connection_returns_zero(self, tmp_path):
        cache = DBContextCache(tmp_path)
        assert cache.invalidate_connection("nonexistent") == 0

    def test_invalidation_persisted_to_disk(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        cache.invalidate_connection("main")

        cache2 = DBContextCache(tmp_path)
        assert cache2.get("dbctx:table:main:orders") is None


class TestStats:
    def test_empty_cache_stats(self, tmp_path):
        cache = DBContextCache(tmp_path)
        stats = cache.stats()
        assert stats["cached_entries"] == 0
        assert stats["by_type"] == {}

    def test_counts_entries_by_type(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        cache.set("dbctx:table:main:users", "Users.", "anthropic")
        cache.set("dbctx:column:main:orders.id", "PK.", "openai")
        cache.set("dbctx:proc:main:calc", "Procedure.", "groq")
        stats = cache.stats()
        assert stats["cached_entries"] == 4
        assert stats["by_type"]["table"] == 2
        assert stats["by_type"]["column"] == 1
        assert stats["by_type"]["proc"] == 1

    def test_models_used(self, tmp_path):
        cache = DBContextCache(tmp_path)
        cache.set("dbctx:table:main:orders", "Orders.", "anthropic")
        cache.set("dbctx:column:main:orders.id", "PK.", "groq")
        stats = cache.stats()
        assert set(stats["models_used"]) == {"anthropic", "groq"}


class TestCorruptFile:
    def test_corrupt_json_loads_empty(self, tmp_path):
        (tmp_path / "db_context.json").write_text("not valid json")
        cache = DBContextCache(tmp_path)
        assert cache.get("dbctx:table:main:orders") is None

    def test_corrupt_json_does_not_raise(self, tmp_path):
        (tmp_path / "db_context.json").write_text("{invalid")
        cache = DBContextCache(tmp_path)  # should not raise
        cache.set("dbctx:table:main:orders", "Recovered.", "anthropic")
        assert cache.get("dbctx:table:main:orders") == "Recovered."
