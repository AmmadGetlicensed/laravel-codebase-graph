"""Phase 08 — Community Detection.

Run the Leiden community-detection algorithm on the CALLS graph.
Each detected community gets a Community node, and every Class_ / Method
that participates in the call graph gets a MEMBER_OF edge and a
community_id update.

Requires: python-igraph and leidenalg.
If either is absent the phase logs a warning and exits cleanly.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


def run(ctx: PipelineContext) -> None:
    """Detect communities in the call graph and write Community nodes."""
    with phase_timer("Community Detection"):
        try:
            import igraph as ig
            import leidenalg
        except ImportError:
            logger.warning(
                "igraph or leidenalg not available; skipping community detection. "
                "Install with: pip install python-igraph leidenalg"
            )
            ctx.stats["communities_detected"] = 0
            return

        db = ctx.db

        # ── 1. Fetch all CALLS edges ──────────────────────────────────────
        try:
            result = db.execute(
                "MATCH (a)-[:CALLS]->(b) RETURN a.node_id AS from_id, b.node_id AS to_id"
            )
        except Exception as e:
            logger.error("Failed to query CALLS edges for community detection", error=str(e))
            ctx.stats["communities_detected"] = 0
            return

        if not result:
            logger.info("No CALLS edges found; skipping community detection")
            ctx.stats["communities_detected"] = 0
            return

        # ── 2. Build igraph ───────────────────────────────────────────────
        # Collect unique vertex names
        vertex_set: set[str] = set()
        edge_pairs: list[tuple[str, str]] = []

        for row in result:
            from_id = row.get("from_id") or row.get("a.node_id", "")
            to_id = row.get("to_id") or row.get("b.node_id", "")
            if from_id and to_id:
                vertex_set.add(from_id)
                vertex_set.add(to_id)
                edge_pairs.append((from_id, to_id))

        if not vertex_set:
            logger.info("No vertices in call graph; skipping community detection")
            ctx.stats["communities_detected"] = 0
            return

        vertices = sorted(vertex_set)
        v_index: dict[str, int] = {v: i for i, v in enumerate(vertices)}

        g = ig.Graph(directed=False)
        g.add_vertices(len(vertices))
        g.vs["name"] = vertices

        edges_int = [
            (v_index[f], v_index[t])
            for f, t in edge_pairs
            if f in v_index and t in v_index
        ]
        g.add_edges(edges_int)

        # ── 3. Leiden partitioning ────────────────────────────────────────
        try:
            partition = leidenalg.find_partition(
                g, leidenalg.ModularityVertexPartition
            )
        except Exception as e:
            logger.error("Leiden partitioning failed", error=str(e))
            ctx.stats["communities_detected"] = 0
            return

        communities_detected = 0

        for community_id, member_indices in enumerate(partition):
            if not member_indices:
                continue

            member_nids = [vertices[i] for i in member_indices]
            size = len(member_nids)

            # Auto-label: pick the most common namespace from members
            label = _auto_label(member_nids, ctx)

            community_nid = make_node_id("community", str(community_id))

            # Insert Community node
            try:
                db._insert_node("Community", {
                    "node_id": community_nid,
                    "community_id": community_id,
                    "size": size,
                    "label": label,
                })
                communities_detected += 1
            except Exception as e:
                logger.debug(
                    "Community node insert failed",
                    community_id=community_id,
                    error=str(e),
                )
                continue

            # Create MEMBER_OF edges and update community_id on member nodes
            for nid in member_nids:
                node_label = _label_for_nid(nid)
                if not node_label:
                    continue

                try:
                    db.upsert_rel(
                        "MEMBER_OF",
                        node_label, nid,
                        "Community", community_nid,
                    )
                except Exception as e:
                    logger.debug(
                        "MEMBER_OF edge failed",
                        nid=nid,
                        community_id=community_id,
                        error=str(e),
                    )

                # Update community_id property on the node itself
                try:
                    escaped_nid = nid.replace("'", "\\'")
                    db._conn.execute(
                        f"MATCH (n:{node_label} {{node_id: '{escaped_nid}'}}) "
                        f"SET n.community_id = {community_id}"
                    )
                except Exception as e:
                    logger.debug(
                        "community_id update failed",
                        nid=nid,
                        error=str(e),
                    )

        ctx.stats["communities_detected"] = communities_detected
        logger.info(
            "Community detection complete",
            communities=communities_detected,
            vertices=len(vertices),
            edges=len(edges_int),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auto_label(member_nids: list[str], ctx: PipelineContext) -> str:
    """Generate a human-readable label from the most common namespace in the community."""
    namespaces: list[str] = []
    for nid in member_nids:
        # Derive FQN from the node_id (e.g. "class:App\\Http\\Controllers\\Foo")
        fqn = _fqn_from_nid(nid)
        if fqn:
            ns = "\\".join(fqn.split("\\")[:-1])
            if ns:
                namespaces.append(ns)

    if not namespaces:
        return f"Community {member_nids[0][:30] if member_nids else '?'}"

    most_common_ns, _ = Counter(namespaces).most_common(1)[0]
    # Shorten: take last two segments
    parts = most_common_ns.split("\\")
    short = "\\".join(parts[-2:]) if len(parts) >= 2 else most_common_ns
    return short


def _fqn_from_nid(nid: str) -> str:
    """Strip the type prefix from a node_id to get the FQN-like part."""
    for prefix in ("class:", "method:", "function:", "trait:", "interface:"):
        if nid.startswith(prefix):
            raw = nid[len(prefix):]
            # method node_ids are "method:FQN::methodName" — take everything before "::"
            if prefix == "method:" and "::" in raw:
                raw = raw.split("::")[0]
            return raw
    return ""


def _label_for_nid(nid: str) -> str | None:
    """Return the Kuzu node label for a node_id, or None if unknown."""
    if nid.startswith("class:"):
        return "Class_"
    if nid.startswith("method:"):
        return "Method"
    if nid.startswith("function:"):
        return "Function_"
    if nid.startswith("trait:"):
        return "Trait_"
    return None
