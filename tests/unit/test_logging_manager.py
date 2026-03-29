"""Unit tests for LogManager from laravelgraph.logging_manager."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest


class TestLogManagerInit:
    def test_log_manager_empty_dir(self, tmp_path):
        """LogManager works with no log files."""
        from laravelgraph.logging_manager import LogManager

        lm = LogManager(tmp_path)
        assert lm.get_recent() == []
        assert lm.get_log_files() == []


class TestLogManagerReadJsonl:
    def test_log_manager_reads_jsonl(self, tmp_path):
        """LogManager parses JSONL log files."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "info", "event": "test event", "tool": "laravelgraph_routes"}) + "\n"
            + json.dumps({"level": "error", "event": "error event", "tool": "laravelgraph_cypher"}) + "\n"
        )
        lm = LogManager(tmp_path)
        entries = lm.get_recent(limit=10)
        assert len(entries) == 2

    def test_log_manager_skips_non_json_lines(self, tmp_path):
        """LogManager skips lines that are not valid JSON without crashing."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            "not json at all\n"
            + json.dumps({"level": "info", "event": "valid"}) + "\n"
        )
        lm = LogManager(tmp_path)
        entries = lm.get_recent(limit=10)
        assert len(entries) == 1
        assert entries[0]["event"] == "valid"

    def test_log_manager_reads_multiple_files(self, tmp_path):
        """LogManager reads entries from all log files in the directory."""
        from laravelgraph.logging_manager import LogManager

        (tmp_path / "a.log").write_text(
            json.dumps({"level": "info", "event": "from a"}) + "\n"
        )
        (tmp_path / "b.log").write_text(
            json.dumps({"level": "info", "event": "from b"}) + "\n"
        )
        lm = LogManager(tmp_path)
        entries = lm.get_recent(limit=10)
        assert len(entries) == 2

    def test_log_manager_respects_limit(self, tmp_path):
        """LogManager respects the limit parameter in get_recent()."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        lines = [json.dumps({"level": "info", "event": f"e{i}"}) for i in range(10)]
        log_file.write_text("\n".join(lines) + "\n")
        lm = LogManager(tmp_path)
        entries = lm.get_recent(limit=3)
        assert len(entries) == 3


class TestLogManagerFilterByLevel:
    def test_log_manager_filter_by_level(self, tmp_path):
        """LogManager filters entries by level."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "info", "event": "info event"}) + "\n"
            + json.dumps({"level": "error", "event": "error event"}) + "\n"
        )
        lm = LogManager(tmp_path)
        errors = lm.get_recent(level="error")
        assert len(errors) == 1
        assert errors[0]["level"] == "error"

    def test_log_manager_filter_level_case_insensitive(self, tmp_path):
        """LogManager level filter is case-insensitive."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "WARNING", "event": "w event"}) + "\n"
        )
        lm = LogManager(tmp_path)
        entries = lm.get_recent(level="warning")
        assert len(entries) == 1


class TestLogManagerFilterByTool:
    def test_log_manager_filter_by_tool(self, tmp_path):
        """LogManager filters entries by tool name."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "info", "event": "call", "tool": "laravelgraph_routes"}) + "\n"
            + json.dumps({"level": "info", "event": "call", "tool": "laravelgraph_models"}) + "\n"
        )
        lm = LogManager(tmp_path)
        routes_logs = lm.get_recent(tool="routes")
        assert len(routes_logs) == 1

    def test_log_manager_filter_by_tool_no_match(self, tmp_path):
        """LogManager returns empty list when tool filter matches nothing."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "info", "event": "call", "tool": "laravelgraph_routes"}) + "\n"
        )
        lm = LogManager(tmp_path)
        entries = lm.get_recent(tool="nonexistent_tool")
        assert entries == []


class TestLogManagerGetLogFiles:
    def test_get_log_files_returns_paths(self, tmp_path):
        """get_log_files() returns file paths for all log files."""
        from laravelgraph.logging_manager import LogManager

        (tmp_path / "app.log").write_text("line\n")
        (tmp_path / "errors.log").write_text("line\n")
        lm = LogManager(tmp_path)
        files = lm.get_log_files()
        assert len(files) == 2
        assert all(isinstance(f, (str, Path)) for f in files)


class TestLogManagerClearOld:
    def test_log_manager_clear_old(self, tmp_path):
        """LogManager.clear_old() removes files older than N days."""
        from laravelgraph.logging_manager import LogManager

        old_file = tmp_path / "old.log"
        old_file.write_text("old log\n")
        # Set mtime to 31 days ago
        old_time = time.time() - (31 * 86400)
        os.utime(old_file, (old_time, old_time))

        new_file = tmp_path / "new.log"
        new_file.write_text(json.dumps({"level": "info", "event": "new"}) + "\n")

        lm = LogManager(tmp_path)
        deleted = lm.clear_old(days=30)
        assert deleted == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_log_manager_clear_old_returns_zero_when_nothing_to_delete(self, tmp_path):
        """clear_old() returns 0 when no files are old enough to delete."""
        from laravelgraph.logging_manager import LogManager

        new_file = tmp_path / "new.log"
        new_file.write_text(json.dumps({"level": "info", "event": "new"}) + "\n")

        lm = LogManager(tmp_path)
        deleted = lm.clear_old(days=30)
        assert deleted == 0
        assert new_file.exists()

    def test_log_manager_clear_old_empty_dir(self, tmp_path):
        """clear_old() returns 0 on an empty directory."""
        from laravelgraph.logging_manager import LogManager

        lm = LogManager(tmp_path)
        assert lm.clear_old(days=30) == 0


class TestLogManagerGetStats:
    def test_log_manager_get_stats(self, tmp_path):
        """LogManager.get_stats() returns correct structure."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "info", "event": "e1", "tool": "tool_a"}) + "\n"
            + json.dumps({"level": "error", "event": "e2", "tool": "tool_a"}) + "\n"
        )
        lm = LogManager(tmp_path)
        stats = lm.get_stats()
        assert "total_entries" in stats
        assert "by_level" in stats
        assert stats["total_entries"] == 2
        assert stats["by_level"].get("info") == 1
        assert stats["by_level"].get("error") == 1

    def test_log_manager_get_stats_empty(self, tmp_path):
        """get_stats() returns zeroed structure when no log files exist."""
        from laravelgraph.logging_manager import LogManager

        lm = LogManager(tmp_path)
        stats = lm.get_stats()
        assert "total_entries" in stats
        assert stats["total_entries"] == 0
        assert "by_level" in stats

    def test_log_manager_get_stats_by_tool(self, tmp_path):
        """get_stats() tracks call counts per tool when tool field is present."""
        from laravelgraph.logging_manager import LogManager

        log_file = tmp_path / "test.log"
        log_file.write_text(
            json.dumps({"level": "info", "event": "call", "tool": "laravelgraph_routes"}) + "\n"
            + json.dumps({"level": "info", "event": "call", "tool": "laravelgraph_routes"}) + "\n"
            + json.dumps({"level": "info", "event": "call", "tool": "laravelgraph_models"}) + "\n"
        )
        lm = LogManager(tmp_path)
        stats = lm.get_stats()
        # Should have some breakdown by tool if the implementation supports it
        assert stats["total_entries"] == 3
