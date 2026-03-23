"""Tests for the graph schema definitions."""

from __future__ import annotations

import pytest

from laravelgraph.core.schema import NODE_TYPES, REL_TYPES, node_id


# ── node_id() ─────────────────────────────────────────────────────────────────

class TestNodeIdFormat:
    def test_simple_node_id(self):
        result = node_id("class", "App\\Models\\User")
        assert result == "class:App\\Models\\User"

    def test_method_node_id_two_parts(self):
        result = node_id("method", "App\\Http\\Controllers\\UserController", "store")
        assert result == "method:App\\Http\\Controllers\\UserController::store"

    def test_route_node_id(self):
        result = node_id("route", "api.users.index")
        assert result == "route:api.users.index"

    def test_node_id_label_prefix(self):
        result = node_id("file", "/app/Models/User.php")
        assert result.startswith("file:")

    def test_node_id_double_backslash_normalized(self):
        """Double backslashes from string escaping should be normalized to single."""
        result = node_id("class", "App\\\\Models\\\\User")
        assert "\\\\" not in result

    def test_node_id_three_parts(self):
        result = node_id("property", "App\\Models\\User", "email")
        assert result == "property:App\\Models\\User::email"

    def test_node_id_single_part(self):
        result = node_id("folder", "/app")
        assert result == "folder:/app"

    def test_node_id_returns_string(self):
        result = node_id("method", "SomeClass", "someMethod")
        assert isinstance(result, str)


# ── NODE_TYPES structure ──────────────────────────────────────────────────────

class TestAllNodeTypesHaveNodeId:
    def test_all_node_types_have_node_id_as_first_property(self):
        for label, props in NODE_TYPES:
            assert len(props) >= 1, f"{label} has no properties"
            first_prop_name = props[0][0]
            assert first_prop_name == "node_id", (
                f"{label}: first property is '{first_prop_name}', expected 'node_id'"
            )

    def test_all_node_types_have_node_id_as_string(self):
        for label, props in NODE_TYPES:
            first_prop_type = props[0][1]
            assert first_prop_type == "STRING", (
                f"{label}: node_id type is '{first_prop_type}', expected 'STRING'"
            )

    def test_no_empty_node_type_labels(self):
        for label, _ in NODE_TYPES:
            assert label, "Empty node type label found"

    def test_no_empty_property_names(self):
        for label, props in NODE_TYPES:
            for prop_name, prop_type in props:
                assert prop_name, f"{label} has a property with empty name"
                assert prop_type, f"{label}.{prop_name} has empty type"


# ── NODE_TYPES content checks ─────────────────────────────────────────────────

class TestNodeTypesCoverage:
    def _labels(self) -> set[str]:
        return {label for label, _ in NODE_TYPES}

    def test_core_php_types_present(self):
        labels = self._labels()
        assert "Class_" in labels
        assert "Method" in labels
        assert "Interface_" in labels
        assert "Trait_" in labels
        assert "Enum_" in labels

    def test_laravel_specific_types_present(self):
        labels = self._labels()
        assert "EloquentModel" in labels
        assert "Controller" in labels
        assert "Middleware" in labels
        assert "ServiceProvider" in labels
        assert "Event" in labels
        assert "Listener" in labels

    def test_route_type_present(self):
        assert "Route" in self._labels()

    def test_blade_types_present(self):
        labels = self._labels()
        assert "BladeTemplate" in labels
        assert "BladeComponent" in labels

    def test_database_types_present(self):
        labels = self._labels()
        assert "DatabaseTable" in labels
        assert "DatabaseColumn" in labels
        assert "Migration" in labels

    def test_analysis_types_present(self):
        labels = self._labels()
        assert "Community" in labels
        assert "Process" in labels

    def test_minimum_node_type_count(self):
        assert len(NODE_TYPES) > 20, f"Expected >20 node types, got {len(NODE_TYPES)}"

    def test_node_types_is_list_of_tuples(self):
        assert isinstance(NODE_TYPES, list)
        for item in NODE_TYPES:
            assert isinstance(item, tuple)
            assert len(item) == 2

    def test_node_type_props_are_list_of_tuples(self):
        for label, props in NODE_TYPES:
            assert isinstance(props, list), f"{label} props is not a list"
            for prop in props:
                assert isinstance(prop, tuple), f"{label} has non-tuple prop: {prop}"
                assert len(prop) == 2


