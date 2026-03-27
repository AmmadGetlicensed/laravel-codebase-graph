"""Integration tests for phase_05 DISPATCHES edge creation.

Verifies that after running the full pipeline on the tiny-laravel-app fixture,
the dispatch detection pass correctly creates DISPATCHES edges for:
  - UserService::create → UserRegistered  (from `event(new UserRegistered($user))`)

These tests run the pipeline once (scope="module") and query the resulting graph.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def pipeline_ctx(tmp_path_factory):
    """Run the full pipeline once and reuse context across tests in this module."""
    tmp = tmp_path_factory.mktemp("dispatch_test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False
    pipeline = Pipeline(app_copy, config=cfg)
    ctx = pipeline.run(full=True, skip_embeddings=True)
    return ctx


# ── DISPATCHES edges exist ────────────────────────────────────────────────────

class TestDispatchEdgesExist:
    """Verify that the pipeline creates at least one DISPATCHES edge."""

    def test_dispatches_edges_exist_in_graph(self, pipeline_ctx):
        """At least one DISPATCHES edge must exist after the pipeline."""
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method)-[d:DISPATCHES]->(e) RETURN m.fqn AS caller, e.fqn AS target LIMIT 20"
        )
        assert rows, (
            "Expected at least one DISPATCHES edge in the graph. "
            "Check that phase_05 dispatch detection ran and found dispatch patterns."
        )

    def test_dispatches_count_in_stats(self, pipeline_ctx):
        """Pipeline stats should record dispatches_traced > 0."""
        count = pipeline_ctx.stats.get("dispatches_traced", 0)
        assert count > 0, (
            f"Expected dispatches_traced > 0 in pipeline stats, got {count}. "
            "Phase_05 dispatch detection may not have run."
        )


# ── UserService::create → UserRegistered ─────────────────────────────────────

class TestUserServiceDispatch:
    """UserService::create calls event(new UserRegistered($user)) — must produce an edge."""

    def test_user_service_create_dispatches_user_registered(self, pipeline_ctx):
        """DISPATCHES edge must exist: UserService::create → UserRegistered."""
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method)-[d:DISPATCHES]->(e:Event) "
            "WHERE m.fqn CONTAINS 'UserService' AND m.name = 'create' "
            "RETURN m.fqn AS caller, e.fqn AS target, d.dispatch_type AS dtype"
        )
        assert rows, (
            "Expected DISPATCHES edge from UserService::create to an Event node. "
            "The fixture has `event(new UserRegistered($user))` in UserService.php:14."
        )
        dtypes = [r.get("dtype") for r in rows]
        assert "event" in dtypes, f"Expected dispatch_type='event', got {dtypes}"

    def test_user_registered_event_node_exists(self, pipeline_ctx):
        """The UserRegistered Event node must be indexed."""
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (e:Event) WHERE e.fqn CONTAINS 'UserRegistered' "
            "RETURN e.fqn AS fqn LIMIT 1"
        )
        assert rows, (
            "Expected an Event node for UserRegistered. "
            "Check that phase_03/04 indexes app/Events/UserRegistered.php."
        )

    def test_dispatch_edge_has_correct_props(self, pipeline_ctx):
        """DISPATCHES edge must have dispatch_type and is_queued properties."""
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method)-[d:DISPATCHES]->(e:Event) "
            "WHERE m.fqn CONTAINS 'UserService' AND m.name = 'create' "
            "RETURN d.dispatch_type AS dtype, d.is_queued AS queued"
        )
        assert rows
        row = rows[0]
        assert row.get("dtype") == "event"
        assert row.get("queued") is False or row.get("queued") == 0


# ── Caller method node is indexed ─────────────────────────────────────────────

class TestCallerMethodIndexed:
    """Prerequisite: UserService::create method node must exist."""

    def test_user_service_create_method_exists(self, pipeline_ctx):
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method) WHERE m.fqn = 'App\\\\Services\\\\UserService::create' "
            "RETURN m.fqn AS fqn LIMIT 1"
        )
        assert rows, (
            "Method node App\\Services\\UserService::create not found. "
            "Phase_03 may not have parsed UserService.php."
        )

    def test_user_service_method_has_node_id(self, pipeline_ctx):
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method) WHERE m.fqn = 'App\\\\Services\\\\UserService::create' "
            "RETURN m.node_id AS nid LIMIT 1"
        )
        assert rows
        assert rows[0].get("nid"), "Method node_id should not be empty"


# ── No false-positive dispatch edges ─────────────────────────────────────────

class TestNoFalsePositiveDispatches:
    """Plain static calls like Hash::make, User::create should not produce DISPATCHES edges."""

    def test_hash_make_not_dispatched(self, pipeline_ctx):
        """Hash::make should not produce a DISPATCHES edge."""
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method)-[d:DISPATCHES]->(t) "
            "WHERE t.fqn CONTAINS 'Hash' OR t.name = 'Hash' "
            "RETURN t.fqn AS fqn LIMIT 5"
        )
        assert not rows, f"Unexpected DISPATCHES edge to Hash: {rows}"

    def test_user_create_not_dispatched(self, pipeline_ctx):
        """User::create (Eloquent) should not produce a DISPATCHES edge to User model."""
        db = pipeline_ctx.db
        rows = db.execute(
            "MATCH (m:Method)-[d:DISPATCHES]->(t:EloquentModel) "
            "WHERE t.name = 'User' "
            "RETURN m.fqn AS caller, t.fqn AS target LIMIT 5"
        )
        assert not rows, f"User::create produced a false-positive DISPATCHES edge: {rows}"
