"""Unit tests for explain.py — Option 1 (source injection) and Option 2 (docblock enrichment)."""

from __future__ import annotations

from pathlib import Path

import pytest

from laravelgraph.mcp.explain import clean_docblock, read_source_snippet


FIXTURES = Path(__file__).parent.parent / "fixtures" / "tiny-laravel-app"


# ── clean_docblock (Option 2) ─────────────────────────────────────────────────

class TestCleanDocblock:
    def test_strips_asterisk_prefixes(self):
        raw = "/**\n * Does something useful.\n */"
        result = clean_docblock(raw)
        assert "*" not in result
        assert "Does something useful" in result

    def test_strips_at_tags(self):
        raw = "/**\n * Process the payment.\n * @param string $id\n * @return void\n */"
        result = clean_docblock(raw)
        assert "@param" not in result
        assert "@return" not in result
        assert "Process the payment" in result

    def test_stops_at_first_at_tag(self):
        raw = "/**\n * First line.\n * Second line.\n * @param string $x\n * Third line.\n */"
        result = clean_docblock(raw)
        # Description lines are before @tags
        assert "First line" in result
        assert "Second line" in result
        assert "Third line" not in result

    def test_multiline_description_joined(self):
        raw = "/**\n * Send the welcome email\n * to the newly registered user.\n * @return void\n */"
        result = clean_docblock(raw)
        assert "Send the welcome email" in result
        assert "to the newly registered user" in result

    def test_empty_docblock_returns_empty(self):
        assert clean_docblock("") == ""
        assert clean_docblock(None) == ""  # type: ignore[arg-type]

    def test_docblock_with_only_tags_returns_empty(self):
        raw = "/**\n * @param string $x\n * @return void\n */"
        result = clean_docblock(raw)
        assert result == ""

    def test_real_fixture_listener_docblock(self):
        """Verify the SendWelcomeEmail fixture docblock is correctly cleaned."""
        listener_file = (
            FIXTURES / "app" / "Listeners" / "SendWelcomeEmail.php"
        )
        content = listener_file.read_text()
        # Extract the docblock manually
        start = content.index("/**")
        end = content.index("*/", start) + 2
        raw = content[start:end]
        result = clean_docblock(raw)
        assert "welcome email" in result.lower()
        assert "@param" not in result
        assert "@return" not in result
        assert "WelcomeNotification" in result or "welcome" in result.lower()

    def test_real_fixture_controller_docblock(self):
        """Verify the PostController.index docblock is correctly cleaned."""
        ctrl_file = (
            FIXTURES / "app" / "Http" / "Controllers" / "PostController.php"
        )
        content = ctrl_file.read_text()
        start = content.index("/**")
        end = content.index("*/", start) + 2
        raw = content[start:end]
        result = clean_docblock(raw)
        assert "published posts" in result.lower() or "paginated" in result.lower()


# ── read_source_snippet (Option 1) ───────────────────────────────────────────

