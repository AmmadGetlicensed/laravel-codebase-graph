"""Tests for the composer.json parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from laravelgraph.parsers.composer import (
    ComposerInfo,
    PSR4Mapping,
    build_class_map,
    parse_composer,
)

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def composer_info() -> ComposerInfo:
    return parse_composer(TINY_APP / "composer.json")


# ── Basic parsing ─────────────────────────────────────────────────────────────

class TestParseComposer:
    def test_parse_returns_composer_info(self, composer_info: ComposerInfo):
        assert isinstance(composer_info, ComposerInfo)

    def test_no_errors(self, composer_info: ComposerInfo):
        assert composer_info.errors == []

    def test_name_extracted(self, composer_info: ComposerInfo):
        assert composer_info.name == "test/tiny-app"

    def test_description_extracted(self, composer_info: ComposerInfo):
        assert composer_info.description == "Tiny Laravel test fixture"

    def test_php_constraint_extracted(self, composer_info: ComposerInfo):
        assert composer_info.php_constraint == "^8.2"


# ── Laravel version detection ─────────────────────────────────────────────────

class TestLaravelVersion:
    def test_laravel_version_extracted(self, composer_info: ComposerInfo):
        assert composer_info.laravel_version == "11.0"  # fixture has "^11.0"

    def test_laravel_version_major_minor(self, tmp_path: Path):
        """Test version extraction from various constraint formats."""
        composer_data = {
            "require": {"laravel/framework": "^10.0"},
            "autoload": {"psr-4": {"App\\": "app/"}},
        }
        import json
        composer_file = tmp_path / "composer.json"
        composer_file.write_text(json.dumps(composer_data))
        result = parse_composer(composer_file)
        assert result.laravel_version == "10.0"

    def test_laravel_version_tilde(self, tmp_path: Path):
        import json
        composer_data = {"require": {"laravel/framework": "~11.3"}}
        composer_file = tmp_path / "composer.json"
        composer_file.write_text(json.dumps(composer_data))
        result = parse_composer(composer_file)
        assert result.laravel_version == "11.3"

    def test_laravel_version_unknown_when_missing(self, tmp_path: Path):
        import json
        composer_data = {"require": {"php": "^8.2"}}
        composer_file = tmp_path / "composer.json"
        composer_file.write_text(json.dumps(composer_data))
        result = parse_composer(composer_file)
        assert result.laravel_version == "unknown"


# ── PSR-4 mappings ────────────────────────────────────────────────────────────

class TestPSR4Mappings:
    def test_psr4_mappings_extracted(self, composer_info: ComposerInfo):
        assert len(composer_info.psr4_mappings) >= 1

    def test_app_namespace_mapped(self, composer_info: ComposerInfo):
        namespaces = {m.namespace for m in composer_info.psr4_mappings}
        assert "App\\" in namespaces

    def test_app_path_mapped(self, composer_info: ComposerInfo):
        app_mapping = next(m for m in composer_info.psr4_mappings if m.namespace == "App\\")
        assert app_mapping.path == "app/"

    def test_psr4_mapping_type(self, composer_info: ComposerInfo):
        for mapping in composer_info.psr4_mappings:
            assert isinstance(mapping, PSR4Mapping)

    def test_dev_mappings_empty_for_tiny_app(self, composer_info: ComposerInfo):
        # The tiny app fixture has no autoload-dev
        assert composer_info.psr4_dev_mappings == []


# ── Class map building ────────────────────────────────────────────────────────

class TestBuildClassMap:
    def test_build_class_map_returns_dict(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        assert isinstance(class_map, dict)

    def test_class_map_contains_user_model(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        assert "App\\Models\\User" in class_map

    def test_class_map_contains_post_model(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        assert "App\\Models\\Post" in class_map

    def test_class_map_contains_user_controller(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        assert "App\\Http\\Controllers\\UserController" in class_map

    def test_class_map_file_paths_exist(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        for fqn, path in class_map.items():
            assert path.exists(), f"Expected file for {fqn} at {path}"

    def test_class_map_maps_to_php_files(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        for fqn, path in class_map.items():
            assert path.suffix == ".php", f"{fqn} maps to non-PHP file: {path}"

    def test_class_map_all_app_classes_found(self, composer_info: ComposerInfo):
        class_map = build_class_map(TINY_APP, composer_info.psr4_mappings)
        expected_fqns = [
            "App\\Models\\User",
            "App\\Models\\Post",
            "App\\Models\\Profile",
            "App\\Models\\Tag",
            "App\\Http\\Controllers\\UserController",
            "App\\Http\\Controllers\\PostController",
            "App\\Http\\Middleware\\AuthenticateApi",
            "App\\Services\\UserService",
            "App\\Events\\UserRegistered",
            "App\\Listeners\\SendWelcomeEmail",
        ]
        missing = [fqn for fqn in expected_fqns if fqn not in class_map]
        assert missing == [], f"Missing FQNs in class map: {missing}"

    def test_build_class_map_nonexistent_dir(self, tmp_path: Path):
        """build_class_map should handle missing directories gracefully."""
        mapping = PSR4Mapping(namespace="App\\", path="app/")
        class_map = build_class_map(tmp_path / "nonexistent", [mapping])
        assert class_map == {}


# ── Missing / malformed files ─────────────────────────────────────────────────

class TestComposerMissing:
    def test_missing_file_returns_errors(self):
        result = parse_composer(Path("/nonexistent/composer.json"))
        assert len(result.errors) >= 1
        assert "not found" in result.errors[0].lower() or "not found" in result.errors[0]

    def test_missing_file_empty_mappings(self):
        result = parse_composer(Path("/nonexistent/composer.json"))
        assert result.psr4_mappings == []

    def test_missing_file_unknown_version(self):
        result = parse_composer(Path("/nonexistent/composer.json"))
        assert result.laravel_version == "unknown"

    def test_malformed_json_returns_errors(self, tmp_path: Path):
        bad_file = tmp_path / "composer.json"
        bad_file.write_text("{ this is not valid json }")
        result = parse_composer(bad_file)
        assert len(result.errors) >= 1

    def test_malformed_json_empty_mappings(self, tmp_path: Path):
        bad_file = tmp_path / "composer.json"
        bad_file.write_text("{invalid}")
        result = parse_composer(bad_file)
        assert result.psr4_mappings == []


# ── Packages extraction ───────────────────────────────────────────────────────

class TestPackagesExtraction:
    def test_packages_extracted(self, composer_info: ComposerInfo):
        assert "laravel/framework" in composer_info.packages
        assert "php" in composer_info.packages

    def test_dev_packages_empty(self, composer_info: ComposerInfo):
        assert composer_info.dev_packages == {}

    def test_full_composer_with_dev_deps(self, tmp_path: Path):
        import json
        data = {
            "name": "test/app",
            "require": {"laravel/framework": "^11.0"},
            "require-dev": {
                "phpunit/phpunit": "^11.0",
                "fakerphp/faker": "^1.23",
            },
            "autoload": {"psr-4": {"App\\": "app/"}},
        }
        composer_file = tmp_path / "composer.json"
        composer_file.write_text(json.dumps(data))
        result = parse_composer(composer_file)
        assert "phpunit/phpunit" in result.dev_packages
        assert "fakerphp/faker" in result.dev_packages
