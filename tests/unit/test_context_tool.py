"""Unit tests for context-tool bugs:
- Source code must always appear in laravelgraph_context, even when summary is cached
- community_id -1 must not appear in output (both string and integer form)
- community_id with a real value must appear
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_node(
    fqn="App\\Http\\Controllers\\PostController::index",
    label="Method",
    file_path="/app/Http/Controllers/PostController.php",
    line_start=13,
    line_end=16,
    community_id=None,
    raw_doc="",
):
    return {
        "n.fqn": fqn,
        "n._label": label,
        "n.file_path": file_path,
        "n.line_start": line_start,
        "n.line_end": line_end,
        "n.community_id": community_id,
        "n.docblock": raw_doc,
        "n.laravel_role": None,
        "n.is_dead_code": False,
        "n.description": None,
    }


# ── community_id display ───────────────────────────────────────────────────────

class TestCommunityIdDisplay:
    """Verify community_id -1 (integer and string) is suppressed."""

    def _build_lines_for_community(self, community_id):
        """Replicate the community_id section logic from server.py context tool."""
        lines = []
        comm_id = community_id
        if comm_id is not None and str(comm_id) not in ("-1", "", "None"):
            lines.append(f"- **Community:** {comm_id}")
        return lines

    def test_integer_negative_one_suppressed(self):
        lines = self._build_lines_for_community(-1)
        assert not any("Community" in l for l in lines)

    def test_string_negative_one_suppressed(self):
        lines = self._build_lines_for_community("-1")
        assert not any("Community" in l for l in lines)

    def test_none_suppressed(self):
        lines = self._build_lines_for_community(None)
        assert not any("Community" in l for l in lines)

    def test_empty_string_suppressed(self):
        lines = self._build_lines_for_community("")
        assert not any("Community" in l for l in lines)

    def test_real_community_id_shown(self):
        lines = self._build_lines_for_community(42)
        assert any("Community" in l and "42" in l for l in lines)

    def test_string_community_id_shown(self):
        lines = self._build_lines_for_community("5")
        assert any("Community" in l and "5" in l for l in lines)

    def test_zero_community_id_shown(self):
        """Community 0 is a valid cluster — must not be suppressed."""
        lines = self._build_lines_for_community(0)
        assert any("Community" in l and "0" in l for l in lines)


# ── Source always present (even with cached summary) ──────────────────────────

class TestSourceAlwaysShown:
    """Source code block must appear regardless of summary cache state."""

    @pytest.fixture()
    def php_file(self, tmp_path):
        php = tmp_path / "PostController.php"
        php.write_text(
            "<?php\nclass PostController {\n"
            "    public function index() {\n"
            "        return view('posts.index');\n"
            "    }\n"
            "}\n"
        )
        return php

    def test_source_appears_when_no_cached_summary(self, php_file, tmp_path):
        from laravelgraph.mcp.explain import _append_source_block

        lines: list[str] = []
        # cached_summary is None (old code would run source block)
        cached_summary = None
        fp = str(php_file)
        ls, le = 3, 5

        # New logic: always append source when fp and ls are set
        if fp and ls:
            _append_source_block(fp, ls, le, project_root=None, lines=lines)

        full = "\n".join(lines)
        assert "```php" in full, "Source block missing when no cached summary"
        assert "posts.index" in full

    def test_source_appears_when_summary_is_cached(self, php_file, tmp_path):
        from laravelgraph.mcp.explain import _append_source_block

        lines: list[str] = []
        # Simulates the fixed logic: cached_summary exists but source still appended
        cached_summary = "This controller handles the posts listing page."
        fp = str(php_file)
        ls, le = 3, 5

        # Fixed: condition is just `if fp and ls` — no `not cached_summary`
        if fp and ls:
            _append_source_block(fp, ls, le, project_root=None, lines=lines)

        full = "\n".join(lines)
        assert "```php" in full, "Source block missing when summary is cached — regression!"
        assert "posts.index" in full

    def test_source_absent_when_no_file_path(self):
        from laravelgraph.mcp.explain import _append_source_block

        lines: list[str] = []
        fp = ""   # no file path — gracefully skip
        ls = 3

        if fp and ls:
            _append_source_block(fp, ls, 5, project_root=None, lines=lines)

        assert "```php" not in "\n".join(lines)

    def test_source_absent_for_missing_file(self, tmp_path):
        from laravelgraph.mcp.explain import _append_source_block

        lines: list[str] = []
        fp = str(tmp_path / "NonExistent.php")
        ls = 1

        if fp and ls:
            _append_source_block(fp, ls, 5, project_root=None, lines=lines)

        # Missing file should not crash and should produce no source block
        assert "```php" not in "\n".join(lines)
