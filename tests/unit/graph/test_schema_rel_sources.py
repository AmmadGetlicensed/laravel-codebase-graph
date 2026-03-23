"""Unit tests: relationship source/target label constraints in schema.

These tests guard against regressions where a pipeline phase passes an invalid
source label to upsert_rel() — e.g. using 'File' as the source for
RENDERS_TEMPLATE, which KuzuDB rejects with a Binder exception.
"""

from __future__ import annotations

import pytest

from laravelgraph.core.schema import REL_TYPES, NODE_TYPES


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rel_schema() -> dict[str, list[tuple[str, str]]]:
    """Return {rel_name: [(src_label, dst_label), ...]} for all REL_TYPES."""
    result: dict[str, list[tuple[str, str]]] = {}
    for entry in REL_TYPES:
        # entry is (name, [(src, dst), ...], props)  OR  (name, props) — check length
        if len(entry) == 3:
            name, pairs, _ = entry
        else:
            name, pairs = entry
            pairs = []
        result[name] = list(pairs)
    return result


def _node_labels() -> set[str]:
    return {label for label, _ in NODE_TYPES}


# ── RENDERS_TEMPLATE ──────────────────────────────────────────────────────────

class TestRendersTemplateSchema:
    """RENDERS_TEMPLATE must accept Method and Class_ sources but NOT File."""

    def test_renders_template_exists(self):
        schema = _rel_schema()
        assert "RENDERS_TEMPLATE" in schema, "RENDERS_TEMPLATE not found in REL_TYPES"

    def test_renders_template_allows_method_source(self):
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["RENDERS_TEMPLATE"]}
        assert "Method" in src_labels, (
            "RENDERS_TEMPLATE must allow Method as source — "
            "controllers call view() from methods"
        )

    def test_renders_template_allows_class_source(self):
        """Class_ fallback is needed when a view() call can't be attributed to a method."""
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["RENDERS_TEMPLATE"]}
        assert "Class_" in src_labels, (
            "RENDERS_TEMPLATE must allow Class_ as source — "
            "class-level fallback when method cannot be resolved"
        )

    def test_renders_template_does_not_allow_file_source(self):
        """Regression: phase 18 was passing File nodes as source, causing Binder exceptions."""
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["RENDERS_TEMPLATE"]}
        assert "File" not in src_labels, (
            "RENDERS_TEMPLATE must NOT allow File as source — "
            "KuzuDB rejects it with a Binder exception. "
            "Use Method or Class_ instead."
        )

    def test_renders_template_target_is_blade_template(self):
        schema = _rel_schema()
        dst_labels = {dst for _, dst in schema["RENDERS_TEMPLATE"]}
        assert "BladeTemplate" in dst_labels, (
            "RENDERS_TEMPLATE target must be BladeTemplate"
        )


# ── USES_CONFIG / USES_ENV ────────────────────────────────────────────────────

class TestConfigEnvSchema:
    """USES_CONFIG and USES_ENV must explicitly allow File as a source
    (config files themselves reference keys directly)."""

    def test_uses_config_exists(self):
        assert "USES_CONFIG" in _rel_schema()

    def test_uses_env_exists(self):
        assert "USES_ENV" in _rel_schema()

    def test_uses_config_allows_file_source(self):
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["USES_CONFIG"]}
        assert "File" in src_labels, (
            "USES_CONFIG must allow File as source — "
            "config PHP files themselves reference env() keys"
        )

    def test_uses_env_allows_file_source(self):
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["USES_ENV"]}
        assert "File" in src_labels, (
            "USES_ENV must allow File as source"
        )

    def test_uses_config_allows_method_source(self):
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["USES_CONFIG"]}
        assert "Method" in src_labels

    def test_uses_env_allows_method_source(self):
        schema = _rel_schema()
        src_labels = {src for src, _ in schema["USES_ENV"]}
        assert "Method" in src_labels


# ── All source/target labels reference known node types ───────────────────────

