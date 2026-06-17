"""File watcher for LaravelGraph — live re-indexing on change.

Uses watchfiles (Rust-backed) for efficient OS-level file watching.

On every changed file we re-run the phases that file can affect, so the
high-value semantic edges (routes, events→listeners→jobs, Eloquent
relationships, DB access, N+1) stay fresh — not just the call graph:

  any .php            → 3,4,5,6,7,20,21,22,28,32,33   (file-local symbol phases)
  app/Models/*        → + 13,25,26,31                  (Eloquent, table link, DB access, N+1)
  app/Events|Listeners|Jobs/* → + 17                   (event/listener/job graph)
  routes/*.php        → 14,15  (full route rebuild — phase 14 resets all routes)
  database/migrations/*.php → 19
  *.blade.php         → 18

Global, whole-graph phases (communities, flows, dead code) can't be computed
per file; they run on a debounced batch. Embeddings (phase 12) refresh on a
full `laravelgraph analyze`, not on watch.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from laravelgraph.config import Config, index_dir
from laravelgraph.core.graph import GraphDB
from laravelgraph.core.registry import Registry
from laravelgraph.logging import get_logger
from laravelgraph.parsers.composer import parse_composer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# File-local symbol phases: safe to run on a single changed .php file.
BASE_PHP_PHASES = [3, 4, 5, 6, 7, 20, 21, 22, 28, 32, 33]
# Global, whole-graph phases — run batched on a debounce, never per file.
GLOBAL_PHASES = [8, 9, 10]


def _phase_fn(num: int):
    """Lazy-import a phase's run() by number (avoids circular imports)."""
    from laravelgraph.pipeline import (
        phase_03_ast,
        phase_04_imports,
        phase_05_calls,
        phase_06_heritage,
        phase_07_types,
        phase_08_community,
        phase_09_flows,
        phase_10_dead_code,
        phase_13_eloquent,
        phase_14_routes,
        phase_15_middleware,
        phase_17_events,
        phase_19_schema,
        phase_20_config,
        phase_21_di,
        phase_22_api,
        phase_25_model_table_link,
        phase_26_db_access,
        phase_28_contracts,
        phase_31_query_patterns,
        phase_32_http_clients,
        phase_33_notifications,
    )

    return {
        3: phase_03_ast.run, 4: phase_04_imports.run, 5: phase_05_calls.run,
        6: phase_06_heritage.run, 7: phase_07_types.run, 8: phase_08_community.run,
        9: phase_09_flows.run, 10: phase_10_dead_code.run, 13: phase_13_eloquent.run,
        14: phase_14_routes.run, 15: phase_15_middleware.run, 17: phase_17_events.run,
        19: phase_19_schema.run, 20: phase_20_config.run, 21: phase_21_di.run,
        22: phase_22_api.run, 25: phase_25_model_table_link.run,
        26: phase_26_db_access.run, 28: phase_28_contracts.run,
        31: phase_31_query_patterns.run, 32: phase_32_http_clients.run,
        33: phase_33_notifications.run,
    }[num]


def _phases_for_php_file(file_path: Path) -> list[int]:
    """Which phases a changed PHP file should trigger, by Laravel convention."""
    s = str(file_path).replace("\\", "/")
    phases = list(BASE_PHP_PHASES)
    if "/app/Models/" in s:
        phases += [13, 25, 26, 31]
    if any(seg in s for seg in ("/app/Events/", "/app/Listeners/", "/app/Jobs/")):
        phases += [17]
    return phases


def _run_phases(ctx: PipelineContext, phases: list[int]) -> None:
    for num in phases:
        try:
            _phase_fn(num)(ctx)
        except Exception as e:
            logger.error("Watch phase failed", phase=num, error=str(e))


def start_watch(
    project_root: Path,
    config: Config,
    interactive: bool = False,
) -> None:
    """Start watching the project for file changes."""
    try:
        from watchfiles import watch
    except ImportError:
        logger.error("watchfiles not installed. Install with: pip install watchfiles")
        return

    debounce = config.pipeline.watch_debounce_seconds
    pending_global_reindex = threading.Event()

    def _trigger_global() -> None:
        while True:
            pending_global_reindex.wait()
            pending_global_reindex.clear()
            time.sleep(debounce)
            if not pending_global_reindex.is_set():
                _run_global_phases(project_root, config)

    threading.Thread(target=_trigger_global, daemon=True).start()

    skip_patterns = ["/.laravelgraph/", "/vendor/", "/node_modules/", "/storage/", "/.git/"]
    logger.info("Watch mode started", project=str(project_root), debounce=debounce)

    for changes in watch(str(project_root), watch_filter=_make_filter(skip_patterns)):
        for _change_type, path in changes:
            if any(skip in path for skip in skip_patterns):
                continue
            path_obj = Path(path)
            rel = str(path_obj.relative_to(project_root)) if str(path_obj).startswith(str(project_root)) else path
            if interactive:
                print(f"  [changed] {rel}")
            logger.info("File changed", path=rel)
            _reindex_path(project_root, config, path_obj)
            pending_global_reindex.set()


