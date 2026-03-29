"""Phase 10 — Dead Code Detection.

Multi-pass dead code detection with Laravel-aware exemptions. Marks methods
and functions that have no incoming call edges and are not reachable through
any Laravel framework entry mechanism.

Performance design
------------------
Instead of running 10+ Cypher queries *per node* (N+1), this phase runs a
fixed number of bulk queries upfront to produce Python sets, then the main
loop does only O(1) set-membership lookups per node. This reduces Cypher
round-trips from O(N × 10) down to O(10 + N) — dramatically faster for
large codebases and far less pressure on KuzuDB's buffer pool.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Methods that are always exempted from dead-code marking
_EXEMPT_METHOD_NAMES: frozenset[str] = frozenset({
    "handle",
    "boot",
    "register",
    "up",
    "down",
    "run",
    "definition",
    "schedule",        # App\Console\Kernel::schedule — called by framework scheduler
    "broadcastOn",     # Event classes — called by Laravel Broadcasting
    "broadcastWith",   # Event classes — called by Laravel Broadcasting
    "broadcastAs",     # Event classes — called by Laravel Broadcasting
    "via",             # Notification classes — called by Laravel Notifications
    "toMail",          # Notification/Mailable classes — called by framework
    "toArray",         # Notification/Resource classes — called by framework
    "build",           # Mailable::build() — called by Mail::send()
    "__construct",
    "__call",
    "__callStatic",
    "__get",
    "__set",
    "__toString",
    "__invoke",
    "__destruct",
    "__clone",
    "__sleep",
    "__wakeup",
    "__serialize",
    "__unserialize",
    "__isset",
    "__unset",
    "__debugInfo",
})

# Laravel roles whose ALL methods are always exempt (framework calls them via conventions)
_EXEMPT_ROLES: frozenset[str] = frozenset({
    "policy",
    "observer",
    "request",    # FormRequest — authorize() / rules() / messages() called by framework
    "resource",   # API Resources — toArray() / collection() / setExtra() called by framework
})

_SCOPE_PATTERN = re.compile(r"^scope[A-Z]")
_ACCESSOR_PATTERN = re.compile(r"^get[A-Z].*Attribute$")
_MUTATOR_PATTERN = re.compile(r"^set[A-Z].*Attribute$")

# Eloquent relationship method body markers — any method containing one of these
# returns an Eloquent relationship instance and is called dynamically as a magic
# property ($model->relationName), which is invisible to static call analysis.
_ELOQUENT_RELATIONSHIP_CALLS = frozenset({
    "hasOne",
    "hasMany",
    "belongsTo",
    "belongsToMany",
    "hasOneThrough",
    "hasManyThrough",
    "morphOne",
    "morphMany",
    "morphTo",
    "morphToMany",
    "morphedByMany",
    "hasOneOfMany",
    "hasManyOfMany",
    # Laravel 8+ shorthand aliases
    "through",
})


def _is_exempt_by_name(method_name: str) -> bool:
    if method_name in _EXEMPT_METHOD_NAMES:
        return True
    if _SCOPE_PATTERN.match(method_name):
        return True
    if _ACCESSOR_PATTERN.match(method_name):
        return True
    if _MUTATOR_PATTERN.match(method_name):
        return True
    return False


# ── Bulk precomputation helpers ────────────────────────────────────────────────

def _bulk_referenced_nids(db: Any, label: str) -> set[str]:
    """Return all node_ids of *label* nodes that have ANY incoming relevant edge.

    Replaces the 8-query-per-node ``_has_incoming_call_edges`` function with a
    single pass of 8 bulk queries that build one set.
    """
    result: set[str] = set()
    edge_types = [
        "CALLS", "BLADE_CALLS", "ROUTES_TO", "LISTENS_TO",
        "HANDLES", "DISPATCHES", "SCHEDULES", "QUERIES_TABLE",
    ]
    for edge in edge_types:
        try:
            rows = db.execute(
                f"MATCH ()-[:{edge}]->(n:{label}) RETURN DISTINCT n.node_id AS nid"
            )
            for r in rows:
                nid = r.get("nid")
                if nid:
                    result.add(nid)
        except Exception as exc:
            logger.debug("bulk_referenced query failed", edge=edge, label=label, error=str(exc))
    return result


def _bulk_trait_method_nids(db: Any) -> set[str]:
    """Return node_ids of all methods defined in a Trait_."""
    try:
        rows = db.execute(
            "MATCH (:Trait_)-[:DEFINES]->(m:Method) RETURN DISTINCT m.node_id AS nid"
        )
        return {r["nid"] for r in rows if r.get("nid")}
    except Exception as exc:
        logger.debug("bulk_trait_methods failed", error=str(exc))
        return set()


def _bulk_exempt_role_method_nids(db: Any) -> set[str]:
    """Return node_ids of methods whose owning class has an exempt laravel_role."""
    try:
        rows = db.execute(
            "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
            "WHERE c.laravel_role IN $roles "
            "RETURN DISTINCT m.node_id AS nid",
            {"roles": list(_EXEMPT_ROLES)},
        )
        return {r["nid"] for r in rows if r.get("nid")}
    except Exception as exc:
        logger.debug("bulk_exempt_role_methods failed", error=str(exc))
        return set()


def _bulk_process_nids(db: Any, label: str) -> set[str]:
    """Return node_ids of nodes reachable via STEP_IN_PROCESS."""
    try:
        rows = db.execute(
            f"MATCH (n:{label})-[:STEP_IN_PROCESS]->(:Process) RETURN DISTINCT n.node_id AS nid"
        )
        return {r["nid"] for r in rows if r.get("nid")}
    except Exception as exc:
        logger.debug("bulk_process_nids failed", label=label, error=str(exc))
        return set()


def _bulk_route_handler_info(db: Any) -> tuple[set[str], set[str]]:
    """Return (handler_method_names, handler_method_nids) for all routes."""
    method_names: set[str] = set()
    method_nids: set[str] = set()
    try:
        rows = db.execute("MATCH (r:Route) RETURN r.action_method AS mname")
        for r in rows:
            if r.get("mname"):
                method_names.add(r["mname"])
    except Exception as exc:
        logger.debug("bulk_route_handler_names failed", error=str(exc))
    try:
        rows = db.execute(
            "MATCH ()-[:ROUTES_TO]->(m:Method) RETURN DISTINCT m.node_id AS nid"
        )
        for r in rows:
            if r.get("nid"):
                method_nids.add(r["nid"])
    except Exception as exc:
        logger.debug("bulk_route_handler_nids failed", error=str(exc))
    return method_names, method_nids


def _bulk_overriding_method_nids(db: Any) -> set[str]:
    """Return node_ids of methods that override a method in their parent class."""
    try:
        rows = db.execute(
            "MATCH (child:Class_)-[:DEFINES]->(m:Method) "
            "MATCH (child)-[:EXTENDS_CLASS]->(parent:Class_) "
            "MATCH (parent)-[:DEFINES]->(pm:Method) "
            "WHERE pm.name = m.name "
            "RETURN DISTINCT m.node_id AS nid"
        )
        return {r["nid"] for r in rows if r.get("nid")}
    except Exception as exc:
        logger.debug("bulk_overriding_methods failed", error=str(exc))
        return set()


def _bulk_eloquent_rel_method_nids(db: Any) -> set[str]:
    """Return node_ids of methods in Model classes that define Eloquent relationships.

    Reads the source file for each model method to detect relationship calls.
    This is the one check that still requires file I/O, but it's batched: we
    fetch all model method locations in one query, then scan source in Python.
    """
    try:
        rows = db.execute(
            "MATCH (c:Class_ {laravel_role: 'model'})-[:DEFINES]->(m:Method) "
            "RETURN m.node_id AS nid, m.file_path AS fp, "
            "m.line_start AS ls, m.line_end AS le"
        )
    except Exception as exc:
        logger.debug("bulk_eloquent_rel_methods query failed", error=str(exc))
        return set()

    result: set[str] = set()
    for row in rows:
        nid = row.get("nid") or ""
        fp = row.get("fp") or ""
        ls = row.get("ls") or 0
        le = row.get("le") or 0
        if not (nid and fp and ls):
            continue
        try:
            source_lines = Path(fp).read_text(errors="replace").splitlines()
            body = "\n".join(source_lines[max(0, ls - 1):min(le or ls + 50, ls + 100)])
            if any(f"$this->{rel}(" in body for rel in _ELOQUENT_RELATIONSHIP_CALLS):
                result.add(nid)
        except OSError:
            pass
    return result


def _mark_node_dead(db: Any, node_label: str, node_id_val: str) -> None:
    """Update is_dead_code=true on the node."""
    try:
        db._conn.execute(
            f"MATCH (n:{node_label} {{node_id: $nid}}) SET n.is_dead_code = true",
            parameters={"nid": node_id_val},
        )
    except Exception as exc:
        logger.debug("Failed to mark dead code", label=node_label, nid=node_id_val, error=str(exc))


def _unmark_node_dead(db: Any, node_label: str, node_id_val: str) -> None:
    """Update is_dead_code=false on the node."""
    try:
        db._conn.execute(
            f"MATCH (n:{node_label} {{node_id: $nid}}) SET n.is_dead_code = false",
            parameters={"nid": node_id_val},
        )
    except Exception as exc:
        logger.debug("Failed to unmark dead code", label=node_label, nid=node_id_val, error=str(exc))


def run(ctx: PipelineContext) -> None:
    """Detect dead methods and functions with Laravel-aware exemptions."""
    db = ctx.db
    dead_methods = 0
    dead_functions = 0

    # ── Reset: clear all previous dead-code flags before re-evaluating ────────
    for label in ("Method", "Function_"):
        try:
            db._conn.execute(f"MATCH (n:{label}) SET n.is_dead_code = false")
        except Exception as exc:
            logger.debug("Failed to reset dead-code flags", label=label, error=str(exc))

    # ── Bulk precomputation (replaces per-row DB queries) ─────────────────────
    logger.info("Precomputing dead-code exemption sets...")

    referenced_methods   = _bulk_referenced_nids(db, "Method")
    trait_methods        = _bulk_trait_method_nids(db)
    exempt_role_methods  = _bulk_exempt_role_method_nids(db)
    process_methods      = _bulk_process_nids(db, "Method")
    route_handler_names, route_handler_nids = _bulk_route_handler_info(db)
    overriding_methods   = _bulk_overriding_method_nids(db)
    eloquent_rel_methods = _bulk_eloquent_rel_method_nids(db)

    referenced_functions = _bulk_referenced_nids(db, "Function_")
    process_functions    = _bulk_process_nids(db, "Function_")

    logger.info(
        "Exemption sets ready",
        referenced_methods=len(referenced_methods),
        trait_methods=len(trait_methods),
        exempt_role_methods=len(exempt_role_methods),
        route_handlers=len(route_handler_nids),
        overriding=len(overriding_methods),
        eloquent_rel=len(eloquent_rel_methods),
    )

    # ── Pass 1: Methods ───────────────────────────────────────────────────────
    try:
        method_rows = db.execute(
            "MATCH (m:Method) RETURN m.node_id AS nid, m.name AS name, "
            "m.fqn AS fqn, m.file_path AS fp"
        )
    except Exception as exc:
        logger.error("Failed to fetch Method nodes", error=str(exc))
        method_rows = []

    dead_method_nids: list[str] = []

    for row in method_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        fp = row.get("fp") or ""
        if not nid:
            continue

        # Skip vendor/legacy files
        if fp and ("/vendor/" in fp or "/legacy/" in fp
                   or "\\vendor\\" in fp or "\\legacy\\" in fp):
            continue

        # All exemption checks are now O(1) set lookups
        if _is_exempt_by_name(name):
            continue
        if nid in trait_methods:
            continue
        if nid in exempt_role_methods:
            continue
        if nid in eloquent_rel_methods:
            continue
        if nid in process_methods:
            continue
        if nid in referenced_methods:
            continue
        if name in route_handler_names or nid in route_handler_nids:
            continue
        if nid in overriding_methods:
            continue

        _mark_node_dead(db, "Method", nid)
        dead_method_nids.append(nid)
        dead_methods += 1

    # ── Pass 2: Functions ─────────────────────────────────────────────────────
    try:
        func_rows = db.execute(
            "MATCH (f:Function_) RETURN f.node_id AS nid, f.name AS name"
        )
    except Exception as exc:
        logger.error("Failed to fetch Function_ nodes", error=str(exc))
        func_rows = []

    for row in func_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        if not nid:
            continue
        if name in _EXEMPT_METHOD_NAMES:
            continue
        if nid in process_functions:
            continue
        if nid in referenced_functions:
            continue

        _mark_node_dead(db, "Function_", nid)
        dead_functions += 1

    # ── Pass 3: Bulk-revive methods that override a LIVE parent method ────────
    # One query replaces the previous per-nid loop.
    revived = 0
    try:
        revive_rows = db.execute(
            "MATCH (child:Class_)-[:DEFINES]->(m:Method) "
            "WHERE m.is_dead_code = true "
            "MATCH (child)-[:EXTENDS_CLASS]->(parent:Class_) "
            "MATCH (parent)-[:DEFINES]->(pm:Method) "
            "WHERE pm.name = m.name "
            "AND (pm.is_dead_code = false OR pm.is_dead_code IS NULL) "
            "RETURN DISTINCT m.node_id AS nid"
        )
        for row in revive_rows:
            nid = row.get("nid") or ""
            if nid:
                _unmark_node_dead(db, "Method", nid)
                dead_methods -= 1
                revived += 1
    except Exception as exc:
        logger.debug("Pass 3 bulk revive failed — skipping", error=str(exc))

    ctx.stats["dead_methods"] = max(0, dead_methods)
    ctx.stats["dead_functions"] = max(0, dead_functions)

    logger.info(
        "Dead code detection complete",
        dead_methods=dead_methods,
        dead_functions=dead_functions,
        revived=revived,
    )
