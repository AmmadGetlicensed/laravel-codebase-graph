"""File watcher for LaravelGraph — live re-indexing on change.

Uses watchfiles (Rust-backed) for efficient OS-level file watching.
- File-local phases (parse, imports, calls, types) run immediately on change
- Global phases (communities, processes, dead code) batch with 30s debounce
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from laravelgraph.config import Config
from laravelgraph.logging import get_logger

logger = get_logger(__name__)

# Phases that can run on a single file (fast, local)
LOCAL_PHASES = [3, 4, 5, 6, 7, 20]

# Phases that need global context (slow, batched)
GLOBAL_PHASES = [8, 9, 10, 12]


def start_watch(
    project_root: Path,
    config: Config,
    interactive: bool = False,
) -> None:
    """Start watching the project for file changes.

    Args:
        project_root: Laravel project root
        config: LaravelGraph config
        interactive: If True, print status to console
    """
    try:
        from watchfiles import watch, Change
    except ImportError:
        logger.error("watchfiles not installed. Install with: pip install watchfiles")
        return

    debounce = config.pipeline.watch_debounce_seconds
    pending_global_reindex = threading.Event()
    last_global_run = [time.time()]

    def _trigger_global() -> None:
        """Run global phases after debounce period."""
        while True:
            pending_global_reindex.wait()
            pending_global_reindex.clear()
            time.sleep(debounce)
            if not pending_global_reindex.is_set():
                _run_global_phases(project_root, config)
                last_global_run[0] = time.time()

    global_thread = threading.Thread(target=_trigger_global, daemon=True)
    global_thread.start()

    watch_path = str(project_root)
    skip_patterns = ["/.laravelgraph/", "/vendor/", "/node_modules/", "/storage/", "/.git/"]

    logger.info("Watch mode started", project=str(project_root), debounce=debounce)

    for changes in watch(watch_path, watch_filter=_make_filter(skip_patterns)):
        for change_type, path in changes:
            path_obj = Path(path)
            rel_path = str(path_obj.relative_to(project_root))

            if any(skip in path for skip in skip_patterns):
                continue

            logger.info("File changed", change_type=change_type.name, path=rel_path)

            if interactive:
                print(f"  [{change_type.name}] {rel_path}")

            # Re-index the changed file immediately
            if path.endswith(".php"):
                _run_file_phases(project_root, config, path_obj)
            elif path.endswith(".blade.php"):
                _run_blade_reindex(project_root, config, path_obj)

            # Schedule global re-index
            pending_global_reindex.set()


def _make_filter(skip_patterns: list[str]):
    from watchfiles import Change

    def _filter(change: Change, path: str) -> bool:
        if not (path.endswith(".php") or path.endswith(".json") or path.endswith(".env")):
            return False
        for pattern in skip_patterns:
            if pattern in path:
                return False
        return True

    return _filter


def _run_file_phases(project_root: Path, config: Config, file_path: Path) -> None:
    """Run file-local pipeline phases for a single changed file."""
    start = time.perf_counter()
    try:
        from laravelgraph.config import index_dir
        from laravelgraph.core.graph import GraphDB
        from laravelgraph.parsers.composer import parse_composer
        from laravelgraph.pipeline.orchestrator import PipelineContext

        db = GraphDB(index_dir(project_root) / "graph.kuzu")
        composer = parse_composer(project_root / "composer.json")

        ctx = PipelineContext(
            project_root=project_root,
            config=config,
            db=db,
            composer=composer,
            php_files=[file_path],
            all_files=[file_path],
        )

        # Remove stale symbols from this file
        db.delete_file_symbols(str(file_path))

        # Re-run local phases
        from laravelgraph.pipeline import phase_03_ast, phase_04_imports, phase_05_calls, phase_06_heritage
        for phase_fn in [phase_03_ast.run, phase_04_imports.run, phase_05_calls.run, phase_06_heritage.run]:
            try:
                phase_fn(ctx)
            except Exception as e:
                logger.error("Watch phase failed", phase=str(phase_fn), error=str(e))

        elapsed = (time.perf_counter() - start) * 1000
        logger.info("File re-indexed", file=str(file_path.name), duration_ms=round(elapsed, 2))

    except Exception as e:
        logger.error("File re-index failed", file=str(file_path), error=str(e))


def _run_blade_reindex(project_root: Path, config: Config, file_path: Path) -> None:
    """Re-index a changed Blade template."""
    try:
        from laravelgraph.config import index_dir
        from laravelgraph.core.graph import GraphDB
        from laravelgraph.parsers.composer import parse_composer
        from laravelgraph.pipeline.orchestrator import PipelineContext
        from laravelgraph.pipeline import phase_18_blade

        db = GraphDB(index_dir(project_root) / "graph.kuzu")
        composer = parse_composer(project_root / "composer.json")

        ctx = PipelineContext(
            project_root=project_root,
            config=config,
            db=db,
            composer=composer,
            blade_files=[file_path],
            all_files=[file_path],
        )

        phase_18_blade.run(ctx)
        logger.info("Blade template re-indexed", file=str(file_path.name))
    except Exception as e:
        logger.error("Blade re-index failed", file=str(file_path), error=str(e))


def _run_global_phases(project_root: Path, config: Config) -> None:
    """Run batch global phases (community detection, dead code, etc.)."""
    start = time.perf_counter()
    try:
        from laravelgraph.config import index_dir
        from laravelgraph.core.graph import GraphDB
        from laravelgraph.parsers.composer import parse_composer
        from laravelgraph.pipeline.orchestrator import PipelineContext
        from laravelgraph.pipeline import phase_08_community, phase_09_flows, phase_10_dead_code

        db = GraphDB(index_dir(project_root) / "graph.kuzu")
        composer = parse_composer(project_root / "composer.json")

        ctx = PipelineContext(
            project_root=project_root,
            config=config,
            db=db,
            composer=composer,
        )

        for phase_fn in [phase_08_community.run, phase_09_flows.run, phase_10_dead_code.run]:
            try:
                phase_fn(ctx)
            except Exception as e:
                logger.error("Global phase failed", phase=str(phase_fn), error=str(e))

        elapsed = (time.perf_counter() - start) * 1000
        logger.info("Global re-index complete", duration_ms=round(elapsed, 2))
    except Exception as e:
        logger.error("Global re-index failed", error=str(e))
