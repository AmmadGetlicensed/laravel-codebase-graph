# AGENTS.md — laravelgraph/pipeline/

## OVERVIEW

26-phase analysis engine. Each phase is a standalone module with a single `run(ctx: PipelineContext) -> None` function. Phases share state through `PipelineContext` — a dataclass defined in `orchestrator.py`.

## STRUCTURE

```
pipeline/
├── orchestrator.py      # Pipeline class, PipelineContext dataclass, lazy phase imports
└── phase_NN_name.py     # One file per phase (01–26), each exports run(ctx)
```

## PHASE ORDERING (CRITICAL)

Deviations from numeric order — see `orchestrator.py` `all_phases` list:
- **Phase 10 (dead code) runs AFTER phase 18 (Blade)** — needs `BLADE_CALLS` edges to exist first.
- **Phase 24 → 25 → 26** strictly ordered: introspect DB → link models to tables → analyze DB access.
- Phases 1–9 then 11–23 run in numeric order otherwise.

## PipelineContext Fields

| Field | Type | Populated by |
|-------|------|-------------|
| `project_root` | `Path` | Constructor |
| `config` | `Config` | Constructor |
| `db` | `GraphDB` | Constructor |
| `composer` | `ComposerInfo` | Constructor |
| `php_files` | `list[Path]` | Phase 1 |
| `blade_files` | `list[Path]` | Phase 1 |
| `route_files` | `list[Path]` | Phase 1 |
| `migration_files` | `list[Path]` | Phase 1 |
| `all_files` | `list[Path]` | Phase 1 |
| `class_map` | `dict[str, Path]` | Phase 4 (FQN → path) |
| `parsed_php` | `dict[str, Any]` | Phase 3 (path → PHPFile) |
| `parsed_blade` | `dict[str, Any]` | Phase 18 (path → BladeParsed) |
| `fqn_index` | `dict[str, str]` | Phase 3 (FQN → node_id) |
| `route_nodes` | `list[dict]` | Phase 14 |
| `stats` | `dict[str, int]` | All phases (accumulate counts) |
| `errors` | `list[str]` | All phases (non-fatal errors) |

## CONVENTIONS

- Every phase module: `from __future__ import annotations` at top.
- Phase function signature: exactly `def run(ctx: PipelineContext) -> None`.
- Graph writes via `ctx.db.upsert_node()` / `ctx.db.upsert_edge()` only — never raw Cypher.
- Use `ctx.stats["key"] = ctx.stats.get("key", 0) + N` to accumulate counts.
- Errors go to `ctx.errors.append(msg)` — never raise from a phase (pipeline must continue).
- Logging: `pipeline_logger = get_pipeline_logger()` from `laravelgraph.logging`.

## ANTI-PATTERNS

- **Do not import phase modules at module level** — lazy imports inside `Pipeline.run()` only (circular import prevention).
- **Do not query KuzuDB in phases** — phases write only; read happens in MCP server.
- **Do not reorder phases** without updating the comment block in `orchestrator.py`.
- **Do not cache phase results across `Pipeline.run()` calls** — `PipelineContext` is per-run.

## ADDING A PHASE

1. Create `phase_NN_name.py` with `def run(ctx: PipelineContext) -> None`.
2. Add lazy import in `orchestrator.py` `Pipeline.run()` import block.
3. Insert `(NN, "Phase Name", phase_NN_name.run)` into `all_phases` at the correct position.
4. Update ordering comments if it has dependencies on other phases.
5. Add test in `tests/integration/pipeline/` or `tests/unit/pipeline/`.
