"""Unit tests for laravelgraph.agent_installer."""
from __future__ import annotations

from pathlib import Path


# ── helpers ───────────────────────────────────────────────────────────────────

def _import():
    from laravelgraph import agent_installer
    return agent_installer


def _full_block(mod) -> str:
    """Return the full agent block (minimal CLAUDE.md block + rich body)."""
    # build_minimal_block has the section markers; build_rich_agent_body has the protocol.
    # Tests that check protocol content should use build_rich_agent_body (the full content).
    return mod.build_rich_agent_body(mod.DynamicData())


# ── build_minimal_block ───────────────────────────────────────────────────────

class TestBuildAgentBlock:
    def test_contains_start_marker(self):
        mod = _import()
        block = mod.build_minimal_block()
        assert mod._SECTION_START in block

    def test_contains_end_marker(self):
        mod = _import()
        block = mod.build_minimal_block()
        assert mod._SECTION_END in block

    def test_contains_tool_hierarchy(self):
        mod = _import()
        block = _full_block(mod)
        assert "Tool Hierarchy" in block

    def test_mentions_feature_context(self):
        mod = _import()
        block = mod.build_minimal_block()
        assert "laravelgraph_feature_context" in block

    def test_mentions_store_discoveries_protocol(self):
        mod = _import()
        block = _full_block(mod)
        assert "store_discoveries" in block
        assert "findings persist across sessions" in block

    def test_mentions_plugin_knowledge(self):
        mod = _import()
        block = _full_block(mod)
        assert "laravelgraph_plugin_knowledge" in block

    def test_mentions_plugin_evolve(self):
        mod = _import()
        block = _full_block(mod)
        assert "plugin evolve" in block

    def test_lists_common_pitfalls(self):
        mod = _import()
        block = _full_block(mod)
        assert "Pitfall" in block or "pitfall" in block.lower() or "Pitfalls" in block

    def test_mentions_request_flow(self):
        mod = _import()
        block = mod.build_minimal_block()
        assert "laravelgraph_request_flow" in block

    def test_mentions_impact(self):
        mod = _import()
        block = mod.build_minimal_block()
        assert "laravelgraph_impact" in block


# ── _upsert_section ───────────────────────────────────────────────────────────

class TestUpsertSection:
    def test_appends_to_empty_file(self, tmp_path):
        mod = _import()
        target = tmp_path / "CLAUDE.md"
        block = f"{mod._SECTION_START}\nHello\n{mod._SECTION_END}"
        mod._upsert_section(target, block)
        content = target.read_text()
        assert "Hello" in content

    def test_appends_to_existing_file(self, tmp_path):
        mod = _import()
        target = tmp_path / "CLAUDE.md"
        target.write_text("# Existing content\n")
        block = f"{mod._SECTION_START}\nNew section\n{mod._SECTION_END}"
        mod._upsert_section(target, block)
        content = target.read_text()
        assert "# Existing content" in content
        assert "New section" in content

    def test_replaces_existing_section(self, tmp_path):
        mod = _import()
        target = tmp_path / "CLAUDE.md"
        initial = f"Before\n{mod._SECTION_START}\nOld content\n{mod._SECTION_END}\nAfter"
        target.write_text(initial)

        new_block = f"{mod._SECTION_START}\nNew content\n{mod._SECTION_END}"
        mod._upsert_section(target, new_block)

        content = target.read_text()
        assert "New content" in content
        assert "Old content" not in content
        assert "Before" in content
        assert "After" in content

    def test_idempotent(self, tmp_path):
        mod = _import()
        target = tmp_path / "CLAUDE.md"
        block = f"{mod._SECTION_START}\nContent\n{mod._SECTION_END}"

        mod._upsert_section(target, block)
        mod._upsert_section(target, block)

        content = target.read_text()
        # Should appear exactly once
        assert content.count(mod._SECTION_START) == 1

    def test_creates_nonexistent_file(self, tmp_path):
        mod = _import()
        target = tmp_path / "subdir" / "CLAUDE.md"
        target.parent.mkdir()
        block = f"{mod._SECTION_START}\nContent\n{mod._SECTION_END}"
        mod._upsert_section(target, block)
        assert target.exists()
        assert "Content" in target.read_text()


# ── install targets ───────────────────────────────────────────────────────────

class TestInstallTargets:
    def test_install_for_claude_code(self, tmp_path):
        mod = _import()
        written = mod.install_for_claude_code(tmp_path)
        # Returns a list of written paths; CLAUDE.md is always included
        written_paths = written if isinstance(written, list) else [written]
        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md in written_paths
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "LaravelGraph" in content

    def test_install_for_opencode(self, tmp_path):
        mod = _import()
        written = mod.install_for_opencode(tmp_path)
        written_paths = written if isinstance(written, list) else [written]
        target = tmp_path / ".opencode" / "instructions.md"
        assert target in written_paths
        assert target.exists()
        content = target.read_text()
        assert "LaravelGraph" in content

    def test_install_for_cursor(self, tmp_path):
        mod = _import()
        written = mod.install_for_cursor(tmp_path)
        written_paths = written if isinstance(written, list) else [written]
        target = tmp_path / ".cursorrules"
        assert target in written_paths
        assert target.exists()
        content = target.read_text()
        assert "LaravelGraph" in content

    def test_install_creates_opencode_dir(self, tmp_path):
        mod = _import()
        assert not (tmp_path / ".opencode").exists()
        mod.install_for_opencode(tmp_path)
        assert (tmp_path / ".opencode").is_dir()

    def test_install_target_keys(self):
        mod = _import()
        assert "claude-code" in mod.INSTALL_TARGETS
        assert "opencode"    in mod.INSTALL_TARGETS
        assert "cursor"      in mod.INSTALL_TARGETS
