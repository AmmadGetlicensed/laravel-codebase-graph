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
    ) -> PipelineContext:
        """Execute the full (or selective) pipeline.

        Args:
            full: Force full rebuild, ignoring any incremental state.
            skip_embeddings: Skip vector embedding generation (phase 12).
            phases: If set, run only the listed phase numbers.
            on_phase_start: Optional callback(idx, name) called before each phase.
            on_phase_done: Optional callback(idx, name, elapsed) called after each phase.
        """
        db_path = index_dir(self.project_root) / "graph.kuzu"

        if full and db_path.exists():
            import shutil
            shutil.rmtree(str(db_path), ignore_errors=True)
            logger.info("Full rebuild: cleared existing index", path=str(db_path))

        db = GraphDB(db_path)
        composer = parse_composer(self.project_root / "composer.json")

        ctx = PipelineContext(
            project_root=self.project_root,
            config=self.config,
            db=db,
            composer=composer,
        )

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
        )

        all_phases: list[tuple[int, str, Any]] = [
            (1,  "File Discovery",              phase_01_discovery.run),
            (2,  "Structure",                   phase_02_structure.run),
            (3,  "AST Parsing",                 phase_03_ast.run),
            (4,  "Import/Namespace Resolution", phase_04_imports.run),
            (5,  "Call Graph Tracing",          phase_05_calls.run),
            (6,  "Heritage Analysis",           phase_06_heritage.run),
            (7,  "Type Analysis",               phase_07_types.run),
            (8,  "Community Detection",         phase_08_community.run),
            (9,  "Execution Flow Detection",    phase_09_flows.run),
            (10, "Dead Code Detection",         phase_10_dead_code.run),
            (11, "Change Coupling (Git)",       phase_11_git.run),
            (12, "Embeddings",                  phase_12_embeddings.run if not skip_embeddings else _skip),
            (13, "Eloquent Relationships",      phase_13_eloquent.run),
            (14, "Route Analysis",              phase_14_routes.run),
            (15, "Middleware Resolution",       phase_15_middleware.run),
            (16, "Service Container Bindings",  phase_16_bindings.run),
            (17, "Event/Listener/Job Graph",    phase_17_events.run),
            (18, "Blade Template Graph",        phase_18_blade.run),
            (19, "Database Schema",             phase_19_schema.run),
            (20, "Config/Env Dependencies",     phase_20_config.run),
            (21, "Dependency Injection",        phase_21_di.run),
            (22, "API Contract Analysis",       phase_22_api.run),
            (23, "Scheduled Tasks",             phase_23_schedule.run),
        ]

        active_phases = [(n, nm, fn) for n, nm, fn in all_phases if not phases or n in phases]
        total = len(active_phases)
        for idx, (phase_num, phase_name, phase_fn) in enumerate(active_phases, start=1):
            if on_phase_start:
                on_phase_start(idx, phase_name, total)

            t0 = time.time()
            with phase_timer(phase_name, extra_ctx=ctx.stats):
                try:
                    phase_fn(ctx)
                except Exception as e:
                    msg = f"Phase {phase_num} ({phase_name}) failed: {e}"
                    ctx.errors.append(msg)
                    logger.error(msg, phase=phase_num, exc_info=True)
            elapsed = time.time() - t0

            if on_phase_done:
                on_phase_done(idx, phase_name, elapsed)

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
