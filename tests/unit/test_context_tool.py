"""Unit tests for context-tool behaviour:
- Source code appears on first query (cache cold) and when include_source=True
- Source code is omitted when cache is warm and include_source=False (default)
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


# ── Source inclusion logic ────────────────────────────────────────────────────

def _should_include_source(cached_summary, include_source, fp, ls):
    """Mirrors the should_include_source condition in server.py laravelgraph_context."""
    return bool(fp and ls and (not cached_summary or include_source))


class TestSourceInclusionLogic:
    """Source appears on cold cache or explicit include_source=True; omitted on warm cache."""

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

    def _render(self, php_file, cached_summary, include_source=False):
        from laravelgraph.mcp.explain import _append_source_block
        lines: list[str] = []
        fp, ls, le = str(php_file), 3, 5
        if _should_include_source(cached_summary, include_source, fp, ls):
            _append_source_block(fp, ls, le, project_root=None, lines=lines)
        return "\n".join(lines)

    # Cold cache — source always included
    def test_source_appears_on_cold_cache(self, php_file):
        out = self._render(php_file, cached_summary=None)
        assert "```php" in out
        assert "posts.index" in out

    # Warm cache + default — source omitted (the big change)
    def test_source_omitted_when_cache_warm_default(self, php_file):
        out = self._render(php_file, cached_summary="Handles posts listing.")
        assert "```php" not in out, "Source must be omitted when cache is warm and include_source=False"

    # Warm cache + include_source=True — source returned
    def test_source_included_when_cache_warm_and_flag_set(self, php_file):
        out = self._render(php_file, cached_summary="Handles posts listing.", include_source=True)
        assert "```php" in out
        assert "posts.index" in out

    # Cold cache + include_source=True — source returned (no change)
    def test_source_included_on_cold_cache_with_flag(self, php_file):
        out = self._render(php_file, cached_summary=None, include_source=True)
        assert "```php" in out

    # No file path — always skip regardless
    def test_source_absent_when_no_file_path(self):
        assert not _should_include_source(None, False, "", 3)
        assert not _should_include_source(None, True, "", 3)

    # No line start — always skip
    def test_source_absent_when_no_line_start(self, php_file):
        assert not _should_include_source(None, False, str(php_file), 0)

    # Missing file — no crash, no source block
    def test_source_absent_for_missing_file(self, tmp_path):
        from laravelgraph.mcp.explain import _append_source_block
        lines: list[str] = []
        fp = str(tmp_path / "NonExistent.php")
        if _should_include_source(None, False, fp, 1):
            _append_source_block(fp, 1, 5, project_root=None, lines=lines)
        assert "```php" not in "\n".join(lines)
