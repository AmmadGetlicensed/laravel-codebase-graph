# Contributing to LaravelGraph

Thank you for your interest in contributing! This document covers setup, conventions, and the contribution process.

## Table of Contents
- [Development Setup](#development-setup)
- [Project Structure](#project-structure)
- [Adding Pipeline Phases](#adding-pipeline-phases)
- [Adding MCP Tools](#adding-mcp-tools)
- [Testing](#testing)
- [Code Style](#code-style)
- [Pull Request Process](#pull-request-process)

---

## Development Setup

```bash
# Clone the repository
git clone https://github.com/laravelgraph/laravelgraph.git
cd laravelgraph

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate  # Windows

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Verify installation
laravelgraph --version
```

### Testing your setup:

```bash
# Run the test suite
pytest tests/ -v

# Run against the tiny fixture
laravelgraph analyze tests/fixtures/tiny-laravel-app --full
laravelgraph status tests/fixtures/tiny-laravel-app
laravelgraph routes -p tests/fixtures/tiny-laravel-app
```

---

## Project Structure

```
laravelgraph/
├── laravelgraph/
│   ├── __init__.py          # Version and package metadata
│   ├── cli.py               # Typer CLI (all commands)
│   ├── config.py            # Pydantic configuration model
│   ├── logging.py           # Structured logging via structlog
│   ├── core/
│   │   ├── graph.py         # KuzuDB graph database interface
│   │   ├── schema.py        # All node/relationship type definitions
│   │   └── registry.py      # Global repository registry
│   ├── parsers/
│   │   ├── php.py           # PHP AST parser (tree-sitter + regex fallback)
│   │   ├── blade.py         # Blade template parser
│   │   └── composer.py      # composer.json + PSR-4 autoloading
│   ├── pipeline/
│   │   ├── orchestrator.py  # Pipeline runner + PipelineContext
│   │   ├── phase_01_*.py    # One file per phase (1–23)
│   │   └── ...
│   ├── search/
│   │   └── hybrid.py        # BM25 + vector + fuzzy + RRF
│   ├── analysis/
│   │   └── impact.py        # Blast radius analysis
│   ├── mcp/
│   │   └── server.py        # FastMCP server with all tools
│   └── watch/
│       └── watcher.py       # watchfiles-based live re-indexing
├── tests/
│   ├── fixtures/            # Laravel test applications
│   ├── unit/                # Isolated unit tests
│   ├── integration/         # End-to-end pipeline tests
│   └── performance/         # Benchmark tests
├── pyproject.toml
└── README.md
```

---

## Adding Pipeline Phases

Each phase is a Python module in `laravelgraph/pipeline/` with a single `run(ctx: PipelineContext)` function.

### Phase template:

```python
"""Phase NN: <description>."""
from __future__ import annotations

from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

def run(ctx: PipelineContext) -> None:
    """<What this phase does>."""
    with phase_timer("Phase Name"):
        _do_work(ctx)

def _do_work(ctx: PipelineContext) -> None:
    for item in ctx.php_files:
        try:
            _process_file(ctx, item)
        except Exception as e:
            ctx.errors.append(f"Phase NN: {item}: {e}")
            logger.warning("Item processing failed", file=str(item), error=str(e))

    ctx.stats["items_processed"] = len(ctx.php_files)
```

Rules:
- Never raise from `run()` — always catch and append to `ctx.errors`
- Use `ctx.stats["key"] += count` to track meaningful metrics
- Log with `logger.info/warning/error` — never `print()`
- Reference the `PipelineContext` attributes, don't re-query files from disk
- Add your phase to `orchestrator.py`'s phase list

---

## Adding MCP Tools

Add tools to `laravelgraph/mcp/server.py` inside the `create_server()` function:

```python
@mcp.tool()
def laravelgraph_my_tool(param: str) -> str:
    """One-line description for the AI agent.

    Args:
        param: Description of the parameter
    """
    start = time.perf_counter()
    db = _db()

    # ... implementation ...

    elapsed = (time.perf_counter() - start) * 1000
    _log_tool("laravelgraph_my_tool", {"param": param}, result_count, elapsed)

    return result_text + _next_steps(
        "Next action hint for the AI agent",
        "Another useful next step",
    )
```

Rules:
- Tool name must start with `laravelgraph_`
- Return Markdown-formatted strings (AI agents receive these)
- Always include next-step hints via `_next_steps()`
- Log every tool call via `_log_tool()`
- Handle errors gracefully — return error message strings, don't raise

---

## Testing

### Test categories

| Category | Location | Purpose |
|----------|----------|---------|
| Unit | `tests/unit/` | Isolated parser/graph tests |
| Integration | `tests/integration/` | Full pipeline on fixtures |
| Performance | `tests/performance/` | Benchmarks with thresholds |
| Snapshots | `tests/snapshots/` | CLI output, MCP response structure |

### Running tests

```bash
# All tests
pytest tests/

# Single category
pytest tests/unit/ -v
pytest tests/integration/ -v

# With coverage report
pytest --cov=laravelgraph --cov-report=html

# Single test file
pytest tests/unit/parsing/test_php_parser.py -v

# Benchmarks (normally skipped)
pytest tests/performance/ --benchmark-only
```

### Coverage requirements

- Overall: 80% minimum (CI enforced)
- MCP tools: 100% target
- Pipeline phases: 90% target

### Writing tests

- Every new feature needs tests in the relevant category
- Every bug fix needs a regression test
- Use `tests/fixtures/tiny-laravel-app/` for integration tests
- Don't mock the graph database in integration tests (defeats the purpose)
- Use `tmp_path` for isolated test runs

---

## Code Style

```bash
# Lint
ruff check laravelgraph/ --fix

# Format (ruff handles this too)
ruff format laravelgraph/

# Type check
mypy laravelgraph/
```

Conventions:
- `from __future__ import annotations` in every module
- Type hints on all public functions
- Docstrings on public classes and functions
- structlog for all logging — never `print()` in library code
- `logger = get_logger(__name__)` at module level

---

## Pull Request Process

1. Fork the repo and create a feature branch: `git checkout -b feat/my-feature`
2. Write your code and tests
3. Run the full test suite: `pytest tests/ -v`
4. Run the linter: `ruff check laravelgraph/`
5. Update CHANGELOG.md under `[Unreleased]`
6. Open a PR with a clear description of what changed and why
7. Reference any related issues

### PR checklist
- [ ] Tests pass (`pytest tests/`)
- [ ] Linter passes (`ruff check`)
- [ ] New features have tests
- [ ] Bug fixes have regression tests
- [ ] CHANGELOG.md updated
- [ ] Docstrings added/updated

---

## Reporting Bugs

Open an issue at https://github.com/laravelgraph/laravelgraph/issues with:
- LaravelGraph version (`laravelgraph --version`)
- Python version
- Laravel version of the project being analyzed
- Steps to reproduce
- Expected vs actual behavior
- Relevant log output (from `~/.laravelgraph/logs/laravelgraph-errors.log`)