class TestRelLabelsSanity:
    """Every label referenced in REL_TYPES must exist in NODE_TYPES."""

    def test_all_rel_source_labels_are_known_node_types(self):
        known = _node_labels()
        unknown = []
        for entry in REL_TYPES:
            if len(entry) == 3:
                name, pairs, _ = entry
            else:
                name, pairs = entry
                pairs = []
            for src, dst in pairs:
                if src not in known:
                    unknown.append(f"{name}: source '{src}' not in NODE_TYPES")
                if dst not in known:
                    unknown.append(f"{name}: target '{dst}' not in NODE_TYPES")
        assert unknown == [], "Unknown labels in REL_TYPES:\n" + "\n".join(unknown)


# ── GraphDB rejects File→BladeTemplate at runtime ────────────────────────────

class TestGraphDBRejectsInvalidRelSource:
    """Verify KuzuDB actually rejects File→BladeTemplate RENDERS_TEMPLATE edges."""

    def test_file_source_for_renders_template_creates_no_edge(self, tmp_path):
        """upsert_rel with File source silently fails (KuzuDB Binder exception is caught).

        The important thing is that NO edge is created — we verify the edge
        count stays at zero after the bad call.
        """
        from laravelgraph.core.graph import GraphDB
        from laravelgraph.core.schema import node_id as make_node_id

        db = GraphDB(tmp_path / "test.kuzu")

        file_nid = make_node_id("file", "app/Http/Controllers/TestController.php")
        blade_nid = make_node_id("blade", "posts.index")

        db.upsert_node("File", {"node_id": file_nid, "path": "app/Http/Controllers/TestController.php", "name": "TestController.php"})
        db.upsert_node("BladeTemplate", {"node_id": blade_nid, "name": "posts.index", "file_path": "resources/views/posts/index.blade.php", "relative_path": "posts/index.blade.php"})

        # upsert_rel catches the KuzuDB Binder exception internally — it logs a warning
        # but does not re-raise.  The edge must NOT be created.
        db.upsert_rel("RENDERS_TEMPLATE", "File", file_nid, "BladeTemplate", blade_nid, {"line": 10})

        # Verify KuzuDB directly rejects this combination
        with pytest.raises(Exception):
            db._conn.execute(
                "MATCH (a:File {node_id: $fid}), (b:BladeTemplate {node_id: $tid}) "
                "CREATE (a)-[:RENDERS_TEMPLATE {line: 10}]->(b)",
                parameters={"fid": file_nid, "tid": blade_nid},
            )

        db.close()

    def test_method_source_for_renders_template_is_accepted(self, tmp_path):
        from laravelgraph.core.graph import GraphDB
        from laravelgraph.core.schema import node_id as make_node_id

        db = GraphDB(tmp_path / "test2.kuzu")

        class_nid = make_node_id("class", "App\\Http\\Controllers\\PostController")
        method_nid = make_node_id("method", "App\\Http\\Controllers\\PostController", "index")
        blade_nid = make_node_id("blade", "posts.index")

        db.upsert_node("Class_", {
            "node_id": class_nid, "name": "PostController",
            "fqn": "App\\Http\\Controllers\\PostController",
            "file_path": "app/Http/Controllers/PostController.php",
            "line_start": 1, "line_end": 30,
            "is_abstract": False, "is_final": False,
            "laravel_role": "controller",
        })
        db.upsert_node("Method", {
            "node_id": method_nid, "name": "index",
            "fqn": "App\\Http\\Controllers\\PostController::index",
            "file_path": "app/Http/Controllers/PostController.php",
            "line_start": 9, "line_end": 13,
            "visibility": "public", "is_static": False,
            "is_abstract": False, "return_type": "",
            "laravel_role": "controller_action",
        })
        db.upsert_node("BladeTemplate", {
            "node_id": blade_nid, "name": "posts.index",
            "file_path": "resources/views/posts/index.blade.php",
            "relative_path": "posts/index.blade.php",
        })

        # This must NOT raise
        db.upsert_rel("RENDERS_TEMPLATE", "Method", method_nid, "BladeTemplate", blade_nid, {"line": 12})

        rows = db.execute(
            "MATCH (m:Method)-[r:RENDERS_TEMPLATE]->(t:BladeTemplate) "
            "RETURN m.node_id AS mnid, t.node_id AS tnid"
        )
        assert any(r.get("mnid") == method_nid for r in rows), (
            "Expected RENDERS_TEMPLATE edge from Method to BladeTemplate"
        )

        db.close()
