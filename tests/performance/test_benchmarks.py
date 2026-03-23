"""Performance benchmarks for LaravelGraph.

Run with: pytest tests/performance/ --benchmark-only
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

TINY_APP = Path(__file__).parent.parent / "fixtures" / "tiny-laravel-app"


@pytest.fixture(scope="module")
def indexed_tiny_app(tmp_path_factory):
    """Index the tiny fixture app once for all benchmarks."""
    tmp = tmp_path_factory.mktemp("bench")
    app_copy = tmp / "app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False
    pipeline = Pipeline(app_copy, config=cfg)
    ctx = pipeline.run(full=True, skip_embeddings=True)
    return ctx


def test_benchmark_full_pipeline_tiny(benchmark, tmp_path):
    """Benchmark: full pipeline on tiny fixture must complete < 10s."""
    app_copy = tmp_path / "app"
    shutil.copytree(str(TINY_APP), str(app_copy))

    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    cfg = Config()
    cfg.embedding.enabled = False

    def run_pipeline():
        Pipeline(app_copy, config=cfg).run(full=True, skip_embeddings=True)

    result = benchmark.pedantic(run_pipeline, iterations=1, rounds=1)

    # Verify timing threshold
    assert benchmark.stats["mean"] < 10.0, (
        f"Pipeline took {benchmark.stats['mean']:.1f}s — target < 10s"
    )


def test_benchmark_php_parser(benchmark):
    """Benchmark: parsing User.php model must be < 100ms."""
    from laravelgraph.parsers.php import PHPParser

    user_php = TINY_APP / "app" / "Models" / "User.php"
    parser = PHPParser()

    result = benchmark(parser.parse_file, user_php)
    assert result.classes  # Ensure something was parsed
    assert benchmark.stats["mean"] < 0.1  # < 100ms


def test_benchmark_blade_parser(benchmark):
    """Benchmark: parsing a Blade template must be < 50ms."""
    from laravelgraph.parsers.blade import BladeParser

    blade_file = TINY_APP / "resources" / "views" / "posts" / "index.blade.php"
    parser = BladeParser()

    result = benchmark(parser.parse_file, blade_file)
    assert result.extends is not None
    assert benchmark.stats["mean"] < 0.05  # < 50ms


def test_benchmark_search(benchmark, indexed_tiny_app):
    """Benchmark: hybrid search must complete < 200ms."""
    from laravelgraph.config import SearchConfig
    from laravelgraph.search.hybrid import HybridSearch

    db = indexed_tiny_app.db
    search = HybridSearch(db, SearchConfig())
    search.build_index()

    def run_search():
        return search.search("user authentication", limit=10)

    results = benchmark(run_search)
    assert benchmark.stats["mean"] < 0.2  # < 200ms


def test_benchmark_impact_analysis(benchmark, indexed_tiny_app):
    """Benchmark: impact analysis must complete < 500ms."""
    from laravelgraph.analysis.impact import ImpactAnalyzer

    db = indexed_tiny_app.db
    analyzer = ImpactAnalyzer(db)

    # Find a method to analyze
    try:
        methods = db.execute("MATCH (m:Method) RETURN m.node_id AS id LIMIT 1")
        if not methods:
            pytest.skip("No methods in graph")
        method_id = methods[0]["id"]
    except Exception:
        pytest.skip("Could not query methods")

    def run_impact():
        return analyzer.analyze(method_id, depth=3)

    result = benchmark(run_impact)
    assert benchmark.stats["mean"] < 0.5  # < 500ms
