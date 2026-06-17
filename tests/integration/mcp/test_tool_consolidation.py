"""WS3 guard: the 12 primary tools exist and dispatch identically to legacy.

The consolidated tools (search/sql/map/db/trace/risks/tests/status, plus the
pre-existing cypher/context/feature_context/impact) are the documented surface.
The legacy single-purpose tools remain as deprecated aliases for one release.
These tests assert (a) the primary surface is registered and (b) each
dispatcher returns exactly what the legacy tool returns — so consolidation can
never silently change behavior.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore")

TINY = Path(__file__).parent.parent.parent / "fixtures" / "tiny-laravel-app"

PRIMARY_12 = [
    "laravelgraph_search",
    "laravelgraph_cypher",
    "laravelgraph_sql",
    "laravelgraph_context",
    "laravelgraph_feature_context",
    "laravelgraph_impact",
    "laravelgraph_map",
    "laravelgraph_trace",
    "laravelgraph_db",
    "laravelgraph_risks",
    "laravelgraph_tests",
    "laravelgraph_status",
]

# (new dispatcher call) must equal (legacy call)
EQUIVALENCES = [
    (("laravelgraph_map", {"kind": "routes"}), ("laravelgraph_routes", {})),
    (("laravelgraph_map", {"kind": "models", "filter": "User"}),
     ("laravelgraph_models", {"model_name": "User"})),
    (("laravelgraph_map", {"kind": "events"}), ("laravelgraph_events", {})),
    (("laravelgraph_map", {"kind": "bindings"}), ("laravelgraph_bindings", {})),
    (("laravelgraph_db", {"mode": "schema", "table": "users"}),
     ("laravelgraph_schema", {"table_name": "users"})),
    (("laravelgraph_db", {"mode": "context", "table": "users"}),
     ("laravelgraph_db_context", {"table": "users"})),
    (("laravelgraph_trace", {"kind": "request", "target": "/posts"}),
     ("laravelgraph_request_flow", {"route": "/posts"})),
    (("laravelgraph_risks", {"kind": "perf"}), ("laravelgraph_performance_risks", {})),
    (("laravelgraph_risks", {"kind": "dead_code"}), ("laravelgraph_dead_code", {})),
    (("laravelgraph_tests", {"symbol": "User"}), ("laravelgraph_suggest_tests", {"symbol": "User"})),
    (("laravelgraph_search", {"q": "User"}), ("laravelgraph_query", {"query": "User"})),
]


@pytest.fixture(scope="module")
def app_root():
    from eval.client import index_app

    indexed = index_app(TINY, into=Path(tempfile.mkdtemp(prefix="lg_ws3_")) / "tiny")
    yield indexed.root
    indexed.cleanup()


def test_primary_surface_registered(app_root):
    from eval.client import list_tool_names

    names = set(list_tool_names(app_root))
    missing = [t for t in PRIMARY_12 if t not in names]
    assert not missing, f"primary tools missing: {missing}"


def test_no_plugin_tools_remain(app_root):
    from eval.client import list_tool_names

    assert not [n for n in list_tool_names(app_root) if "plugin" in n.lower()]


def test_dispatchers_equal_legacy(app_root):
    from eval.client import run_calls

    calls = []
    for new_c, leg_c in EQUIVALENCES:
        calls.append(new_c)
        calls.append(leg_c)
    resp = run_calls(app_root, calls)
    for i, (new_c, leg_c) in enumerate(EQUIVALENCES):
        assert resp[2 * i] == resp[2 * i + 1], (
            f"{new_c} diverged from {leg_c}"
        )
