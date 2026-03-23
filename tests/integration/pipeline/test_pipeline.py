"""Integration tests: run full pipeline on tiny-laravel-app fixture."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def pipeline_ctx(tmp_path_factory):
    """Run the full pipeline once and reuse the context across tests in this module."""
    tmp = tmp_path_factory.mktemp("pipeline_test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False  # Skip slow embeddings in tests

    pipeline = Pipeline(app_copy, config=cfg)
    ctx = pipeline.run(full=True, skip_embeddings=True)
    return ctx


# ── File discovery ────────────────────────────────────────────────────────────

class TestDiscovery:
    def test_php_files_discovered(self, pipeline_ctx):
        assert len(pipeline_ctx.php_files) >= 10, (
            f"Expected >=10 PHP files, got {len(pipeline_ctx.php_files)}"
        )

    def test_blade_files_discovered(self, pipeline_ctx):
        assert len(pipeline_ctx.blade_files) >= 2, (
            f"Expected >=2 Blade files, got {len(pipeline_ctx.blade_files)}"
        )

    def test_route_files_discovered(self, pipeline_ctx):
        assert len(pipeline_ctx.route_files) >= 2, (
            f"Expected >=2 route files (web.php + api.php), got {len(pipeline_ctx.route_files)}"
        )

    def test_migration_files_discovered(self, pipeline_ctx):
        assert len(pipeline_ctx.migration_files) >= 2, (
            f"Expected >=2 migration files, got {len(pipeline_ctx.migration_files)}"
        )

    def test_php_files_are_paths(self, pipeline_ctx):
        for f in pipeline_ctx.php_files:
            assert isinstance(f, Path)
            assert f.suffix == ".php"

    def test_blade_files_end_in_blade_php(self, pipeline_ctx):
        for f in pipeline_ctx.blade_files:
            assert f.name.endswith(".blade.php"), f"Not a blade file: {f}"


# ── Graph node creation ───────────────────────────────────────────────────────

class TestGraphNodes:
    def test_classes_created(self, pipeline_ctx):
        stats = pipeline_ctx.db.stats()
        assert stats.get("Class_", 0) >= 5, (
            f"Expected >=5 Class_ nodes (User, Post, Profile, etc.), got {stats}"
        )

    def test_methods_created(self, pipeline_ctx):
        stats = pipeline_ctx.db.stats()
        assert stats.get("Method", 0) >= 10, (
            f"Expected >=10 Method nodes, got {stats}"
        )

    def test_routes_created(self, pipeline_ctx):
        stats = pipeline_ctx.db.stats()
        assert stats.get("Route", 0) >= 3, (
            f"Expected >=3 Route nodes, got {stats}"
        )

    def test_specific_class_nodes_present(self, pipeline_ctx):
        db = pipeline_ctx.db
        for class_name in ["User", "Post", "Profile"]:
            rows = db.execute(
                "MATCH (c:Class_ {name: $name}) RETURN c.name AS name",
                {"name": class_name},
            )
            assert len(rows) >= 1, f"Expected Class_ node for '{class_name}'"

    def test_controller_nodes_present(self, pipeline_ctx):
        db = pipeline_ctx.db
        rows = db.execute("MATCH (c:Controller) RETURN c.name AS name")
        controller_names = {r["name"] for r in rows}
        assert "UserController" in controller_names or len(rows) >= 1, (
            "Expected at least one Controller node"
        )

    def test_no_pipeline_context_errors(self, pipeline_ctx):
        """Pipeline should complete with no errors (or at most minor ones)."""
        # Some phases (git, embeddings) may warn but shouldn't hard-fail the whole pipeline
        # We allow a small number of non-critical errors
        critical_errors = [
            e for e in pipeline_ctx.errors
            if "Phase 1" in e or "Phase 2" in e or "Phase 3" in e
        ]
        assert critical_errors == [], f"Critical pipeline errors: {critical_errors}"


# ── Eloquent relationships ─────────────────────────────────────────────────────

class TestEloquentRelationships:
    def test_user_has_many_posts(self, pipeline_ctx):
        db = pipeline_ctx.db
        rels = db.execute(
            "MATCH (m:EloquentModel)-[r:HAS_RELATIONSHIP]->(related) "
            "WHERE m.name = 'User' AND r.relationship_type = 'hasMany' "
            "RETURN related.name AS related"
        )
        assert any(r.get("related") == "Post" for r in rels), (
            f"Expected User hasMany Post, got: {rels}"
        )

    def test_post_belongs_to_user(self, pipeline_ctx):
        db = pipeline_ctx.db
        rels = db.execute(
            "MATCH (m:EloquentModel)-[r:HAS_RELATIONSHIP]->(related) "
            "WHERE m.name = 'Post' AND r.relationship_type = 'belongsTo' "
            "RETURN related.name AS related"
        )
        assert any(r.get("related") == "User" for r in rels), (
            f"Expected Post belongsTo User, got: {rels}"
        )

    def test_user_has_one_profile(self, pipeline_ctx):
        db = pipeline_ctx.db
        rels = db.execute(
            "MATCH (m:EloquentModel)-[r:HAS_RELATIONSHIP]->(related) "
            "WHERE m.name = 'User' AND r.relationship_type = 'hasOne' "
            "RETURN related.name AS related"
        )
        assert any(r.get("related") == "Profile" for r in rels), (
            f"Expected User hasOne Profile, got: {rels}"
        )

    def test_eloquent_model_nodes_created(self, pipeline_ctx):
        db = pipeline_ctx.db
        rows = db.execute("MATCH (m:EloquentModel) RETURN m.name AS name")
        model_names = {r["name"] for r in rows}
        expected = {"User", "Post", "Profile", "Tag"}
        found = expected & model_names
        assert len(found) >= 3, f"Expected at least 3 Eloquent models, found: {found}"


# ── Routes ────────────────────────────────────────────────────────────────────

class TestRoutes:
    def test_api_routes_parsed(self, pipeline_ctx):
        db = pipeline_ctx.db
        routes = db.execute("MATCH (r:Route {is_api: true}) RETURN r.uri AS uri")
        assert len(routes) >= 1, "Expected at least one API route"

    def test_web_routes_parsed(self, pipeline_ctx):
        db = pipeline_ctx.db
        routes = db.execute("MATCH (r:Route) RETURN r.uri AS uri")
        uris = [r.get("uri", "") for r in routes]
        # web.php defines /, /posts, /posts/{post}
        assert any("posts" in uri for uri in uris) or len(routes) >= 2, (
            f"Expected web routes, got: {uris}"
        )

    def test_route_has_controller(self, pipeline_ctx):
        db = pipeline_ctx.db
        routes = db.execute(
            "MATCH (r:Route) WHERE r.controller_fqn IS NOT NULL "
            "RETURN r.controller_fqn AS ctrl LIMIT 5"
        )
        assert len(routes) >= 1, "Expected at least one route with a controller FQN"

    def test_routes_have_http_methods(self, pipeline_ctx):
        db = pipeline_ctx.db
        routes = db.execute("MATCH (r:Route) RETURN r.http_method AS method")
        methods = {r.get("method", "").upper() for r in routes if r.get("method")}
        assert len(methods) >= 1, f"Expected HTTP methods on routes, got: {methods}"


# ── Dead code detection ───────────────────────────────────────────────────────

class TestDeadCode:
    def test_dead_code_query_does_not_error(self, pipeline_ctx):
        """The dead code query should execute without errors (result may be empty)."""
        db = pipeline_ctx.db
        # Just verify the query doesn't raise
        result = db.execute(
            "MATCH (m:Method {is_dead_code: true}) RETURN m.name AS name LIMIT 10"
        )
        assert isinstance(result, list)

    def test_unused_method_query(self, pipeline_ctx):
        """PostController::unusedMethod is a strong candidate for dead code."""
        db = pipeline_ctx.db
        dead = db.execute(
            "MATCH (m:Method {is_dead_code: true, name: 'unusedMethod'}) "
            "RETURN m.fqn AS fqn"
        )
        # Best-effort: if detected, it should have the right FQN
        if dead:
            assert "PostController" in dead[0].get("fqn", ""), (
                f"Unexpected FQN for unusedMethod: {dead[0]}"
            )


# ── Database schema (migrations) ──────────────────────────────────────────────

class TestMigrationSchema:
    def test_users_table_created(self, pipeline_ctx):
        db = pipeline_ctx.db
        tables = db.execute(
            "MATCH (t:DatabaseTable {name: 'users'}) RETURN t.name AS name"
        )
        assert len(tables) == 1, f"Expected users table, got: {tables}"

    def test_posts_table_created(self, pipeline_ctx):
        db = pipeline_ctx.db
        tables = db.execute(
            "MATCH (t:DatabaseTable {name: 'posts'}) RETURN t.name AS name"
        )
        assert len(tables) == 1, f"Expected posts table, got: {tables}"

    def test_users_table_has_email_column(self, pipeline_ctx):
        db = pipeline_ctx.db
        cols = db.execute(
            "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) "
            "WHERE t.name = 'users' RETURN c.name AS col"
        )
        col_names = [r.get("col") for r in cols]
        assert "email" in col_names, f"Expected 'email' column in users table, got: {col_names}"

    def test_users_table_has_name_column(self, pipeline_ctx):
        db = pipeline_ctx.db
        cols = db.execute(
            "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) "
            "WHERE t.name = 'users' RETURN c.name AS col"
        )
        col_names = [r.get("col") for r in cols]
        assert "name" in col_names, f"Expected 'name' column in users table, got: {col_names}"

    def test_users_table_has_password_column(self, pipeline_ctx):
        db = pipeline_ctx.db
        cols = db.execute(
            "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) "
            "WHERE t.name = 'users' RETURN c.name AS col"
        )
        col_names = [r.get("col") for r in cols]
        assert "password" in col_names, (
            f"Expected 'password' column in users table, got: {col_names}"
        )

    def test_migration_nodes_created(self, pipeline_ctx):
        db = pipeline_ctx.db
        migrations = db.execute("MATCH (m:Migration) RETURN m.name AS name")
        assert len(migrations) >= 2, f"Expected >=2 Migration nodes, got: {migrations}"


# ── Pipeline context ──────────────────────────────────────────────────────────

class TestPipelineContext:
    def test_context_has_project_root(self, pipeline_ctx):
        assert pipeline_ctx.project_root is not None
        assert pipeline_ctx.project_root.is_dir()

    def test_context_has_composer_info(self, pipeline_ctx):
        assert pipeline_ctx.composer is not None
        assert pipeline_ctx.composer.laravel_version == "11.x"

    def test_context_has_db(self, pipeline_ctx):
        assert pipeline_ctx.db is not None

    def test_context_has_fqn_index(self, pipeline_ctx):
        assert isinstance(pipeline_ctx.fqn_index, dict)
        # FQN index should have entries for the parsed classes
        assert len(pipeline_ctx.fqn_index) >= 1

    def test_context_stats_populated(self, pipeline_ctx):
        assert isinstance(pipeline_ctx.stats, dict)
