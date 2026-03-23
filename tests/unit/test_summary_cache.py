"""Unit tests for SummaryCache — read/write, mtime invalidation, stats."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from laravelgraph.mcp.cache import SummaryCache


class TestSummaryCacheReadWrite:
    def test_set_and_get_returns_summary(self, tmp_path):
        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "Does something useful.", "anthropic")
        assert sc.get("method:Foo::bar") == "Does something useful."

    def test_get_missing_returns_none(self, tmp_path):
        sc = SummaryCache(tmp_path)
        assert sc.get("method:NonExistent::foo") is None

    def test_set_persists_across_instances(self, tmp_path):
        sc1 = SummaryCache(tmp_path)
        sc1.set("method:Foo::bar", "Persisted summary.", "openai")

        sc2 = SummaryCache(tmp_path)  # new instance loads from disk
        assert sc2.get("method:Foo::bar") == "Persisted summary."

    def test_set_overwrites_existing(self, tmp_path):
        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "First summary.", "anthropic")
        sc.set("method:Foo::bar", "Updated summary.", "openai")
        assert sc.get("method:Foo::bar") == "Updated summary."

    def test_multiple_entries_independent(self, tmp_path):
        sc = SummaryCache(tmp_path)
        sc.set("method:A::foo", "Summary A.", "anthropic")
        sc.set("method:B::bar", "Summary B.", "openai")
        assert sc.get("method:A::foo") == "Summary A."
        assert sc.get("method:B::bar") == "Summary B."

    def test_empty_summary_returns_none(self, tmp_path):
        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "", "anthropic")
        # Empty string stored but .get() should return None for falsy values
        assert sc.get("method:Foo::bar") is None

    def test_stats_counts_entries(self, tmp_path):
        sc = SummaryCache(tmp_path)
        assert sc.stats()["cached_summaries"] == 0
        sc.set("method:Foo::a", "A.", "anthropic")
        sc.set("method:Foo::b", "B.", "openai")
        stats = sc.stats()
        assert stats["cached_summaries"] == 2
        assert set(stats["models_used"]) == {"anthropic", "openai"}

    def test_summaries_json_created(self, tmp_path):
        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "Some summary.", "anthropic")
        assert (tmp_path / "summaries.json").exists()


class TestSummaryCacheMtimeInvalidation:
    def test_stale_entry_invalidated_when_file_modified(self, tmp_path):
        php = tmp_path / "Foo.php"
        php.write_text("<?php class Foo {}")

        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "Original summary.", "anthropic", file_path=str(php))

        # Simulate file modification (bump mtime by 2 seconds)
        new_mtime = os.path.getmtime(str(php)) + 2.0
        os.utime(str(php), (new_mtime, new_mtime))

        # Should return None — file changed, entry is stale
        assert sc.get("method:Foo::bar", file_path=str(php)) is None

    def test_fresh_entry_not_invalidated(self, tmp_path):
        php = tmp_path / "Foo.php"
        php.write_text("<?php class Foo {}")

        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "Fresh summary.", "anthropic", file_path=str(php))

        # No file change — should still be valid
        assert sc.get("method:Foo::bar", file_path=str(php)) == "Fresh summary."

    def test_get_without_file_path_skips_mtime_check(self, tmp_path):
        php = tmp_path / "Foo.php"
        php.write_text("<?php class Foo {}")

        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "Cached.", "anthropic", file_path=str(php))

        # Modify the file
        new_mtime = os.path.getmtime(str(php)) + 5.0
        os.utime(str(php), (new_mtime, new_mtime))

        # Without file_path: no mtime check, returns cached value
        assert sc.get("method:Foo::bar") == "Cached."

    def test_missing_file_does_not_crash(self, tmp_path):
        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::bar", "Orphan summary.", "anthropic", file_path="/nonexistent/Foo.php")
        # OSError on missing file is swallowed — returns summary
        assert sc.get("method:Foo::bar", file_path="/nonexistent/Foo.php") == "Orphan summary."


class TestSummaryCacheInvalidateFile:
    def test_invalidate_file_removes_matching_entries(self, tmp_path):
        php = tmp_path / "Foo.php"
        php.write_text("<?php")

        sc = SummaryCache(tmp_path)
        sc.set("method:Foo::a", "A.", "anthropic", file_path=str(php))
        sc.set("method:Foo::b", "B.", "anthropic", file_path=str(php))
        sc.set("method:Bar::c", "C.", "anthropic", file_path=str(tmp_path / "Bar.php"))

        removed = sc.invalidate_file(str(php))
        assert removed == 2
        assert sc.get("method:Foo::a") is None
        assert sc.get("method:Foo::b") is None
        assert sc.get("method:Bar::c") == "C."

    def test_invalidate_nonexistent_file_returns_zero(self, tmp_path):
        sc = SummaryCache(tmp_path)
        assert sc.invalidate_file("/nonexistent/file.php") == 0

    def test_corrupt_cache_file_handled_gracefully(self, tmp_path):
        cache_path = tmp_path / "summaries.json"
        cache_path.write_text("not json {{{{")
        # Should not raise — falls back to empty cache
        sc = SummaryCache(tmp_path)
        assert sc.stats()["cached_summaries"] == 0