def _reindex_path(project_root: Path, config: Config, path_obj: Path) -> None:
    """Route a changed path to the correct incremental re-index handler."""
    s = str(path_obj).replace("\\", "/")
    if s.endswith(".blade.php"):
        _run_blade_reindex(project_root, config, path_obj)
    elif "/routes/" in s and s.endswith(".php"):
        _run_route_reindex(project_root, config)
    elif "/database/migrations/" in s and s.endswith(".php"):
        _run_migration_reindex(project_root, config, path_obj)
    elif s.endswith(".php"):
        _run_file_phases(project_root, config, path_obj)


def _make_filter(skip_patterns: list[str]):
    from watchfiles import Change

    def _filter(change: Change, path: str) -> bool:
        if not (path.endswith(".php") or path.endswith(".json") or path.endswith(".env")):
            return False
        return not any(pattern in path for pattern in skip_patterns)

    return _filter


def _open_ctx(project_root: Path, config: Config, db: GraphDB, **kwargs: Any) -> PipelineContext:
    composer = parse_composer(project_root / "composer.json")
    return PipelineContext(
        project_root=project_root, config=config, db=db, composer=composer, **kwargs
    )


def _run_file_phases(project_root: Path, config: Config, file_path: Path) -> None:
    """Re-index a single changed PHP file across all phases it can affect."""
    start = time.perf_counter()
    db = GraphDB(index_dir(project_root) / "graph.kuzu")
    try:
        db.delete_file_symbols(str(file_path))
        ctx = _open_ctx(
            project_root, config, db, php_files=[file_path], all_files=[file_path]
        )
        # Cross-file resolution needs the global FQN map; phase 3 re-adds this
        # file's own symbols on top.
        ctx.fqn_index = db.build_fqn_index()
        ctx.class_map = db.build_class_map()
        _run_phases(ctx, _phases_for_php_file(file_path))
        Registry().touch(project_root)
        logger.info(
            "File re-indexed",
            file=file_path.name,
            phases=_phases_for_php_file(file_path),
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
    finally:
        db.close()


def _run_route_reindex(project_root: Path, config: Config) -> None:
    """Rebuild the route graph (phase 14 resets all Route nodes, so re-scan all
    route files) plus middleware resolution."""
    db = GraphDB(index_dir(project_root) / "graph.kuzu")
    try:
        routes_dir = project_root / "routes"
        route_files = list(routes_dir.glob("*.php")) if routes_dir.exists() else []
        ctx = _open_ctx(project_root, config, db, route_files=route_files)
        ctx.class_map = db.build_class_map()
        ctx.fqn_index = db.build_fqn_index()
        _run_phases(ctx, [14, 15])
        Registry().touch(project_root)
        logger.info("Routes re-indexed", route_files=len(route_files))
    finally:
        db.close()


def _run_migration_reindex(project_root: Path, config: Config, file_path: Path) -> None:
    """Re-run schema extraction. Phase 19 reads all migration files, so pass the
    full set to keep table/column nodes consistent."""
    db = GraphDB(index_dir(project_root) / "graph.kuzu")
    try:
        mig_dir = project_root / "database" / "migrations"
        migrations = list(mig_dir.glob("*.php")) if mig_dir.exists() else [file_path]
        ctx = _open_ctx(project_root, config, db, migration_files=migrations)
        _run_phases(ctx, [19])
        Registry().touch(project_root)
        logger.info("Migrations re-indexed", count=len(migrations))
    finally:
        db.close()


def _run_blade_reindex(project_root: Path, config: Config, file_path: Path) -> None:
    """Re-index a changed Blade template (phase 18)."""
    from laravelgraph.pipeline import phase_18_blade

    db = GraphDB(index_dir(project_root) / "graph.kuzu")
    try:
        ctx = _open_ctx(
            project_root, config, db, blade_files=[file_path], all_files=[file_path]
        )
        ctx.fqn_index = db.build_fqn_index()
        phase_18_blade.run(ctx)
        Registry().touch(project_root)
        logger.info("Blade template re-indexed", file=file_path.name)
    finally:
        db.close()


def _run_global_phases(project_root: Path, config: Config) -> None:
    """Run whole-graph phases (communities, flows, dead code) after a debounce."""
    start = time.perf_counter()
    db = GraphDB(index_dir(project_root) / "graph.kuzu")
    try:
        ctx = _open_ctx(project_root, config, db)
        _run_phases(ctx, GLOBAL_PHASES)
        Registry().touch(project_root)
        logger.info(
            "Global re-index complete",
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
    finally:
        db.close()
