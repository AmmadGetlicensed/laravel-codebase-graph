"""Phase 07 — Type Analysis.

Emit USES_TYPE edges from Method and Property nodes to the Class_ (or
Interface_/Trait_) nodes they reference in parameter types, return types,
and property type hints.

Handles:
  - Nullable types: ?Foo → Foo
  - Union types:    Foo|Bar → [Foo, Bar]
  - Intersection:   Foo&Bar → [Foo, Bar]
  - Scalar / built-in types are silently skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Scalar / pseudo-types that should never produce USES_TYPE edges
_SCALAR_TYPES = frozenset({
    "int", "integer", "float", "double", "string", "bool", "boolean",
    "null", "void", "never", "array", "callable", "iterable", "mixed",
    "object", "resource", "self", "static", "parent", "$this",
    "true", "false",
})

_TYPE_SEPARATOR = re.compile(r"[|&]")


def _split_type(type_hint: str) -> list[str]:
    """Split a PHP type string into individual atomic type names."""
    cleaned = type_hint.strip().lstrip("?")
    # Remove leading backslash from FQNs
    parts = [t.strip().lstrip("\\") for t in _TYPE_SEPARATOR.split(cleaned)]
    return [p for p in parts if p and p not in _SCALAR_TYPES]


def _resolve_type(
    type_name: str,
    file_aliases: dict[str, str],
    ctx: PipelineContext,
) -> str | None:
    """Return the node_id for a type name, or None if unresolvable."""
    # 1. Use-statement alias lookup
    fqn = file_aliases.get(type_name, type_name)

    # 2. Direct fqn_index hit
    nid = ctx.fqn_index.get(fqn)
    if nid:
        return nid

    # 3. If name contains backslash (partial/full FQN already), try as-is
    if "\\" in type_name:
        nid = ctx.fqn_index.get(type_name)
        if nid:
            return nid

    return None


def _label_for(nid: str) -> str:
    if nid.startswith("interface:"):
        return "Interface_"
    if nid.startswith("trait:"):
        return "Trait_"
    return "Class_"


def run(ctx: PipelineContext) -> None:
    """Walk all parsed symbols and emit USES_TYPE edges."""
    with phase_timer("Type Analysis"):
        db = ctx.db
        use_aliases: dict[str, dict[str, str]] = getattr(ctx, "_use_aliases", {})
        type_edges = 0

        for path_str, parsed in ctx.parsed_php.items():
            file_aliases = use_aliases.get(path_str, {})

            # ── Classes: method params, return types, property types ──────
            for cls in parsed.classes:
                class_fqn = cls.fqn

                # Method-level types
                for method in cls.methods:
                    method_fqn = f"{class_fqn}::{method.name}"
                    method_nid = ctx.fqn_index.get(method_fqn)
                    if not method_nid:
                        continue

                    # Return type
                    if method.return_type:
                        for type_name in _split_type(method.return_type):
                            target_nid = _resolve_type(type_name, file_aliases, ctx)
                            if target_nid:
                                try:
                                    db.upsert_rel(
                                        "USES_TYPE",
                                        "Method", method_nid,
                                        _label_for(target_nid), target_nid,
                                        props={"role": "return", "line": method.line_start},
                                    )
                                    type_edges += 1
                                except Exception as e:
                                    logger.debug(
                                        "USES_TYPE return edge failed",
                                        method=method_fqn,
                                        type=type_name,
                                        error=str(e),
                                    )

                    # Parameter types
                    for param in method.params:
                        if not param.type_hint:
                            continue
                        for type_name in _split_type(param.type_hint):
                            target_nid = _resolve_type(type_name, file_aliases, ctx)
                            if target_nid:
                                try:
                                    db.upsert_rel(
                                        "USES_TYPE",
                                        "Method", method_nid,
                                        _label_for(target_nid), target_nid,
                                        props={"role": "param", "line": method.line_start},
                                    )
                                    type_edges += 1
                                except Exception as e:
                                    logger.debug(
                                        "USES_TYPE param edge failed",
                                        method=method_fqn,
                                        type=type_name,
                                        error=str(e),
                                    )

                # Property types — emit from Class_ node (no Property node exists yet)
                class_nid = ctx.fqn_index.get(class_fqn)
                for prop in cls.properties:
                    if not prop.type_hint:
                        continue
                    for type_name in _split_type(prop.type_hint):
                        target_nid = _resolve_type(type_name, file_aliases, ctx)
                        if target_nid and class_nid:
                            try:
                                db.upsert_rel(
                                    "USES_TYPE",
                                    "Class_", class_nid,
                                    _label_for(target_nid), target_nid,
                                    props={"role": "property", "line": prop.line},
                                )
                                type_edges += 1
                            except Exception as e:
                                logger.debug(
                                    "USES_TYPE property edge failed",
                                    cls=class_fqn,
                                    prop=prop.name,
                                    type=type_name,
                                    error=str(e),
                                )

            # ── Traits: same logic ────────────────────────────────────────
            for trait in parsed.traits:
                for method in trait.methods:
                    method_fqn = f"{trait.fqn}::{method.name}"
                    method_nid = ctx.fqn_index.get(method_fqn)
                    if not method_nid:
                        continue

                    if method.return_type:
                        for type_name in _split_type(method.return_type):
                            target_nid = _resolve_type(type_name, file_aliases, ctx)
                            if target_nid:
                                try:
                                    db.upsert_rel(
                                        "USES_TYPE",
                                        "Method", method_nid,
                                        _label_for(target_nid), target_nid,
                                        props={"role": "return", "line": method.line_start},
                                    )
                                    type_edges += 1
                                except Exception as e:
                                    logger.debug("USES_TYPE trait method failed", error=str(e))

                    for param in method.params:
                        if not param.type_hint:
                            continue
                        for type_name in _split_type(param.type_hint):
                            target_nid = _resolve_type(type_name, file_aliases, ctx)
                            if target_nid:
                                try:
                                    db.upsert_rel(
                                        "USES_TYPE",
                                        "Method", method_nid,
                                        _label_for(target_nid), target_nid,
                                        props={"role": "param", "line": method.line_start},
                                    )
                                    type_edges += 1
                                except Exception as e:
                                    logger.debug("USES_TYPE trait param failed", error=str(e))

            # ── Free functions ────────────────────────────────────────────
            for fn in parsed.functions:
                fn_nid = ctx.fqn_index.get(fn.fqn)
                if not fn_nid:
                    continue

                if fn.return_type:
                    for type_name in _split_type(fn.return_type):
                        target_nid = _resolve_type(type_name, file_aliases, ctx)
                        if target_nid:
                            try:
                                db.upsert_rel(
                                    "USES_TYPE",
                                    "Function_", fn_nid,
                                    _label_for(target_nid), target_nid,
                                    props={"role": "return", "line": fn.line_start},
                                )
                                type_edges += 1
                            except Exception as e:
                                logger.debug("USES_TYPE fn return failed", error=str(e))

                for param in fn.params:
                    if not param.type_hint:
                        continue
                    for type_name in _split_type(param.type_hint):
                        target_nid = _resolve_type(type_name, file_aliases, ctx)
                        if target_nid:
                            try:
                                db.upsert_rel(
                                    "USES_TYPE",
                                    "Function_", fn_nid,
                                    _label_for(target_nid), target_nid,
                                    props={"role": "param", "line": fn.line_start},
                                )
                                type_edges += 1
                            except Exception as e:
                                logger.debug("USES_TYPE fn param failed", error=str(e))

        ctx.stats["type_edges"] = type_edges
        logger.info("Type analysis complete", type_edges=type_edges)
