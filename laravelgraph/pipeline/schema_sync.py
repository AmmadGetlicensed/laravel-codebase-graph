"""schema_sync — Auto-extend REL_TYPES before GraphDB initialization.

Scans all pipeline phase_*.py files (and optional extra dirs, e.g. project plugins)
via AST to find every upsert_rel(rel_type, from_label, ..., to_label, ...) call.
Any (from_label, to_label) pair not already present in schema.REL_TYPES is appended
to the in-memory list *before* GraphDB._init_schema() runs.

This means the KuzuDB CREATE REL TABLE DDL always includes every pair that the
pipeline actually uses — eliminating "Binder exception: Query node violates schema"
warnings without any manual schema maintenance.

No files are written. The mutation is in-memory only and lasts for the lifetime of
the current process (i.e. one analyze run).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import laravelgraph.core.schema as _schema
from laravelgraph.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Directory containing this file — the built-in pipeline phases live here.
_PIPELINE_DIR = Path(__file__).parent


def _scan_dir(directory: Path, glob: str = "phase_*.py") -> dict[str, set[tuple[str, str]]]:
    """
    AST-scan files matching *glob* in *directory* for upsert_rel calls.

    Matches both forms:
        db.upsert_rel("REL", "FromLabel", from_nid, "ToLabel", to_nid, ...)
        upsert_rel("REL", "FromLabel", from_nid, "ToLabel", to_nid, ...)

    Returns {rel_type: {(from_label, to_label), ...}}.
    Only captures calls where args 0, 1, and 3 are string literals so we never
    produce false positives from dynamic calls.
    """
    found: dict[str, set[tuple[str, str]]] = {}
    for py_file in sorted(directory.glob(glob)):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            logger.debug("schema_sync: skipping unparseable file", file=str(py_file))
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_upsert = (
                (isinstance(func, ast.Name) and func.id == "upsert_rel")
                or (isinstance(func, ast.Attribute) and func.attr == "upsert_rel")
            )
            if not is_upsert or len(node.args) < 4:
                continue
            args = node.args
            if not all(isinstance(args[i], ast.Constant) for i in (0, 1, 3)):
                continue
            rel_type: str = args[0].value
            from_label: str = args[1].value
            to_label: str = args[3].value
            found.setdefault(rel_type, set()).add((from_label, to_label))
    return found


def sync_schema(extra_scan_dirs: list[Path] | None = None) -> int:
    """
    Mutate schema.REL_TYPES in-memory so it includes every (from_label, to_label)
    pair that appears in any scanned upsert_rel call.

    Scans:
      - All built-in pipeline phase_*.py files (laravelgraph/pipeline/)
      - Any directories in *extra_scan_dirs* (e.g. project .laravelgraph/plugins/)

    Returns the number of pairs added (0 means schema was already complete).

    Raises nothing — if an unknown rel_type is encountered (e.g. from a plugin that
    creates a brand-new relationship type), a warning is logged and it is skipped.
    Adding new relationship types requires a manual schema.py edit because the
    property list cannot be inferred from a call site alone.
    """
    # Collect all pairs across all scan targets.
    # Pipeline phases use the phase_*.py naming convention; plugin files don't.
    used: dict[str, set[tuple[str, str]]] = {}
    for rel_type, pairs in _scan_dir(_PIPELINE_DIR, glob="phase_*.py").items():
        used.setdefault(rel_type, set()).update(pairs)
    for scan_dir in extra_scan_dirs or []:
        if scan_dir.exists():
            for rel_type, pairs in _scan_dir(scan_dir, glob="*.py").items():
                used.setdefault(rel_type, set()).update(pairs)

    if not used:
        return 0

    # Build a name→index map of the current REL_TYPES list
    rel_index: dict[str, int] = {name: i for i, (name, _, _) in enumerate(_schema.REL_TYPES)}

    added = 0
    for rel_type, pairs in used.items():
        if rel_type not in rel_index:
            logger.warning(
                "schema_sync: upsert_rel uses unknown rel type — "
                "add it to schema.REL_TYPES manually with its property list",
                rel_type=rel_type,
            )
            continue

        idx = rel_index[rel_type]
        _name, existing_pairs, _props = _schema.REL_TYPES[idx]
        # existing_pairs is a mutable list — safe to append
        existing_set: set[tuple[str, str]] = {tuple(p) for p in existing_pairs}  # type: ignore[misc]

        for pair in pairs:
            if pair not in existing_set:
                existing_pairs.append(list(pair))
                existing_set.add(pair)
                added += 1
                logger.info(
                    "schema_sync: added missing rel pair",
                    rel_type=rel_type,
                    from_label=pair[0],
                    to_label=pair[1],
                )

    return added
