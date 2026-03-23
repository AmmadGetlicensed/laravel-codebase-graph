"""Phase 06 — Heritage Analysis.

Build class inheritance (EXTENDS_CLASS), interface implementation
(IMPLEMENTS_INTERFACE), and trait usage (USES_TRAIT) edges.
"""

from __future__ import annotations

from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


def run(ctx: PipelineContext) -> None:
    """Emit heritage edges for all parsed PHP classes, traits, and interfaces."""
    with phase_timer("Heritage Analysis"):
        db = ctx.db

        inheritance_edges = 0
        interface_edges = 0
        trait_edges = 0

        use_aliases: dict[str, dict[str, str]] = getattr(ctx, "_use_aliases", {})

        for path_str, parsed in ctx.parsed_php.items():
            file_aliases = use_aliases.get(path_str, {})

            # ── Classes ──────────────────────────────────────────────────
            for cls in parsed.classes:
                class_nid = ctx.fqn_index.get(cls.fqn)
                if not class_nid:
                    continue

                # EXTENDS_CLASS
                if cls.extends:
                    super_nid = _resolve_symbol(
                        cls.extends, file_aliases, ctx, prefer="class"
                    )
                    if super_nid:
                        try:
                            db.upsert_rel(
                                "EXTENDS_CLASS",
                                "Class_", class_nid,
                                _label_for(super_nid), super_nid,
                            )
                            inheritance_edges += 1
                        except Exception as e:
                            logger.debug(
                                "EXTENDS_CLASS edge failed",
                                cls=cls.fqn,
                                super=cls.extends,
                                error=str(e),
                            )

                # IMPLEMENTS_INTERFACE
                for iface_name in cls.implements:
                    iface_nid = _resolve_symbol(
                        iface_name, file_aliases, ctx, prefer="interface"
                    )
                    if iface_nid:
                        try:
                            db.upsert_rel(
                                "IMPLEMENTS_INTERFACE",
                                "Class_", class_nid,
                                _label_for(iface_nid), iface_nid,
                            )
                            interface_edges += 1
                        except Exception as e:
                            logger.debug(
                                "IMPLEMENTS_INTERFACE edge failed",
                                cls=cls.fqn,
                                iface=iface_name,
                                error=str(e),
                            )

                # USES_TRAIT (class body `use Foo, Bar;`)
                for trait_name in cls.traits:
                    trait_nid = _resolve_symbol(
                        trait_name, file_aliases, ctx, prefer="trait"
                    )
                    if trait_nid:
                        try:
                            db.upsert_rel(
                                "USES_TRAIT",
                                "Class_", class_nid,
                                _label_for(trait_nid), trait_nid,
                                props={"line": 0},
                            )
                            trait_edges += 1
                        except Exception as e:
                            logger.debug(
                                "USES_TRAIT (class) edge failed",
                                cls=cls.fqn,
                                trait=trait_name,
                                error=str(e),
                            )

            # ── Traits ───────────────────────────────────────────────────
            for trait in parsed.traits:
                trait_nid = ctx.fqn_index.get(trait.fqn)
                if not trait_nid:
                    continue

                for used_trait in trait.traits:
                    used_nid = _resolve_symbol(
                        used_trait, file_aliases, ctx, prefer="trait"
                    )
                    if used_nid:
                        try:
                            db.upsert_rel(
                                "USES_TRAIT",
                                "Trait_", trait_nid,
                                _label_for(used_nid), used_nid,
                                props={"line": 0},
                            )
                            trait_edges += 1
                        except Exception as e:
                            logger.debug(
                                "USES_TRAIT (trait) edge failed",
                                trait=trait.fqn,
                                used=used_trait,
                                error=str(e),
                            )

            # ── Interfaces ───────────────────────────────────────────────
            for iface in parsed.interfaces:
                iface_nid = ctx.fqn_index.get(iface.fqn)
                if not iface_nid:
                    continue

                # Interface extends
                for parent_name in iface.extends:
                    parent_nid = _resolve_symbol(
                        parent_name, file_aliases, ctx, prefer="interface"
                    )
                    if parent_nid:
                        try:
                            db.upsert_rel(
                                "EXTENDS_CLASS",
                                "Interface_", iface_nid,
                                _label_for(parent_nid), parent_nid,
                            )
                            inheritance_edges += 1
                        except Exception as e:
                            logger.debug(
                                "Interface EXTENDS edge failed",
                                iface=iface.fqn,
                                parent=parent_name,
                                error=str(e),
                            )

        ctx.stats["inheritance_edges"] = inheritance_edges
        ctx.stats["interface_edges"] = interface_edges
        ctx.stats["trait_edges"] = trait_edges

        logger.info(
            "Heritage analysis complete",
            inheritance=inheritance_edges,
            interfaces=interface_edges,
            traits=trait_edges,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_symbol(
    name: str,
    file_aliases: dict[str, str],
    ctx: PipelineContext,
    prefer: str = "class",
) -> str | None:
    """Resolve a short or fully-qualified name to a node_id.

    Checks use-statement aliases first, then fqn_index.
    """
    # 1. Alias resolution
    fqn = file_aliases.get(name, name)

    # 2. Direct lookup by preferred type prefix
    prefixed = make_node_id(prefer, fqn)
    if prefixed in _nid_values(ctx):
        return prefixed

    # 3. General fqn_index lookup
    nid = ctx.fqn_index.get(fqn)
    if nid:
        return nid

    # 4. If name contains backslash, try as-is
    if "\\" in name:
        nid = ctx.fqn_index.get(name)
        if nid:
            return nid

    return None


_fqn_index_value_set: set[str] | None = None


def _nid_values(ctx: PipelineContext) -> set[str]:
    """Return the set of all known node_id values (the fqn_index values)."""
    return set(ctx.fqn_index.values())


def _label_for(node_id: str) -> str:
    if node_id.startswith("class:"):
        return "Class_"
    if node_id.startswith("interface:"):
        return "Interface_"
    if node_id.startswith("trait:"):
        return "Trait_"
    return "Class_"
