"""Pipeline orchestrator — coordinates all analysis phases."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from laravelgraph.config import Config, index_dir
from laravelgraph.core.graph import GraphDB
from laravelgraph.core.registry import Registry
from laravelgraph.logging import get_logger, get_pipeline_logger, phase_timer
from laravelgraph.parsers.composer import ComposerInfo, parse_composer

logger = get_logger(__name__)
pipeline_logger = get_pipeline_logger()


@dataclass
class PipelineContext:
    """Shared state passed through all pipeline phases."""

    project_root: Path
    config: Config
    db: GraphDB
    composer: ComposerInfo
    class_map: dict[str, Path] = field(default_factory=dict)  # FQN → file path
    php_files: list[Path] = field(default_factory=list)
    blade_files: list[Path] = field(default_factory=list)
    route_files: list[Path] = field(default_factory=list)
    migration_files: list[Path] = field(default_factory=list)
    all_files: list[Path] = field(default_factory=list)
    # Symbol caches built during parsing
    parsed_php: dict[str, Any] = field(default_factory=dict)  # path → PHPFile
    parsed_blade: dict[str, Any] = field(default_factory=dict)  # path → BladeParsed
    # FQN → node_id for cross-phase linking
    fqn_index: dict[str, str] = field(default_factory=dict)   # FQN → node_id
    route_nodes: list[dict[str, Any]] = field(default_factory=list)
    # Stats accumulated across phases
    stats: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    # Internal: set by CLI to enable live status updates from within phases
    _status_fn: Any = field(default=None, repr=False)

    def set_status(self, msg: str) -> None:
        """Update the live status display during a phase (called from within phases)."""
        if self._status_fn is not None:
            try:
                self._status_fn(msg)
            except Exception:
                pass


class Pipeline:
    """Runs all analysis phases against a Laravel project."""

    def __init__(self, project_root: Path, config: Config | None = None) -> None:
        self.project_root = project_root.resolve()
        self.config = config or Config.load(project_root)

    def run(
        self,
        full: bool = False,
        skip_embeddings: bool = False,
        phases: list[int] | None = None,
        on_phase_start: Any = None,
        on_phase_done: Any = None,
        on_phase_status: Any = None,
    ) -> PipelineContext:
        """Execute the full (or selective) pipeline.

        Args:
            full: Force full rebuild, ignoring any incremental state.
            skip_embeddings: Skip vector embedding generation (phase 12).
            phases: If set, run only the listed phase numbers.
            on_phase_start: Optional callback(idx, name, total, description) called before each phase.
            on_phase_done: Optional callback(idx, name, elapsed) called after each phase.
            on_phase_status: Optional callback(msg) wired to ctx.set_status() for live updates.
        """
        db_path = index_dir(self.project_root) / "graph.kuzu"

        if full and db_path.exists():
            import shutil
            shutil.rmtree(str(db_path), ignore_errors=True)
            logger.info("Full rebuild: cleared existing index", path=str(db_path))

        # On full rebuild also wipe the JSON caches so stale LLM annotations
        # and cached query results don't survive the schema reset.
        if full:
            _idx = index_dir(self.project_root)
            for cache_file in ("query_cache.json", "db_context.json"):
                _p = _idx / cache_file
                if _p.exists():
                    try:
                        _p.unlink()
                        logger.info("Full rebuild: cleared cache", file=cache_file)
                    except Exception as _e:
                        logger.warning("Could not clear cache file", file=cache_file, error=str(_e))

        # force_reinit=True drops all tables via Cypher before recreating them.
        # This is the authoritative schema reset — it works even when shutil.rmtree
        # couldn't fully remove the directory (e.g. KuzuDB lock files held by a
        # running MCP server process prevent deletion on some platforms).
        db = GraphDB(db_path, force_reinit=full)
        composer = parse_composer(self.project_root / "composer.json")

        ctx = PipelineContext(
            project_root=self.project_root,
            config=self.config,
            db=db,
            composer=composer,
        )

        if on_phase_status:
            ctx._status_fn = on_phase_status

        pipeline_logger.info(
            "Pipeline started",
            project=str(self.project_root),
            laravel_version=composer.laravel_version,
            php_constraint=composer.php_constraint,
            full=full,
        )

        # Import phases lazily to avoid circular imports
        from laravelgraph.pipeline import (
            phase_01_discovery,
            phase_02_structure,
            phase_03_ast,
            phase_04_imports,
            phase_05_calls,
            phase_06_heritage,
            phase_07_types,
            phase_08_community,
            phase_09_flows,
            phase_10_dead_code,
            phase_11_git,
            phase_12_embeddings,
            phase_13_eloquent,
            phase_14_routes,
            phase_15_middleware,
            phase_16_bindings,
            phase_17_events,
            phase_18_blade,
            phase_19_schema,
            phase_20_config,
            phase_21_di,
            phase_22_api,
            phase_23_schedule,
            phase_24_db_introspect,
            phase_25_model_table_link,
            phase_26_db_access,
            phase_27_features,
            phase_28_contracts,
            phase_29_change_intel,
            phase_30_test_coverage,
            phase_31_query_patterns,
            phase_32_http_clients,
            phase_33_notifications,
        )

        all_phases: list[tuple[int, str, Any, str]] = [
            (1,  "File Discovery",              phase_01_discovery.run,              "Scanning for PHP, Blade, config, and composer files"),
            (2,  "Structure",                   phase_02_structure.run,              "Extracting class/method/property definitions"),
            (3,  "AST Parsing",                 phase_03_ast.run,                   "Building abstract syntax trees for all PHP files"),
            (4,  "Import/Namespace Resolution", phase_04_imports.run,               "Resolving use statements and namespace aliases"),
            (5,  "Call Graph Tracing",          phase_05_calls.run,                 "Mapping method calls and static invocations"),
            (6,  "Heritage Analysis",           phase_06_heritage.run,              "Resolving extends/implements/trait inheritance"),
            (7,  "Type Analysis",               phase_07_types.run,                 "Inferring parameter and return types"),
            (8,  "Community Detection",         phase_08_community.run,             "Grouping related classes into logical communities"),
            (9,  "Execution Flow Detection",    phase_09_flows.run,                 "Detecting control flow and execution paths"),
            (11, "Change Coupling (Git)",       phase_11_git.run,                   "Mining git history for co-change patterns"),
            (12, "Embeddings",                  phase_12_embeddings.run if not skip_embeddings else _skip, "Generating semantic vector embeddings for search"),
            (13, "Eloquent Relationships",      phase_13_eloquent.run,              "Parsing model relationship methods (hasMany, belongsTo, etc.)"),
            (14, "Route Analysis",              phase_14_routes.run,                "Mapping HTTP routes to controllers and middleware"),
            (15, "Middleware Resolution",       phase_15_middleware.run,            "Resolving middleware chains and guard assignments"),
            (16, "Service Container Bindings",  phase_16_bindings.run,              "Indexing service provider bindings and singletons"),
            (17, "Event/Listener/Job Graph",    phase_17_events.run,                "Mapping events to listeners and queued jobs"),
            (18, "Blade Template Graph",        phase_18_blade.run,                 "Linking Blade templates to controllers and components"),
            # Phase 10 intentionally runs after phase 18 so that BLADE_CALLS edges
            # (created by phase 18) are present before dead-code detection fires.
            (10, "Dead Code Detection",         phase_10_dead_code.run,             "Identifying unreachable classes and methods"),
            (19, "Database Schema",             phase_19_schema.run,                "Parsing migration files to extract table/column schema"),
            (20, "Config/Env Dependencies",     phase_20_config.run,                "Finding all config() and env() usages"),
            (21, "Dependency Injection",        phase_21_di.run,                    "Resolving constructor injection and type-hints"),
            (22, "API Contract Analysis",       phase_22_api.run,                   "Extracting FormRequest rules and API resource shapes"),
            (23, "Scheduled Tasks",             phase_23_schedule.run,              "Indexing artisan schedule definitions"),
            # Phase 24 must run after 19 (migration schema) so live data can augment it.
            # Phase 25 must run after both 13 (Eloquent models) and 24 (live tables).
            # Phase 26 must run after 25 (needs table/column/model nodes all present).
            # Phase 27 must run after 14 (routes) and 13/17 (models/events/jobs).
            # Phase 28 must run after 13 (models), 14 (routes), 17 (events/jobs).
            # Phase 29 must run after 3 (AST) so class/method nodes exist.
            # Phase 30 must run after 14 (routes) so route nodes exist.
            # Phase 31 must run after 13 (Eloquent) so model nodes exist.
            (24, "Live DB Introspection",       phase_24_db_introspect.run,         "Connecting to live database — reading tables, columns, procedures, enums"),
            (25, "Model-Table Linking",         phase_25_model_table_link.run,      "Linking Eloquent models to their database tables"),
            (26, "DB Access Analysis",          phase_26_db_access.run,             "Tracing which methods read/write which tables"),
            (27, "Feature Clustering",          phase_27_features.run,              "Grouping routes/models/events into product features"),
            (28, "Contract Extraction",         phase_28_contracts.run,             "Extracting validation rules, policies, and observer hooks"),
            (29, "Change Intelligence",         phase_29_change_intel.run,          "Detecting recently changed files via git diff"),
            (30, "Test Coverage Mapping",       phase_30_test_coverage.run,         "Mapping test files to routes and classes they cover"),
            (31, "N+1 Query Pattern Detection", phase_31_query_patterns.run,        "Finding N+1 query patterns and missing eager loads"),
            # Phase 32 must run after phase 2/3 (symbol nodes exist) and 5 (fqn_index populated)
            # Phase 33 must run after phase 17 (Notification nodes created)
            (32, "External HTTP Client Detection", phase_32_http_clients.run,       "Detecting outbound Http::, Guzzle, and curl calls"),
            (33, "Notification Channel Enrichment", phase_33_notifications.run,     "Enriching notification channels and detecting Mailable classes"),
        ]

        import gc as _gc

        # After these phase numbers complete, release large in-memory caches
        # that are no longer needed by subsequent phases, to reduce peak RSS.
        #
        # parsed_php  — used by phases 5, 13, 17; safe to clear after 17
        # parsed_blade — used by phase 18; safe to clear after 18
        # php_files / blade_files / route_files — paths only, low cost, kept
        _CLEAR_PARSED_PHP_AFTER   = 17   # Event/Listener/Job Graph (last user)
        _CLEAR_PARSED_BLADE_AFTER = 18   # Blade Template Graph (last user)
        _GC_AFTER_PHASES          = {10, 12, 18}  # Dead Code, Embeddings, Blade

        active_phases = [(n, nm, fn, desc) for n, nm, fn, desc in all_phases if not phases or n in phases]
        total = len(active_phases)
        for idx, (phase_num, phase_name, phase_fn, phase_desc) in enumerate(active_phases, start=1):
            if on_phase_start:
                on_phase_start(idx, phase_name, total, phase_desc)

            t0 = time.time()
            with phase_timer(phase_name, extra_ctx=ctx.stats):
                try:
                    phase_fn(ctx)
                except Exception as e:
                    msg = f"Phase {phase_num} ({phase_name}) failed: {e}"
                    ctx.errors.append(msg)
                    logger.error(msg, phase=phase_num, exc_info=True)
            elapsed = time.time() - t0

            # ── Post-phase memory cleanup ─────────────────────────────────────
            if phase_num == _CLEAR_PARSED_PHP_AFTER and ctx.parsed_php:
                freed = len(ctx.parsed_php)
                ctx.parsed_php.clear()
                logger.debug("Cleared parsed_php cache after phase", phase=phase_num, entries=freed)

            if phase_num == _CLEAR_PARSED_BLADE_AFTER and ctx.parsed_blade:
                freed = len(ctx.parsed_blade)
                ctx.parsed_blade.clear()
                logger.debug("Cleared parsed_blade cache after phase", phase=phase_num, entries=freed)

            if phase_num in _GC_AFTER_PHASES:
                _gc.collect()
                logger.debug("GC collect after phase", phase=phase_num)

            if on_phase_done:
                on_phase_done(idx, phase_name, elapsed)

        # Load project-specific pipeline plugins
        from laravelgraph.plugins.loader import load_pipeline_plugins
        _plugins_dir = self.project_root / ".laravelgraph" / "plugins"
        if _plugins_dir.exists():
            _loaded = load_pipeline_plugins(_plugins_dir, ctx, logger)
            if _loaded:
                logger.info("Pipeline plugins loaded", plugins=_loaded)

        # Register in global registry
        registry = Registry()
        registry.register(
            self.project_root,
            laravel_version=composer.laravel_version,
            php_version=composer.php_constraint,
            stats=dict(ctx.stats),
        )

        elapsed = time.time() - ctx.start_time
        pipeline_logger.info(
            "Pipeline completed",
            project=str(self.project_root),
            duration_sec=round(elapsed, 2),
            stats=ctx.stats,
            errors=len(ctx.errors),
        )

        return ctx


def _skip(ctx: PipelineContext) -> None:
    """No-op phase (e.g. embeddings when --no-embeddings is passed)."""
    pipeline_logger.info("Phase skipped")
