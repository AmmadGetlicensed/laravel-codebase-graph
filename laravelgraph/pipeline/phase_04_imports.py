"""Phase 04 — Import / Namespace Resolution.

Resolve PHP `use` statements and create IMPORTS relationships between files.
Also enriches ctx.fqn_index with per-file alias → FQN mappings stored as a
side-table at ctx._use_aliases[str(filepath)] = {alias: fqn}.
"""

from __future__ import annotations

import json
from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.parsers.php import PHPFile, ParsedUse
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


def run(ctx: PipelineContext) -> None:
    """Walk every parsed PHP file's use statements and build IMPORTS edges."""
    with phase_timer("Import/Namespace Resolution"):
        db = ctx.db
        project_root = ctx.project_root

        imports_resolved = 0
        imports_unresolved = 0

        # Build a reverse map: absolute_path_str → node_id for File nodes
        file_nid_map: dict[str, str] = {}
        for fp in ctx.all_files:
            rel_str = _rel(fp, project_root)
            file_nid_map[str(fp)] = make_node_id("file", rel_str)

        # Per-file alias table (populated here, consumed in phase 05/07)
        use_aliases: dict[str, dict[str, str]] = {}

        for path_str, parsed in ctx.parsed_php.items():
            filepath = Path(path_str)
            rel_str = _rel(filepath, project_root)
            from_file_nid = file_nid_map.get(path_str)
            if not from_file_nid:
                continue

            # Build alias table for this file
            aliases: dict[str, str] = {}  # alias → fully-qualified name
            grouped: dict[str, list[str]] = {}  # target_file_nid → [alias, ...]

            for use_stmt in parsed.uses:
                fqn = use_stmt.fqn
                alias = use_stmt.alias
                aliases[alias] = fqn

                # Also register fqn itself into the global fqn_index if not already there
                if fqn not in ctx.fqn_index:
                    # Try to find the node_id from class_map path
                    if fqn in ctx.class_map:
                        target_path = ctx.class_map[fqn]
                        target_rel = _rel(target_path, project_root)
                        ctx.fqn_index[fqn] = make_node_id("class", fqn)

                # Resolve to a File node if possible
                target_file_nid = _resolve_to_file(fqn, ctx, file_nid_map)

                if target_file_nid:
                    if target_file_nid not in grouped:
                        grouped[target_file_nid] = []
                    grouped[target_file_nid].append(alias)
                    imports_resolved += 1
                else:
                    imports_unresolved += 1

            # Create one IMPORTS edge per unique target file, with all aliases bundled
            for target_file_nid, alias_list in grouped.items():
                if target_file_nid == from_file_nid:
                    continue  # skip self-imports
                try:
                    db.upsert_rel(
                        "IMPORTS",
                        "File", from_file_nid,
                        "File", target_file_nid,
                        props={
                            "alias": alias_list[0] if len(alias_list) == 1 else "",
                            "symbols": json.dumps(alias_list),
                            "line": 0,
                        },
                    )
                except Exception as e:
                    logger.debug(
                        "IMPORTS edge failed",
                        from_file=rel_str,
                        to=target_file_nid,
                        error=str(e),
                    )

            use_aliases[path_str] = aliases

        # Attach alias table to ctx for downstream phases
        ctx._use_aliases = use_aliases  # type: ignore[attr-defined]

        ctx.stats["imports_resolved"] = imports_resolved
        ctx.stats["imports_unresolved"] = imports_unresolved

        logger.info(
            "Import resolution complete",
            resolved=imports_resolved,
            unresolved=imports_unresolved,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_to_file(
    fqn: str,
    ctx: PipelineContext,
    file_nid_map: dict[str, str],
) -> str | None:
    """Try to find the File node_id for a given FQN."""
    # 1. Direct class_map lookup
    if fqn in ctx.class_map:
        path = ctx.class_map[fqn]
        nid = file_nid_map.get(str(path))
        if nid:
            return nid

    # 2. Walk psr4 prefixes to find the file path
    project_root = ctx.project_root
    all_psr4 = ctx.composer.psr4_mappings + ctx.composer.psr4_dev_mappings
    for mapping in all_psr4:
        ns = mapping.namespace.rstrip("\\")
        if fqn.startswith(ns + "\\") or fqn == ns:
            suffix = fqn[len(ns):].lstrip("\\")
            candidate = project_root / mapping.path / suffix.replace("\\", "/")
            candidate_php = candidate.with_suffix(".php")
            nid = file_nid_map.get(str(candidate_php))
            if nid:
                return nid

    return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)
