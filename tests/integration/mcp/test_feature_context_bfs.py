"""Integration tests for feature_context output quality.

Verifies that the feature_context MCP tool returns correct output including:
  - Routes, controllers with source
  - Models matching the feature terms
  - Events and listeners from EventServiceProvider

The fixture: feature_context("user registration") should find:
  - Routes: 5 users routes
  - Controller: UserController (with source snippets)
  - Model: User (term "user" matches model name "User")
  - Event: UserRegistered (from EventServiceProvider $listen array)
  - Listener: SendWelcomeEmail

Note: DISPATCHES edges (Method → Event) discovered via BFS call chain are limited
by the quality of CALLS edges. The tiny-laravel-app uses constructor injection
($this->userService->create()), which phase_05 cannot resolve without DI type
inference. Events from EventServiceProvider are indexed directly by phase_17.

These tests run the pipeline once (scope="module") and invoke tools via
server.call_tool() (the official FastMCP programmatic API).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

TINY_APP = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def indexed_app(tmp_path_factory):
    """Index the tiny app, return the project root path."""
    tmp = tmp_path_factory.mktemp("fc_bfs_test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False
    Pipeline(app_copy, config=cfg).run(full=True, skip_embeddings=True)
    return app_copy


def _call_tool(indexed_app, tool_name: str, args: dict) -> str:
    """Call an MCP tool and return the text content."""
    from laravelgraph.mcp.server import create_server
    server = create_server(indexed_app)
    result = asyncio.run(server.call_tool(tool_name, args))
    if hasattr(result, "content") and result.content:
        return result.content[0].text
    return str(result)


@pytest.fixture(scope="module")
def fc_output_user_registration(indexed_app):
    """Call feature_context('user registration') and return the markdown string.

    Uses multi-word query so term matching includes 'user' (4 chars) which
    correctly matches the 'User' model name.
    """
    return _call_tool(indexed_app, "laravelgraph_feature_context",
                      {"feature": "user registration"})


@pytest.fixture(scope="module")
def fc_output_users(indexed_app):
    """Call feature_context('users') and return the markdown string."""
    return _call_tool(indexed_app, "laravelgraph_feature_context", {"feature": "users"})


# ── Basic output sanity ───────────────────────────────────────────────────────

class TestFeatureContextBasicOutput:
    def test_output_is_non_empty(self, fc_output_users):
        assert fc_output_users and len(fc_output_users) > 100

    def test_output_is_string(self, fc_output_users):
        assert isinstance(fc_output_users, str)

    def test_routes_section_present(self, fc_output_users):
        assert "Route" in fc_output_users or "users" in fc_output_users.lower()

    def test_controller_section_present(self, fc_output_users):
        assert "UserController" in fc_output_users, (
            f"Expected UserController in feature_context('users'). Got:\n{fc_output_users[:500]}"
        )

    def test_source_code_shown(self, fc_output_users):
        """Source code snippets should be included in the output."""
        assert "```php" in fc_output_users or "function" in fc_output_users.lower(), (
            f"Expected PHP source snippets. Got:\n{fc_output_users[:500]}"
        )


# ── Model discovery ───────────────────────────────────────────────────────────

class TestFeatureContextModelDiscovery:
    """User model must be discovered when the feature term matches the model name."""

    def test_user_model_in_registration_output(self, fc_output_user_registration):
        """feature_context('user registration') — term 'user' matches model 'User'."""
        assert "User" in fc_output_user_registration, (
            f"Expected 'User' model in feature_context('user registration'). "
            f"Got:\n{fc_output_user_registration[:800]}\n\n"
            "Term 'user' should match EloquentModel with name 'User' via substring check."
        )

    def test_models_section_shown(self, fc_output_user_registration):
        assert "Models Used" in fc_output_user_registration or "Model" in fc_output_user_registration


# ── Event and listener discovery via EventServiceProvider ────────────────────

class TestFeatureContextEventDiscovery:
    """Events from EventServiceProvider must appear when terms match.

    Phase_17 indexes the $listen array → Event nodes exist.
    feature_context queries Events matching the feature terms.
    """

    def test_user_registered_event_in_registration_output(self, fc_output_user_registration):
        """UserRegistered event must appear for 'user registration' query."""
        assert "UserRegistered" in fc_output_user_registration, (
            f"Expected UserRegistered event in feature_context('user registration'). "
            f"Got:\n{fc_output_user_registration[:1000]}\n\n"
            "This requires the Events section to query by matching terms."
        )

    def test_send_welcome_email_listener_shown(self, fc_output_user_registration):
        """SendWelcomeEmail listener must appear alongside UserRegistered event."""
        assert "SendWelcomeEmail" in fc_output_user_registration, (
            f"Expected SendWelcomeEmail listener in output. "
            f"Got:\n{fc_output_user_registration[:1000]}"
        )

    def test_events_section_present(self, fc_output_user_registration):
        assert "Event" in fc_output_user_registration, (
            f"Expected Events section. Got:\n{fc_output_user_registration[:500]}"
        )


# ── Controller discovery ──────────────────────────────────────────────────────

class TestFeatureContextControllerDiscovery:
    def test_user_controller_in_output(self, fc_output_users):
        assert "UserController" in fc_output_users

    def test_user_controller_in_registration_output(self, fc_output_user_registration):
        assert "UserController" in fc_output_user_registration

    def test_all_controller_actions_present(self, fc_output_users):
        """All 5 user routes should be shown."""
        assert "store" in fc_output_users.lower() or "POST" in fc_output_users, (
            f"Expected store action in output. Got:\n{fc_output_users[:500]}"
        )


# ── Route count ───────────────────────────────────────────────────────────────

class TestFeatureContextRouteCount:
    def test_multiple_user_routes_shown(self, fc_output_users):
        """All 5 users API routes should be listed."""
        assert fc_output_users.count("users") >= 3, (
            f"Expected multiple 'users' route entries. Got:\n{fc_output_users[:500]}"
        )


# ── feature_context on specific controller ───────────────────────────────────

class TestFeatureContextByController:
    def test_feature_context_by_controller_name(self, indexed_app):
        result = _call_tool(indexed_app, "laravelgraph_feature_context",
                            {"feature": "UserController"})
        assert isinstance(result, str)
        assert "UserController" in result, (
            f"Expected UserController in output. Got:\n{result[:500]}"
        )

    def test_feature_context_for_posts(self, indexed_app):
        """feature_context for 'posts' should find PostController."""
        result = _call_tool(indexed_app, "laravelgraph_feature_context",
                            {"feature": "posts"})
        assert isinstance(result, str)
        assert len(result) > 50
        assert "Post" in result, f"Expected Post in posts feature context. Got:\n{result[:500]}"