class TestReadSourceSnippet:
    def test_reads_correct_lines(self, tmp_path):
        php = tmp_path / "Test.php"
        php.write_text("<?php\nclass Foo {\n    public function bar(): void\n    {\n        // body\n    }\n}\n")
        snippet = read_source_snippet(str(php), 3, 6)
        assert "public function bar" in snippet
        assert "// body" in snippet

    def test_absolute_path_no_project_root(self, tmp_path):
        php = tmp_path / "Abs.php"
        php.write_text("line1\nline2\nline3\n")
        snippet = read_source_snippet(str(php), 1, 3)
        assert "line1" in snippet
        assert "line3" in snippet

    def test_relative_path_with_project_root(self, tmp_path):
        (tmp_path / "app").mkdir()
        php = tmp_path / "app" / "Foo.php"
        php.write_text("<?php\nclass Foo {}\n")
        snippet = read_source_snippet("app/Foo.php", 1, 2, project_root=tmp_path)
        assert "class Foo" in snippet

    def test_missing_file_returns_empty(self):
        result = read_source_snippet("/nonexistent/path/Foo.php", 1, 10)
        assert result == ""

    def test_empty_file_path_returns_empty(self):
        assert read_source_snippet("", 1, 10) == ""

    def test_line_start_zero_returns_empty(self):
        assert read_source_snippet("/some/file.php", 0, 5) == ""

    def test_caps_at_max_snippet_lines(self, tmp_path):
        php = tmp_path / "Big.php"
        php.write_text("\n".join(f"line{i}" for i in range(1, 200)) + "\n")
        snippet = read_source_snippet(str(php), 1, 100)
        lines = snippet.splitlines()
        # Should be capped — last line is either a truncation notice or within limit
        assert len(lines) <= 122  # _MAX_SNIPPET_LINES (120) + 1 for truncation note + 1 fence

    def test_reads_real_fixture_controller(self):
        """Read the index() method from PostController fixture."""
        ctrl = FIXTURES / "app" / "Http" / "Controllers" / "PostController.php"
        # index() starts at line 13 (after docblock), ends around line 16
        snippet = read_source_snippet(str(ctrl), 13, 17)
        assert snippet != ""
        assert "posts.index" in snippet or "index" in snippet

    def test_reads_real_fixture_listener(self):
        """Read the handle() method from SendWelcomeEmail fixture."""
        listener = FIXTURES / "app" / "Listeners" / "SendWelcomeEmail.php"
        snippet = read_source_snippet(str(listener), 18, 21)
        assert snippet != ""
        assert "WelcomeNotification" in snippet or "notify" in snippet

    def test_truncation_note_appended_for_long_methods(self, tmp_path):
        php = tmp_path / "Long.php"
        # 200 lines — well above _MAX_SNIPPET_LINES (120) to trigger truncation
        php.write_text("\n".join(f"    $line{i} = {i};" for i in range(1, 201)) + "\n")
        snippet = read_source_snippet(str(php), 1, 200)
        assert "more lines" in snippet


# ── Integration: trace_method_flow includes source and docblock ───────────────

class TestTraceMethodFlowIntegration:
    """Run the pipeline on the fixture and verify trace_method_flow output."""

    @pytest.fixture(scope="class")
    def ctx(self, tmp_path_factory):
        import shutil
        tmp = tmp_path_factory.mktemp("explain_source_test")
        app_copy = tmp / "tiny-laravel-app"
        shutil.copytree(str(FIXTURES), str(app_copy))

        from laravelgraph.config import Config
        from laravelgraph.pipeline.orchestrator import Pipeline
        cfg = Config()
        pipeline = Pipeline(app_copy, config=cfg)
        return pipeline.run(full=True, skip_embeddings=True), app_copy

    def test_trace_method_flow_includes_source_snippet(self, ctx):
        """trace_method_flow should include a fenced PHP code block."""
        pipeline_ctx, app_root = ctx
        db = pipeline_ctx.db

        from laravelgraph.mcp.explain import trace_method_flow
        lines: list[str] = []
        trace_method_flow(
            db,
            "App\\Http\\Controllers\\PostController",
            "index",
            lines,
            project_root=app_root,
        )
        full_output = "\n".join(lines)
        assert "```php" in full_output, (
            "Expected a fenced PHP code block in trace_method_flow output. "
            f"Got:\n{full_output}"
        )
        assert "posts.index" in full_output, (
            "Expected the view() call 'posts.index' to appear in the source snippet."
        )

    def test_trace_method_flow_includes_docblock(self, ctx):
        """trace_method_flow should include the cleaned docblock as Purpose."""
        pipeline_ctx, app_root = ctx
        db = pipeline_ctx.db

        from laravelgraph.mcp.explain import trace_method_flow
        lines: list[str] = []
        trace_method_flow(
            db,
            "App\\Http\\Controllers\\PostController",
            "index",
            lines,
            project_root=app_root,
        )
        full_output = "\n".join(lines)
        assert "**Purpose:**" in full_output, (
            "Expected a **Purpose:** line from the cleaned docblock."
        )
        assert "published posts" in full_output.lower() or "paginated" in full_output.lower(), (
            "Expected docblock content about published/paginated posts in output."
        )

    def test_trace_method_flow_source_works_with_absolute_path(self, ctx):
        """The graph stores absolute file paths — source works even without project_root."""
        pipeline_ctx, _ = ctx
        db = pipeline_ctx.db

        from laravelgraph.mcp.explain import trace_method_flow
        lines: list[str] = []
        trace_method_flow(
            db,
            "App\\Http\\Controllers\\PostController",
            "index",
            lines,
            project_root=None,  # No project_root — but graph has absolute paths
        )
        full_output = "\n".join(lines)
        # Absolute paths in the graph mean source is still readable without project_root
        assert "```php" in full_output, (
            "Source snippet should be available via the absolute path stored in the graph."
        )
