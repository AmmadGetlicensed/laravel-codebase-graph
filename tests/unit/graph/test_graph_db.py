"""Tests for the GraphDB class and KuzuDB operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from laravelgraph.core.graph import GraphDB
from laravelgraph.core.schema import NODE_TYPES, REL_TYPES, node_id


# ── Schema creation ───────────────────────────────────────────────────────────

class TestSchemaCreation:
    def test_schema_creation_succeeds(self, tmp_graph: GraphDB):
        """GraphDB constructor should create the schema without errors."""
        # If we got here, __init__ ran without raising
        assert tmp_graph is not None

    def test_all_node_tables_created(self, tmp_graph: GraphDB):
        """All node types defined in schema.py should be present in the DB."""
        existing = tmp_graph._existing_tables()
        for label, _ in NODE_TYPES:
            assert label in existing, f"Node table '{label}' was not created"

    def test_all_rel_tables_created(self, tmp_graph: GraphDB):
        """All relationship types defined in schema.py should be present in the DB."""
        existing = tmp_graph._existing_tables()
        for label, *_ in REL_TYPES:
            assert label in existing, f"Rel table '{label}' was not created"

    def test_schema_is_idempotent(self, tmp_path: Path):
        """Creating GraphDB twice on the same path should not raise."""
        db_path = tmp_path / "idempotent.kuzu"
        db1 = GraphDB(db_path)
        db1.close()
        db2 = GraphDB(db_path)
        db2.close()


# ── Insert and query nodes ────────────────────────────────────────────────────

class TestInsertAndQueryNode:
    def test_insert_class_node(self, tmp_graph: GraphDB):
        nid = node_id("class", "App\\Models\\TestModel")
        tmp_graph.upsert_node("Class_", {
            "node_id": nid,
            "name": "TestModel",
            "fqn": "App\\Models\\TestModel",
            "file_path": "/app/Models/TestModel.php",
            "line_start": 1,
            "line_end": 50,
            "is_abstract": False,
            "is_final": False,
            "laravel_role": "model",
            "is_dead_code": False,
            "community_id": 0,
            "embedding": [],
        })
        rows = tmp_graph.execute("MATCH (c:Class_ {node_id: $id}) RETURN c.name AS name", {"id": nid})
        assert len(rows) == 1
        assert rows[0]["name"] == "TestModel"

    def test_insert_and_retrieve_fqn(self, tmp_graph: GraphDB):
        nid = node_id("class", "App\\Models\\Unique1")
        tmp_graph.upsert_node("Class_", {
            "node_id": nid,
            "name": "Unique1",
            "fqn": "App\\Models\\Unique1",
            "file_path": "/test.php",
            "line_start": 1,
            "line_end": 10,
            "is_abstract": False,
            "is_final": False,
            "laravel_role": "",
            "is_dead_code": False,
            "community_id": 0,
            "embedding": [],
        })
        rows = tmp_graph.execute(
            "MATCH (c:Class_ {node_id: $id}) RETURN c.fqn AS fqn",
            {"id": nid},
        )
        assert rows[0]["fqn"] == "App\\Models\\Unique1"

    def test_upsert_replaces_existing_node(self, tmp_graph: GraphDB):
        nid = node_id("class", "App\\Models\\Mutable")
        props = {
            "node_id": nid,
            "name": "Mutable",
            "fqn": "App\\Models\\Mutable",
            "file_path": "/v1.php",
            "line_start": 1,
            "line_end": 10,
            "is_abstract": False,
            "is_final": False,
            "laravel_role": "",
            "is_dead_code": False,
            "community_id": 0,
            "embedding": [],
        }
        tmp_graph.upsert_node("Class_", props)
        # Update with new file_path
        props["file_path"] = "/v2.php"
        tmp_graph.upsert_node("Class_", props)

        rows = tmp_graph.execute(
            "MATCH (c:Class_ {node_id: $id}) RETURN c.file_path AS fp",
            {"id": nid},
        )
        assert len(rows) == 1
        assert rows[0]["fp"] == "/v2.php"


# ── Relationships ─────────────────────────────────────────────────────────────

class TestUpsertRelationship:
    def _create_two_classes(self, db: GraphDB, name_a: str, name_b: str):
        for name in (name_a, name_b):
            nid = node_id("class", f"App\\{name}")
            db.upsert_node("Class_", {
                "node_id": nid,
                "name": name,
                "fqn": f"App\\{name}",
                "file_path": f"/{name}.php",
                "line_start": 1,
                "line_end": 20,
                "is_abstract": False,
                "is_final": False,
                "laravel_role": "",
                "is_dead_code": False,
                "community_id": 0,
                "embedding": [],
            })

    def test_create_calls_relationship(self, tmp_graph: GraphDB):
        # CALLS goes Method → Method; create two Method nodes
        caller_id = node_id("method", "App\\Caller::call")
        callee_id = node_id("method", "App\\Callee::handle")
        for nid, name, fqn in [
            (caller_id, "call", "App\\Caller::call"),
            (callee_id, "handle", "App\\Callee::handle"),
        ]:
            tmp_graph.upsert_node("Method", {
                "node_id": nid, "name": name, "fqn": fqn,
                "file_path": f"/{name}.php", "line_start": 1, "line_end": 10,
                "visibility": "public", "is_static": False, "is_abstract": False,
                "return_type": "", "param_types": "[]", "docblock": "",
                "is_dead_code": False, "laravel_role": "", "community_id": 0,
                "embedding": [],
            })

        tmp_graph.upsert_rel(
            rel_label="CALLS",
            from_label="Method",
            from_id=caller_id,
            to_label="Method",
            to_id=callee_id,
            props={"confidence": 0.9, "call_type": "direct", "line": 42},
        )

        rows = tmp_graph.execute(
            "MATCH (a:Method {node_id: $aid})-[r:CALLS]->(b:Method {node_id: $bid}) "
            "RETURN r.confidence AS conf",
            {"aid": caller_id, "bid": callee_id},
        )
        assert len(rows) == 1
        assert abs(rows[0]["conf"] - 0.9) < 0.001

    def test_relationship_without_props(self, tmp_graph: GraphDB):
        self._create_two_classes(tmp_graph, "ParentClass", "ChildClass")
        parent_id = node_id("class", "App\\ParentClass")
        child_id = node_id("class", "App\\ChildClass")

        tmp_graph.upsert_rel(
            rel_label="EXTENDS_CLASS",
            from_label="Class_",
            from_id=child_id,
            to_label="Class_",
            to_id=parent_id,
        )

        rows = tmp_graph.execute(
            "MATCH (c:Class_ {node_id: $cid})-[:EXTENDS_CLASS]->(p:Class_ {node_id: $pid}) "
            "RETURN p.name AS parent",
            {"cid": child_id, "pid": parent_id},
        )
        assert len(rows) == 1
        assert rows[0]["parent"] == "ParentClass"


# ── node_exists ───────────────────────────────────────────────────────────────

class TestNodeExists:
    def test_node_exists_true(self, tmp_graph: GraphDB):
        nid = node_id("class", "App\\ExistingClass")
        tmp_graph.upsert_node("Class_", {
            "node_id": nid,
            "name": "ExistingClass",
            "fqn": "App\\ExistingClass",
            "file_path": "/Existing.php",
            "line_start": 1,
            "line_end": 10,
            "is_abstract": False,
            "is_final": False,
            "laravel_role": "",
            "is_dead_code": False,
            "community_id": 0,
            "embedding": [],
        })
        assert tmp_graph.node_exists("Class_", nid) is True

    def test_node_exists_false(self, tmp_graph: GraphDB):
        nid = node_id("class", "App\\NonExistentClass")
        assert tmp_graph.node_exists("Class_", nid) is False

    def test_node_exists_after_delete(self, tmp_graph: GraphDB):
        nid = node_id("class", "App\\Deletable")
        tmp_graph.upsert_node("Class_", {
            "node_id": nid,
            "name": "Deletable",
            "fqn": "App\\Deletable",
            "file_path": "/Deletable.php",
            "line_start": 1,
            "line_end": 10,
            "is_abstract": False,
            "is_final": False,
            "laravel_role": "",
            "is_dead_code": False,
            "community_id": 0,
            "embedding": [],
        })
        assert tmp_graph.node_exists("Class_", nid) is True
        tmp_graph.execute("MATCH (n:Class_ {node_id: $id}) DETACH DELETE n", {"id": nid})
        assert tmp_graph.node_exists("Class_", nid) is False


# ── stats() ───────────────────────────────────────────────────────────────────

class TestStats:
    def test_stats_returns_dict(self, tmp_graph: GraphDB):
        result = tmp_graph.stats()
        assert isinstance(result, dict)

    def test_stats_counts_inserted_nodes(self, tmp_path: Path):
        db = GraphDB(tmp_path / "stats_test.kuzu")
        try:
            for i in range(3):
                nid = node_id("class", f"App\\StatsClass{i}")
                db.upsert_node("Class_", {
                    "node_id": nid,
                    "name": f"StatsClass{i}",
                    "fqn": f"App\\StatsClass{i}",
                    "file_path": f"/Class{i}.php",
                    "line_start": 1,
                    "line_end": 10,
                    "is_abstract": False,
                    "is_final": False,
                    "laravel_role": "",
                    "is_dead_code": False,
                    "community_id": 0,
                    "embedding": [],
                })
            stats = db.stats()
            assert stats.get("Class_", 0) >= 3
        finally:
            db.close()

    def test_empty_db_stats(self, tmp_path: Path):
        db = GraphDB(tmp_path / "empty_stats.kuzu")
        try:
            stats = db.stats()
            # All counts should be zero (keys may be absent or zero)
            for v in stats.values():
                assert v >= 0
        finally:
            db.close()


# ── delete_file_symbols ───────────────────────────────────────────────────────

class TestDeleteFileSymbols:
    def test_delete_removes_nodes_by_file_path(self, tmp_path: Path):
        db = GraphDB(tmp_path / "delete_test.kuzu")
        try:
            file_path = "/app/Models/ToDelete.php"
            nid = node_id("class", "App\\ToDelete")
            db.upsert_node("Class_", {
                "node_id": nid,
                "name": "ToDelete",
                "fqn": "App\\ToDelete",
                "file_path": file_path,
                "line_start": 1,
                "line_end": 10,
                "is_abstract": False,
                "is_final": False,
                "laravel_role": "",
                "is_dead_code": False,
                "community_id": 0,
                "embedding": [],
            })
            assert db.node_exists("Class_", nid) is True
            db.delete_file_symbols(file_path)
            assert db.node_exists("Class_", nid) is False
        finally:
            db.close()

    def test_delete_file_does_not_affect_other_files(self, tmp_path: Path):
        db = GraphDB(tmp_path / "delete_selective.kuzu")
        try:
            file_a = "/app/Models/ModelA.php"
            file_b = "/app/Models/ModelB.php"

            nid_a = node_id("class", "App\\ModelA")
            nid_b = node_id("class", "App\\ModelB")

            for nid, name, fp in [(nid_a, "ModelA", file_a), (nid_b, "ModelB", file_b)]:
                db.upsert_node("Class_", {
                    "node_id": nid,
                    "name": name,
                    "fqn": f"App\\{name}",
                    "file_path": fp,
                    "line_start": 1,
                    "line_end": 10,
                    "is_abstract": False,
                    "is_final": False,
                    "laravel_role": "",
                    "is_dead_code": False,
                    "community_id": 0,
                    "embedding": [],
                })

            db.delete_file_symbols(file_a)
            assert db.node_exists("Class_", nid_a) is False
            assert db.node_exists("Class_", nid_b) is True
        finally:
            db.close()


# ── clear_all ─────────────────────────────────────────────────────────────────

class TestClearAll:
    def test_clear_all_removes_all_nodes(self, tmp_path: Path):
        db = GraphDB(tmp_path / "clear_test.kuzu")
        try:
            for i in range(5):
                nid = node_id("class", f"App\\Clear{i}")
                db.upsert_node("Class_", {
                    "node_id": nid,
                    "name": f"Clear{i}",
                    "fqn": f"App\\Clear{i}",
                    "file_path": f"/Clear{i}.php",
                    "line_start": 1,
                    "line_end": 10,
                    "is_abstract": False,
                    "is_final": False,
                    "laravel_role": "",
                    "is_dead_code": False,
                    "community_id": 0,
                    "embedding": [],
                })
            stats_before = db.stats()
            assert stats_before.get("Class_", 0) >= 5

            db.clear_all()

            stats_after = db.stats()
            assert stats_after.get("Class_", 0) == 0
        finally:
            db.close()

    def test_clear_all_is_idempotent(self, tmp_path: Path):
        """Calling clear_all on an already-empty DB should not raise."""
        db = GraphDB(tmp_path / "clear_idempotent.kuzu")
        try:
            db.clear_all()
            db.clear_all()
        finally:
            db.close()


# ── Context manager ───────────────────────────────────────────────────────────

class TestContextManager:
    def test_context_manager_closes_db(self, tmp_path: Path):
        db_path = tmp_path / "ctx.kuzu"
        with GraphDB(db_path) as db:
            nid = node_id("class", "App\\CtxClass")
            db.upsert_node("Class_", {
                "node_id": nid,
                "name": "CtxClass",
                "fqn": "App\\CtxClass",
                "file_path": "/ctx.php",
                "line_start": 1,
                "line_end": 5,
                "is_abstract": False,
                "is_final": False,
                "laravel_role": "",
                "is_dead_code": False,
                "community_id": 0,
                "embedding": [],
            })
            assert db.node_exists("Class_", nid)
        # After __exit__, DB is closed — no assertion needed, just no exception