# ── REL_TYPES structure ───────────────────────────────────────────────────────

class TestRelTypesAreUnique:
    def test_no_duplicate_rel_type_names(self):
        seen = set()
        duplicates = []
        for label, _ in REL_TYPES:
            if label in seen:
                duplicates.append(label)
            seen.add(label)
        assert duplicates == [], f"Duplicate relationship type names: {duplicates}"

    def test_no_empty_rel_type_labels(self):
        for label, _ in REL_TYPES:
            assert label, "Empty rel type label found"

    def test_minimum_rel_type_count(self):
        assert len(REL_TYPES) > 15, f"Expected >15 rel types, got {len(REL_TYPES)}"

    def test_rel_types_is_list_of_tuples(self):
        assert isinstance(REL_TYPES, list)
        for item in REL_TYPES:
            assert isinstance(item, tuple)
            assert len(item) == 2


class TestRelTypesCoverage:
    def _labels(self) -> set[str]:
        return {label for label, _ in REL_TYPES}

    def test_core_code_relationships(self):
        labels = self._labels()
        assert "CALLS" in labels
        assert "EXTENDS_CLASS" in labels
        assert "IMPLEMENTS_INTERFACE" in labels
        assert "USES_TRAIT" in labels

    def test_laravel_relationships(self):
        labels = self._labels()
        assert "HAS_RELATIONSHIP" in labels
        assert "ROUTES_TO" in labels
        assert "DISPATCHES" in labels
        assert "LISTENS_TO" in labels

    def test_blade_relationships(self):
        labels = self._labels()
        assert "EXTENDS_TEMPLATE" in labels
        assert "INCLUDES_TEMPLATE" in labels
        assert "HAS_COMPONENT" in labels

    def test_database_relationships(self):
        labels = self._labels()
        assert "HAS_COLUMN" in labels
        assert "MIGRATES_TABLE" in labels


# ── Node type property validation ─────────────────────────────────────────────

class TestNodeTypePropertyValidation:
    def test_class_node_has_required_properties(self):
        schema = dict(NODE_TYPES)
        class_props = {name for name, _ in schema["Class_"]}
        required = {"node_id", "name", "fqn", "file_path", "line_start", "is_abstract", "is_final"}
        missing = required - class_props
        assert missing == set(), f"Class_ missing properties: {missing}"

    def test_method_node_has_required_properties(self):
        schema = dict(NODE_TYPES)
        method_props = {name for name, _ in schema["Method"]}
        required = {"node_id", "name", "fqn", "file_path", "visibility", "is_static"}
        missing = required - method_props
        assert missing == set(), f"Method missing properties: {missing}"

    def test_route_node_has_http_properties(self):
        schema = dict(NODE_TYPES)
        route_props = {name for name, _ in schema["Route"]}
        required = {"node_id", "http_method", "uri", "controller_fqn", "is_api"}
        missing = required - route_props
        assert missing == set(), f"Route missing properties: {missing}"

    def test_eloquent_model_has_orm_properties(self):
        schema = dict(NODE_TYPES)
        model_props = {name for name, _ in schema["EloquentModel"]}
        required = {"node_id", "name", "fqn", "fillable", "soft_deletes"}
        missing = required - model_props
        assert missing == set(), f"EloquentModel missing properties: {missing}"


# ── Kuzu type validation ──────────────────────────────────────────────────────

class TestKuzuTypes:
    VALID_TYPES = {
        "STRING", "INT32", "INT64", "FLOAT", "DOUBLE", "BOOLEAN",
        "FLOAT[]", "STRING[]", "INT32[]",
    }

    def test_all_property_types_are_valid_kuzu_types(self):
        invalid = []
        for label, props in NODE_TYPES:
            for prop_name, prop_type in props:
                if prop_type not in self.VALID_TYPES:
                    invalid.append(f"{label}.{prop_name}: {prop_type}")
        assert invalid == [], f"Invalid Kuzu types found:\n" + "\n".join(invalid)

    def test_node_id_is_always_string_type(self):
        for label, props in NODE_TYPES:
            pk_type = props[0][1]
            assert pk_type == "STRING", f"{label}: PK type is {pk_type!r}, expected STRING"
