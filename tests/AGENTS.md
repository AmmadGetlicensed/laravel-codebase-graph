# AGENTS.md — tests/

## OVERVIEW

Three test tiers: unit (fast, no pipeline), integration (full pipeline on tiny-laravel-app), performance. Shared fixtures in `conftest.py`. Coverage gate: 80% minimum.

## STRUCTURE

```
tests/
├── conftest.py                    # tiny_app_root + tmp_graph fixtures (session-scoped)
├── fixtures/tiny-laravel-app/     # Minimal Laravel app used by all integration tests
├── unit/                          # Fast tests; use tmp_graph, never run Pipeline
│   ├── graph/                     # GraphDB, schema tests
│   ├── mcp/                       # MCP tool unit tests
│   ├── parsing/                   # PHPParser, BladeParser tests
│   └── pipeline/                  # Individual phase unit tests
├── integration/
│   ├── pipeline/                  # Full Pipeline.run() on tiny-laravel-app
│   ├── mcp/                       # MCP tools against a pre-built graph
│   └── commands/                  # CLI command integration tests
├── performance/                   # pytest-benchmark tests
└── snapshots/                     # syrupy snapshot files
```

## FIXTURES

```python
# conftest.py
tiny_app_root -> Path   # path to tests/fixtures/tiny-laravel-app/
tmp_graph     -> GraphDB  # fresh in-memory KuzuDB, closed after test
```

**Integration pattern** — `scope="module"` to run pipeline once per test class:
```python
@pytest.fixture(scope="module")
def pipeline_ctx(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("test")
    app_copy = tmp / "tiny-laravel-app"
    shutil.copytree(str(TINY_APP), str(app_copy))
    cfg = Config()
    cfg.embedding.enabled = False   # Always disable embeddings in tests
    pipeline = Pipeline(app_copy, config=cfg)
    return pipeline.run(full=True, skip_embeddings=True)
```

## CONVENTIONS

- **No LLM calls in tests** — use `Config(summary=SummaryConfig(enabled=False))` or mock.
- **No real DB connections in tests** — skip or mock live DB phases.
- **Use `tmp_graph` fixture** for all unit tests touching GraphDB — never a real project.
- **Disable embeddings** in integration tests (`skip_embeddings=True`, `cfg.embedding.enabled = False`).
- **Run without coverage flags manually**: `pytest tests/ --override-ini="addopts=" -v`

## WHERE TO ADD TESTS

| What you're testing | Where |
|--------------------|-------|
| New pipeline phase | `tests/integration/pipeline/` (use pipeline_ctx fixture) or `tests/unit/pipeline/` |
| New MCP tool | `tests/integration/mcp/test_mcp_tools.py` |
| GraphDB / schema change | `tests/unit/graph/` |
| Parser change | `tests/unit/parsing/` |
| CLI command | `tests/integration/commands/test_cli_commands.py` |
| Regression on specific fixture | Add PHP/Blade file to `tests/fixtures/tiny-laravel-app/` |

## ANTI-PATTERNS

- **Do not create `Pipeline` in unit tests** — pipeline runs are slow; use `tmp_graph` directly.
- **Do not hardcode fixture paths** — use `TINY_APP = Path(__file__).parent.parent / "fixtures" / "tiny-laravel-app"`.
- **Do not assert exact counts** when adding fixture files — use `>= N` assertions.
- **Do not run with `--cov`** manually — `pyproject.toml` `addopts` injects it; override with `--override-ini="addopts="`.
