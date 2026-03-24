"""Phase 10 — Dead Code Detection.

Multi-pass dead code detection with Laravel-aware exemptions. Marks methods
and functions that have no incoming call edges and are not reachable through
any Laravel framework entry mechanism.
"""

from __future__ import annotations

import re
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
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


def _has_incoming_call_edges(db: Any, node_id: str, node_label: str) -> bool:
    """Return True if the method/function has any incoming CALLS, ROUTES_TO,
    LISTENS_TO, or HANDLES edge pointing to it."""
    queries = [
        f"MATCH ()-[:CALLS]->(n:{node_label} {{node_id: $nid}}) RETURN count(*) AS cnt",
        f"MATCH ()-[:ROUTES_TO]->(n:{node_label} {{node_id: $nid}}) RETURN count(*) AS cnt",
        f"MATCH ()-[:LISTENS_TO]->(n:{node_label} {{node_id: $nid}}) RETURN count(*) AS cnt",
        f"MATCH ()-[:HANDLES]->(n:{node_label} {{node_id: $nid}}) RETURN count(*) AS cnt",
        f"MATCH ()-[:DISPATCHES]->(n:{node_label} {{node_id: $nid}}) RETURN count(*) AS cnt",
        f"MATCH ()-[:SCHEDULES]->(n:{node_label} {{node_id: $nid}}) RETURN count(*) AS cnt",
    ]
    for q in queries:
        try:
            rows = db.execute(q, {"nid": node_id})
            if rows and rows[0].get("cnt", 0) > 0:
                return True
        except Exception:
            pass
    return False


def _is_in_process(db: Any, node_id: str, node_label: str) -> bool:
    """Return True if the method is reachable through any Process (STEP_IN_PROCESS)."""
    try:
        rows = db.execute(
            f"MATCH (n:{node_label} {{node_id: $nid}})-[:STEP_IN_PROCESS]->(:Process) "
            "RETURN count(*) AS cnt",
            {"nid": node_id},
        )
        return bool(rows and rows[0].get("cnt", 0) > 0)
    except Exception:
        return False


def _is_trait_method(db: Any, node_id: str) -> bool:
    """Return True if this method is defined in a Trait_ (not a Class_)."""
    try:
        rows = db.execute(
            "MATCH (:Trait_)-[:DEFINES]->(m:Method {node_id: $nid}) RETURN count(*) AS cnt",
            {"nid": node_id},
        )
        return bool(rows and rows[0].get("cnt", 0) > 0)
    except Exception:
        return False


def _get_owning_class_role(db: Any, method_nid: str) -> str | None:
    """Return the laravel_role of the class that owns this method, if any."""
    try:
        rows = db.execute(
            "MATCH (c:Class_)-[:DEFINES]->(m:Method {node_id: $nid}) "
            "RETURN c.laravel_role AS role",
            {"nid": method_nid},
        )
        if rows:
            return rows[0].get("role")
    except Exception:
        pass
    return None


def _overrides_parent_method(db: Any, method_nid: str, method_name: str) -> bool:
    """Return True if the owning class extends another class that has the same method."""
    try:
        rows = db.execute(
            "MATCH (child:Class_)-[:DEFINES]->(m:Method {node_id: $nid}) "
            "MATCH (child)-[:EXTENDS_CLASS]->(parent:Class_) "
            "MATCH (parent)-[:DEFINES]->(pm:Method {name: $mname}) "
            "RETURN count(*) AS cnt",
            {"nid": method_nid, "mname": method_name},
        )
        return bool(rows and rows[0].get("cnt", 0) > 0)
    except Exception:
        return False


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
    # Required so that re-runs pick up new exemptions (e.g. after route fixes).
    for label in ("Method", "Function_"):
        try:
            db._conn.execute(f"MATCH (n:{label}) SET n.is_dead_code = false")
        except Exception as exc:
            logger.debug("Failed to reset dead-code flags", label=label, error=str(exc))

    # ── Pass 1: Methods ───────────────────────────────────────────────────────
    try:
        method_rows = db.execute(
            "MATCH (m:Method) RETURN m.node_id AS nid, m.name AS name, m.fqn AS fqn, "
            "m.file_path AS fp"
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

        # Skip vendor/legacy files — their methods are called via dynamic dispatch
        # or string-based lookup that static analysis cannot detect
        if fp and (
            "/vendor/" in fp
            or "/legacy/" in fp
            or "\\vendor\\" in fp
            or "\\legacy\\" in fp
        ):
            continue

        try:
            # Exempt by method name
            if _is_exempt_by_name(name):
                continue

            # Exempt if it's a trait method
            if _is_trait_method(db, nid):
                continue

            # Exempt if in a Policy, Observer, or FormRequest
            role = _get_owning_class_role(db, nid)
            if role in _EXEMPT_ROLES:
                continue

            # Exempt if reachable via a Process
            if _is_in_process(db, nid, "Method"):
                continue

            # Exempt if it has any incoming edges
            if _has_incoming_call_edges(db, nid, "Method"):
                continue

            # Exempt if it overrides a parent class method
            if _overrides_parent_method(db, nid, name):
                continue

            # Mark as dead
            _mark_node_dead(db, "Method", nid)
            dead_method_nids.append(nid)
            dead_methods += 1

        except Exception as exc:
            logger.debug("Error evaluating method dead code", nid=nid, error=str(exc))

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

        try:
            if name in _EXEMPT_METHOD_NAMES:
                continue

            if _is_in_process(db, nid, "Function_"):
                continue

            if _has_incoming_call_edges(db, nid, "Function_"):
                continue

            _mark_node_dead(db, "Function_", nid)
            dead_functions += 1

        except Exception as exc:
            logger.debug("Error evaluating function dead code", nid=nid, error=str(exc))

    # ── Pass 3: Un-flag methods that override a live parent method ─────────────
    # If a method was marked dead but overrides a parent method that is NOT dead,
    # unmark it.
    revived = 0
    for nid in list(dead_method_nids):
        try:
            rows = db.execute(
                "MATCH (child:Class_)-[:DEFINES]->(m:Method {node_id: $nid}) "
                "MATCH (child)-[:EXTENDS_CLASS]->(parent:Class_) "
                "MATCH (parent)-[:DEFINES]->(pm:Method {name: m.name}) "
                "WHERE pm.is_dead_code = false OR pm.is_dead_code IS NULL "
                "RETURN count(*) AS cnt",
                {"nid": nid},
            )
            if rows and rows[0].get("cnt", 0) > 0:
                _unmark_node_dead(db, "Method", nid)
                dead_methods -= 1
                revived += 1
        except Exception:
            pass

    ctx.stats["dead_methods"] = max(0, dead_methods)
    ctx.stats["dead_functions"] = max(0, dead_functions)

    logger.info(
        "Dead code detection complete",
        dead_methods=dead_methods,
        dead_functions=dead_functions,
        revived=revived,
    )
