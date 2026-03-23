"""Tests for the Blade template parser against the tiny-laravel-app fixture files."""

from __future__ import annotations

from pathlib import Path

import pytest

from laravelgraph.parsers.blade import BladeParser, BladeParsed

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"
VIEWS_ROOT = TINY_APP / "resources" / "views"


@pytest.fixture(scope="module")
def parser() -> BladeParser:
    return BladeParser()


@pytest.fixture(scope="module")
def index_blade(parser: BladeParser) -> BladeParsed:
    return parser.parse_file(VIEWS_ROOT / "posts" / "index.blade.php", VIEWS_ROOT)


@pytest.fixture(scope="module")
def layout_blade(parser: BladeParser) -> BladeParsed:
    return parser.parse_file(VIEWS_ROOT / "layouts" / "app.blade.php", VIEWS_ROOT)


# ── @extends ─────────────────────────────────────────────────────────────────

class TestParseExtends:
    def test_extends_detected(self, index_blade: BladeParsed):
        assert index_blade.extends == "layouts.app"

    def test_layout_has_no_extends(self, layout_blade: BladeParsed):
        assert layout_blade.extends is None


# ── @section ─────────────────────────────────────────────────────────────────

class TestParseSections:
    def test_sections_detected(self, index_blade: BladeParsed):
        assert "content" in index_blade.sections
        assert "title" in index_blade.sections

    def test_layout_has_no_sections(self, layout_blade: BladeParsed):
        # layouts/app.blade.php uses @yield, not @section
        assert len(layout_blade.sections) == 0


# ── @yield ────────────────────────────────────────────────────────────────────

class TestParseYields:
    def test_yields_detected_in_layout(self, layout_blade: BladeParsed):
        assert "title" in layout_blade.yields
        assert "content" in layout_blade.yields

    def test_index_has_no_yields(self, index_blade: BladeParsed):
        assert len(index_blade.yields) == 0


# ── @stack ────────────────────────────────────────────────────────────────────

class TestParseStacks:
    def test_stack_detected_in_layout(self, layout_blade: BladeParsed):
        assert "scripts" in layout_blade.stacks

    def test_index_has_no_stacks(self, index_blade: BladeParsed):
        assert len(index_blade.stacks) == 0


# ── <x-component> tags ────────────────────────────────────────────────────────

class TestParseComponents:
    def test_x_post_card_found_in_index(self, index_blade: BladeParsed):
        component_tags = [c.tag for c in index_blade.components]
        assert "post-card" in component_tags

    def test_x_nav_bar_found_in_layout(self, layout_blade: BladeParsed):
        component_tags = [c.tag for c in layout_blade.components]
        assert "nav-bar" in component_tags

    def test_components_are_x_tags(self, index_blade: BladeParsed):
        for comp in index_blade.components:
            assert comp.is_x_tag is True

    def test_layout_component_is_x_tag(self, layout_blade: BladeParsed):
        nav_bar = next(c for c in layout_blade.components if c.tag == "nav-bar")
        assert nav_bar.is_x_tag is True


# ── View name derivation ──────────────────────────────────────────────────────

class TestViewNameDerivation:
    def test_index_view_name_dot_notation(self, index_blade: BladeParsed):
        assert index_blade.view_name == "posts.index"

    def test_layout_view_name_dot_notation(self, layout_blade: BladeParsed):
        assert layout_blade.view_name == "layouts.app"

    def test_view_name_without_views_root(self, parser: BladeParser):
        """When views_root is not provided, use stem-based name."""
        result = parser.parse_file(VIEWS_ROOT / "posts" / "index.blade.php")
        # Should at minimum strip the .blade part
        assert "blade" not in result.view_name
        assert result.view_name == "index"

    def test_derive_view_name_static_method(self):
        """Test the static _derive_view_name directly."""
        views_root = Path("/app/resources/views")
        path = Path("/app/resources/views/admin/dashboard/index.blade.php")
        result = BladeParser._derive_view_name(path, views_root)
        assert result == "admin.dashboard.index"

    def test_derive_view_name_no_views_root(self):
        path = Path("/app/resources/views/posts/index.blade.php")
        result = BladeParser._derive_view_name(path, None)
        assert result == "index"


# ── @include directives ───────────────────────────────────────────────────────

class TestParseIncludes:
    def test_include_directive_parsed(self, parser: BladeParser, tmp_path: Path):
        blade_content = "@include('partials.header')\n@includeIf('partials.sidebar')"
        blade_file = tmp_path / "test.blade.php"
        blade_file.write_text(blade_content)
        result = parser.parse_file(blade_file)
        assert "partials.header" in result.includes
        assert "partials.sidebar" in result.includes


# ── @push / @prepend directives ───────────────────────────────────────────────

class TestParsePushes:
    def test_push_directive_parsed(self, parser: BladeParser, tmp_path: Path):
        blade_content = "@push('scripts')\n<script>console.log('hi');</script>\n@endpush"
        blade_file = tmp_path / "test.blade.php"
        blade_file.write_text(blade_content)
        result = parser.parse_file(blade_file)
        assert "scripts" in result.pushes


# ── Livewire components ───────────────────────────────────────────────────────

class TestLivewireComponents:
    def test_livewire_tag_parsed(self, parser: BladeParser, tmp_path: Path):
        blade_content = "<livewire:user-table />\n<livewire:search-bar />"
        blade_file = tmp_path / "test.blade.php"
        blade_file.write_text(blade_content)
        result = parser.parse_file(blade_file)
        assert "user-table" in result.livewire_components
        assert "search-bar" in result.livewire_components

    def test_livewire_directive_parsed(self, parser: BladeParser, tmp_path: Path):
        blade_content = "@livewire('user-form')"
        blade_file = tmp_path / "test.blade.php"
        blade_file.write_text(blade_content)
        result = parser.parse_file(blade_file)
        assert "user-form" in result.livewire_components


# ── Variables ─────────────────────────────────────────────────────────────────

class TestVariables:
    def test_blade_variable_extracted(self, index_blade: BladeParsed):
        # {{ $posts->links() }} — $posts should be in variables
        assert "posts" in index_blade.variables

    def test_variables_deduped(self, parser: BladeParser, tmp_path: Path):
        blade_content = "{{ $user->name }} {{ $user->email }} {{ $user->id }}"
        blade_file = tmp_path / "test.blade.php"
        blade_file.write_text(blade_content)
        result = parser.parse_file(blade_file)
        # "user" should appear only once despite multiple references
        assert result.variables.count("user") == 1


# ── Plain HTML / no directives ────────────────────────────────────────────────

class TestPlainHtmlBlade:
    def test_blade_file_with_no_directives(self, parser: BladeParser, tmp_path: Path):
        blade_content = """<!DOCTYPE html>
<html>
<head><title>Plain</title></head>
<body><p>Hello world</p></body>
</html>
"""
        blade_file = tmp_path / "plain.blade.php"
        blade_file.write_text(blade_content)
        result = parser.parse_file(blade_file)
        assert result.extends is None
        assert result.sections == []
        assert result.yields == []
        assert result.components == []
        assert result.errors == []


# ── Error handling ────────────────────────────────────────────────────────────

class TestBladeParserErrors:
    def test_missing_file_returns_errors(self, parser: BladeParser):
        result = parser.parse_file(Path("/nonexistent/path/missing.blade.php"))
        assert len(result.errors) >= 1
        assert result.extends is None

    def test_missing_file_has_empty_sections(self, parser: BladeParser):
        result = parser.parse_file(Path("/nonexistent/path/missing.blade.php"))
        assert result.sections == []
        assert result.yields == []
