"""Phase 09 — Execution Flow Detection.

Detect entry points (routes, commands, jobs, listeners, middleware) and trace
execution flows via BFS through CALLS edges, creating Process nodes and
STEP_IN_PROCESS relationships.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

_MAX_BFS_NODES = 200
_DEFAULT_BFS_DEPTH = 5

# Laravel entry-point class roles
_COMMAND_ROLES = {"command"}
_JOB_ROLES = {"job"}
_LISTENER_ROLES = {"listener"}
_MIDDLEWARE_ROLES = {"middleware"}


def _collect_entry_points(ctx: PipelineContext) -> list[dict[str, Any]]:
    """Return a list of dicts with keys: entry_type, entry_fqn, method_node_id."""
    db = ctx.db
    entries: list[dict[str, Any]] = []

    # 1. Routes → controller methods already bound via ROUTES_TO edges
    try:
        rows = db.execute(
            "MATCH (r:Route)-[:ROUTES_TO]->(m:Method) "
            "RETURN r.node_id AS route_id, m.fqn AS method_fqn, m.node_id AS method_nid"
        )
        for row in rows:
            fqn = row.get("method_fqn") or ""
            nid = row.get("method_nid") or ""
            if fqn and nid:
                entries.append({"entry_type": "route", "entry_fqn": fqn, "method_node_id": nid})
    except Exception as exc:
        logger.warning("Failed to collect route entry points", error=str(exc))

    # 2. Artisan commands — classes with laravel_role='command' having a handle() method
    try:
        rows = db.execute(
            "MATCH (c:Class_ {laravel_role: 'command'})-[:DEFINES]->(m:Method {name: 'handle'}) "
            "RETURN m.fqn AS fqn, m.node_id AS nid"
        )
        for row in rows:
            fqn = row.get("fqn") or ""
            nid = row.get("nid") or ""
            if fqn and nid:
                entries.append({"entry_type": "command", "entry_fqn": fqn, "method_node_id": nid})
    except Exception as exc:
        logger.warning("Failed to collect command entry points", error=str(exc))

    # 3. Queue jobs — handle() on job classes
    try:
        rows = db.execute(
            "MATCH (c:Class_ {laravel_role: 'job'})-[:DEFINES]->(m:Method {name: 'handle'}) "
            "RETURN m.fqn AS fqn, m.node_id AS nid"
        )
        for row in rows:
            fqn = row.get("fqn") or ""
            nid = row.get("nid") or ""
            if fqn and nid:
                entries.append({"entry_type": "job", "entry_fqn": fqn, "method_node_id": nid})
    except Exception as exc:
        logger.warning("Failed to collect job entry points", error=str(exc))

    # 4. Event listeners — handle() on listener classes
    try:
        rows = db.execute(
            "MATCH (c:Class_ {laravel_role: 'listener'})-[:DEFINES]->(m:Method {name: 'handle'}) "
            "RETURN m.fqn AS fqn, m.node_id AS nid"
        )
        for row in rows:
            fqn = row.get("fqn") or ""
            nid = row.get("nid") or ""
            if fqn and nid:
                entries.append({"entry_type": "listener", "entry_fqn": fqn, "method_node_id": nid})
    except Exception as exc:
        logger.warning("Failed to collect listener entry points", error=str(exc))

    # 5. Middleware — handle() on middleware classes
    try:
        rows = db.execute(
            "MATCH (c:Class_ {laravel_role: 'middleware'})-[:DEFINES]->(m:Method {name: 'handle'}) "
            "RETURN m.fqn AS fqn, m.node_id AS nid"
        )
        for row in rows:
            fqn = row.get("fqn") or ""
            nid = row.get("nid") or ""
            if fqn and nid:
                entries.append({"entry_type": "middleware", "entry_fqn": fqn, "method_node_id": nid})
    except Exception as exc:
        logger.warning("Failed to collect middleware entry points", error=str(exc))

    # Deduplicate by method_node_id
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for e in entries:
        key = e["method_node_id"]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique


def _bfs_flow(
    db: Any,
    start_nid: str,
    max_depth: int = _DEFAULT_BFS_DEPTH,
    max_nodes: int = _MAX_BFS_NODES,
) -> list[tuple[str, int, int]]:
    """BFS through CALLS edges from start_nid.

    Returns list of (method_node_id, depth, order) tuples.
    """
    visited: dict[str, tuple[int, int]] = {}  # nid → (depth, order)
    queue: deque[tuple[str, int]] = deque([(start_nid, 0)])
    order_counter = 0

    while queue and len(visited) < max_nodes:
        current_nid, depth = queue.popleft()
        if current_nid in visited:
            continue
        visited[current_nid] = (depth, order_counter)
        order_counter += 1

        if depth >= max_depth:
            continue

        try:
            rows = db.execute(
                "MATCH (m:Method {node_id: $nid})-[:CALLS]->(callee:Method) "
                "RETURN callee.node_id AS callee_nid",
                {"nid": current_nid},
            )
            for row in rows:
                callee_nid = row.get("callee_nid") or ""
                if callee_nid and callee_nid not in visited:
                    queue.append((callee_nid, depth + 1))
        except Exception as exc:
            logger.debug("BFS CALLS query failed", nid=current_nid, error=str(exc))

        # Also follow Function_ calls
        try:
            rows = db.execute(
                "MATCH (m:Method {node_id: $nid})-[:CALLS]->(callee:Function_) "
                "RETURN callee.node_id AS callee_nid",
                {"nid": current_nid},
            )
            for row in rows:
                callee_nid = row.get("callee_nid") or ""
                if callee_nid and callee_nid not in visited:
                    queue.append((callee_nid, depth + 1))
        except Exception:
            pass

    return [(nid, depth, order) for nid, (depth, order) in visited.items()]


def run(ctx: PipelineContext) -> None:
    """Detect entry points and trace execution flows via BFS through call graph."""
    db = ctx.db
    max_depth = _DEFAULT_BFS_DEPTH
    processes_detected = 0

    entry_points = _collect_entry_points(ctx)
    logger.info("Collected entry points", count=len(entry_points))

    for ep in entry_points:
        entry_type = ep["entry_type"]
        entry_fqn = ep["entry_fqn"]
        method_nid = ep["method_node_id"]

        process_nid = make_node_id("process", f"{entry_type}:{entry_fqn}")
        process_name = f"{entry_type}:{entry_fqn.split('::')[-1] if '::' in entry_fqn else entry_fqn}"

        try:
            db._insert_node("Process", {
                "node_id": process_nid,
                "name": process_name,
                "entry_type": entry_type,
                "entry_fqn": entry_fqn,
                "depth": max_depth,
            })
        except Exception as exc:
            logger.debug("Process node insert failed (may already exist)", nid=process_nid, error=str(exc))

        # BFS to find all methods in the flow
        try:
            flow_steps = _bfs_flow(db, method_nid, max_depth=max_depth, max_nodes=_MAX_BFS_NODES)
        except Exception as exc:
            logger.warning("BFS flow tracing failed", entry_fqn=entry_fqn, error=str(exc))
            continue

        # Determine whether each node is a Method or Function_
        for step_nid, depth, order in flow_steps:
            try:
                db.upsert_rel(
                    "STEP_IN_PROCESS",
                    "Method",
                    step_nid,
                    "Process",
                    process_nid,
                    {"depth": depth, "step_order": order},
                )
            except Exception:
                # Try as Function_
                try:
                    db.upsert_rel(
                        "STEP_IN_PROCESS",
                        "Function_",
                        step_nid,
                        "Process",
                        process_nid,
                        {"depth": depth, "step_order": order},
                    )
                except Exception as exc2:
                    logger.debug(
                        "STEP_IN_PROCESS rel failed",
                        step_nid=step_nid,
                        process=process_nid,
                        error=str(exc2),
                    )

        processes_detected += 1

    ctx.stats["processes_detected"] = processes_detected
    logger.info("Execution flow detection complete", processes=processes_detected)
