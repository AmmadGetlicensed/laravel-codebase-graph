"""Shared pytest fixtures for LaravelGraph tests."""

import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
TINY_APP = FIXTURES_DIR / "tiny-laravel-app"


@pytest.fixture
def tiny_app_root() -> Path:
    return TINY_APP


@pytest.fixture
def tmp_graph(tmp_path):
    """Create a temporary GraphDB for testing."""
    from laravelgraph.core.graph import GraphDB
    db_path = tmp_path / "test_graph.kuzu"
    db = GraphDB(db_path)
    yield db
    db.close()
