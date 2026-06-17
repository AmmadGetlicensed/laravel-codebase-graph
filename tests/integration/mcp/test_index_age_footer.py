"""WS4 guard: every tool response is stamped with index age, and a staleness
warning appears once a source file changes after indexing.
"""

from __future__ import annotations

import os
import tempfile
import time
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

TINY = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture
def indexed():
    from eval.client import index_app

    app = index_app(TINY, into=Path(tempfile.mkdtemp(prefix="lg_ws4_")) / "tiny")
    yield app.root
    app.cleanup()


def test_footer_present_and_fresh(indexed):
    from eval.client import call_tool

    out = call_tool(indexed, "laravelgraph_map", {"kind": "routes"})
    assert "Index age:" in out
    assert "changed since indexing" not in out  # nothing changed yet


def test_staleness_warning_after_edit(indexed):
    from eval.client import call_tool

    # Make a source file newer than the index timestamp.
    target = indexed / "app" / "Models" / "User.php"
    future = time.time() + 5
    os.utime(target, (future, future))

    out = call_tool(indexed, "laravelgraph_map", {"kind": "routes"})
    assert "changed since indexing" in out
    assert "re-run `laravelgraph analyze`" in out
