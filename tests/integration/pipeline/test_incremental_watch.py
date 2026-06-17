"""WS5 guard: incremental re-indexing keeps high-value edges fresh.

Exercises the watcher's per-file handlers directly (the watchfiles event loop
itself isn't simulated) to prove a route addition, a class edit, and the
FQN-index rebuild all take effect without a full re-index.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

TINY = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture
def app_root():
    from eval.client import index_app

    app = index_app(TINY, into=Path(tempfile.mkdtemp(prefix="lg_ws5_")) / "tiny")
    yield app.root
    app.cleanup()


def _routes(root: Path) -> set[str]:
    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(root) / "graph.kuzu")
    try:
        return {r["uri"] for r in db.execute("MATCH (r:Route) RETURN r.uri AS uri")}
    finally:
        db.close()


def test_build_fqn_index_and_class_map(app_root):
    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    db = GraphDB(index_dir(app_root) / "graph.kuzu")
    try:
        fqn = db.build_fqn_index()
        cmap = db.build_class_map()
    finally:
        db.close()
    assert any("User" in k for k in fqn), "expected User in rebuilt fqn_index"
    assert any("App\\Models\\User" == k for k in cmap), "expected User in class_map"


def test_route_added_is_picked_up(app_root):
    from laravelgraph.config import Config
    from laravelgraph.watch.watcher import _run_route_reindex

    before = _routes(app_root)
    web = app_root / "routes" / "web.php"
    src = web.read_text()
    web.write_text(
        src + "\nRoute::get('/healthz', [App\\Http\\Controllers\\PostController::class, 'index'])"
        "->name('healthz');\n"
    )

    _run_route_reindex(app_root, Config.load(app_root))

    after = _routes(app_root)
    assert any("healthz" in u for u in after), f"new route missing: {after}"
    # Existing routes must survive the rebuild (not wiped to just the new one).
    assert any("posts" in u for u in after), "existing routes were lost"
    assert len(after) > len(before) - 1


def test_class_edit_reindexes_new_method(app_root):
    from laravelgraph.config import Config, index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.watch.watcher import _run_file_phases

    ctrl = app_root / "app" / "Http" / "Controllers" / "PostController.php"
    src = ctrl.read_text()
    # add a new public method before the final closing brace
    edited = src.rstrip().rsplit("}", 1)[0] + (
        "\n    public function freshlyAdded(): void\n    {\n        $x = 1;\n    }\n}\n"
    )
    ctrl.write_text(edited)

    _run_file_phases(app_root, Config.load(app_root), ctrl)

    db = GraphDB(index_dir(app_root) / "graph.kuzu")
    try:
        rows = db.execute(
            "MATCH (m:Method) WHERE m.name = 'freshlyAdded' RETURN m.name AS name"
        )
    finally:
        db.close()
    assert rows, "newly added method was not picked up by incremental re-index"
