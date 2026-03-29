"""Phase 28 — Behavioral Contract Extraction.

Extracts behavioral contracts from FormRequests, Policies, Observers, and
EloquentModel mass-assignment config into Contract nodes with GOVERNS edges.

Contract types
--------------
validation      — FormRequest rules() method field rules
authorization   — Policy class method abilities (viewAny, view, create, …)
lifecycle       — Observer class method lifecycle hooks (created, updated, …)
mass_assignment — EloquentModel $fillable / $guarded property declarations
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Compiled patterns ─────────────────────────────────────────────────────────

# Match the body of the rules() method in a FormRequest
_RULES_METHOD_RE = re.compile(
    r"function\s+rules\s*\(\s*\)[^{]*\{(.*?)\}",
    re.DOTALL,
)

# Match simple string rule pairs: 'field' => 'required|email|max:255'
_SIMPLE_RULE_RE = re.compile(
    r"""['\"]([^'\"]+)['\"]\s*=>\s*['\"]([^'\"]+)['\"]"""
)

# Match public method declarations in Policy / Observer classes
_PUBLIC_METHOD_RE = re.compile(r"public\s+function\s+(\w+)\s*\(")

# Match $fillable array contents
_FILLABLE_RE = re.compile(r"\$fillable\s*=\s*\[([^\]]*)\]", re.DOTALL)

# Match $guarded array contents
_GUARDED_RE = re.compile(r"\$guarded\s*=\s*\[([^\]]*)\]", re.DOTALL)

# Extract quoted strings from a PHP array literal
_ARRAY_STRINGS_RE = re.compile(r"['\"]([^'\"]+)['\"]")

# ── Known policy and observer method sets ────────────────────────────────────

_STANDARD_POLICY_METHODS: frozenset[str] = frozenset({
    "viewAny", "view", "create", "update", "delete",
    "restore", "forceDelete", "before", "after",
})

_OBSERVER_LIFECYCLE_EVENTS: frozenset[str] = frozenset({
    "retrieved", "creating", "created", "updating", "updated",
    "saving", "saved", "deleting", "deleted", "restoring", "restored",
    "replicating", "forceDeleted",
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_file(file_path: str) -> str | None:
    """Read a PHP source file, returning its content or None on error."""
    try:
        return Path(file_path).read_text(errors="replace")
    except OSError as exc:
        logger.debug("Cannot read file", path=file_path, error=str(exc))
        return None


def _extract_validation_rules(source: str) -> dict[str, str]:
    """Parse FormRequest rules() body and return {field: rules_string}.

    Handles simple string rules only:
        'email' => 'required|email|max:255'

    Array-syntax rules (``'email' => ['required', Rule::email()]``) are
    intentionally skipped — they require a PHP AST to parse reliably.
    """
    rules: dict[str, str] = {}
    match = _RULES_METHOD_RE.search(source)
    if not match:
        return rules
    body = match.group(1)
    for m in _SIMPLE_RULE_RE.finditer(body):
        field, rule_str = m.group(1), m.group(2)
        rules[field] = rule_str
    return rules


def _extract_policy_methods(source: str) -> dict[str, bool]:
    """Return a dict of method_name → True for all public methods in a Policy."""
    methods: dict[str, bool] = {}
    for m in _PUBLIC_METHOD_RE.finditer(source):
        method_name = m.group(1)
        # Include both standard Laravel policy methods and any custom ones
        methods[method_name] = True
    return methods


def _extract_observer_methods(source: str) -> dict[str, bool]:
    """Return a dict of lifecycle_event → True for matching public methods."""
    methods: dict[str, bool] = {}
    for m in _PUBLIC_METHOD_RE.finditer(source):
        method_name = m.group(1)
        if method_name in _OBSERVER_LIFECYCLE_EVENTS:
            methods[method_name] = True
    return methods


def _extract_mass_assignment(source: str) -> dict[str, Any]:
    """Return mass-assignment rules from $fillable / $guarded declarations."""
    result: dict[str, Any] = {}

    fillable_match = _FILLABLE_RE.search(source)
    if fillable_match:
        fields = _ARRAY_STRINGS_RE.findall(fillable_match.group(1))
        result["fillable"] = fields

    guarded_match = _GUARDED_RE.search(source)
    if guarded_match:
        fields = _ARRAY_STRINGS_RE.findall(guarded_match.group(1))
        result["guarded"] = fields

    return result


def _first_line_of_method(source: str, method_name: str) -> int:
    """Return the 1-based line number of the named method, or 0 if not found."""
    pattern = re.compile(
        r"function\s+" + re.escape(method_name) + r"\s*\("
    )
    lines = source.splitlines()
    for idx, line in enumerate(lines, start=1):
        if pattern.search(line):
            return idx
    return 0


# ── Main phase ────────────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> None:
    """Extract behavioral contracts from FormRequests, Policies, Observers, and Models."""
    db = ctx.db
    contracts_extracted = 0

    # ── 1. Validation contracts from FormRequest nodes ─────────────────────
    try:
        form_request_rows: list[dict[str, Any]] = db.execute(
            "MATCH (f:FormRequest) "
            "RETURN f.node_id AS nid, f.name AS name, f.fqn AS fqn, f.file_path AS fp"
        )
    except Exception as exc:
        logger.warning("Failed to fetch FormRequest nodes", error=str(exc))
        form_request_rows = []

    for row in form_request_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        fqn = row.get("fqn") or ""
        fp = row.get("fp") or ""
        if not nid or not fp:
            continue

        source = _read_file(fp)
        if source is None:
            continue

        rules = _extract_validation_rules(source)
        if not rules:
            continue

        # Determine line number of the rules() method
        line_start = _first_line_of_method(source, "rules")

        contract_nid = make_node_id("contract", fqn or name, "validation")
        try:
            db.upsert_node("Contract", {
                "node_id": contract_nid,
                "name": f"{name} validation",
                "contract_type": "validation",
                "source_class": name,
                "source_fqn": fqn,
                "rules": json.dumps(rules),
                "file_path": fp,
                "line_start": line_start,
            })
            contracts_extracted += 1
        except Exception as exc:
            logger.warning(
                "Failed to create validation Contract node",
                fqn=fqn,
                error=str(exc),
            )
            continue

        # GOVERNS edge: Contract → FormRequest
        try:
            db.upsert_rel(
                "GOVERNS",
                from_label="Contract",
                from_id=contract_nid,
                to_label="FormRequest",
                to_id=nid,
                props={"role": "validates"},
            )
        except Exception as exc:
            logger.debug(
                "Failed to link Contract to FormRequest",
                contract_nid=contract_nid,
                fr_nid=nid,
                error=str(exc),
            )

        # Also link to routes that use this FormRequest (via Class_/Method injection)
        # Best-effort: find controllers that inject this FormRequest
        try:
            route_rows: list[dict[str, Any]] = db.execute(
                "MATCH (r:Route)-[:ROUTES_TO]->(m:Method)"
                "<-[:DEFINES]-(c:Class_)-[:INJECTS]->(f:FormRequest {node_id: $fnid}) "
                "RETURN r.node_id AS rnid",
                {"fnid": nid},
            )
            for rrow in route_rows:
                rnid = rrow.get("rnid") or ""
                if not rnid:
                    continue
                try:
                    db.upsert_rel(
                        "GOVERNS",
                        from_label="Contract",
                        from_id=contract_nid,
                        to_label="Route",
                        to_id=rnid,
                        props={"role": "validates"},
                    )
                except Exception:
                    pass
        except Exception:
            pass  # route linkage is best-effort

    # ── 2. Authorization contracts from Policy nodes ───────────────────────
    try:
        policy_rows: list[dict[str, Any]] = db.execute(
            "MATCH (p:Policy) "
            "RETURN p.node_id AS nid, p.name AS name, p.fqn AS fqn, "
            "p.file_path AS fp, p.model_fqn AS model_fqn"
        )
    except Exception as exc:
        logger.warning("Failed to fetch Policy nodes", error=str(exc))
        policy_rows = []

    for row in policy_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        fqn = row.get("fqn") or ""
        fp = row.get("fp") or ""
        model_fqn = row.get("model_fqn") or ""
        if not nid or not fp:
            continue

        source = _read_file(fp)
        if source is None:
            continue

        methods = _extract_policy_methods(source)
        if not methods:
            continue

        contract_nid = make_node_id("contract", fqn or name, "authorization")
        try:
            db.upsert_node("Contract", {
                "node_id": contract_nid,
                "name": f"{name} authorization",
                "contract_type": "authorization",
                "source_class": name,
                "source_fqn": fqn,
                "rules": json.dumps(methods),
                "file_path": fp,
                "line_start": 1,
            })
            contracts_extracted += 1
        except Exception as exc:
            logger.warning(
                "Failed to create authorization Contract node",
                fqn=fqn,
                error=str(exc),
            )
            continue

        # GOVERNS edge: Contract → Policy
        try:
            db.upsert_rel(
                "GOVERNS",
                from_label="Contract",
                from_id=contract_nid,
                to_label="Policy",
                to_id=nid,
                props={"role": "authorizes"},
            )
        except Exception as exc:
            logger.debug(
                "Failed to link Contract to Policy",
                contract_nid=contract_nid,
                policy_nid=nid,
                error=str(exc),
            )

        # GOVERNS edge: Contract → EloquentModel (if model FQN known)
        if model_fqn:
            try:
                model_rows: list[dict[str, Any]] = db.execute(
                    "MATCH (m:EloquentModel {fqn: $fqn}) RETURN m.node_id AS mnid",
                    {"fqn": model_fqn},
                )
                for mrow in model_rows:
                    mnid = mrow.get("mnid") or ""
                    if not mnid:
                        continue
                    try:
                        db.upsert_rel(
                            "GOVERNS",
                            from_label="Contract",
                            from_id=contract_nid,
                            to_label="EloquentModel",
                            to_id=mnid,
                            props={"role": "authorizes"},
                        )
                    except Exception:
                        pass
            except Exception:
                pass  # model linkage is best-effort

    # ── 3. Lifecycle contracts from Observer nodes ─────────────────────────
    try:
        observer_rows: list[dict[str, Any]] = db.execute(
            "MATCH (o:Observer) "
            "RETURN o.node_id AS nid, o.name AS name, o.fqn AS fqn, "
            "o.file_path AS fp, o.model_fqn AS model_fqn"
        )
    except Exception as exc:
        logger.warning("Failed to fetch Observer nodes", error=str(exc))
        observer_rows = []

    for row in observer_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        fqn = row.get("fqn") or ""
        fp = row.get("fp") or ""
        model_fqn = row.get("model_fqn") or ""
        if not nid or not fp:
            continue

        source = _read_file(fp)
        if source is None:
            continue

        methods = _extract_observer_methods(source)
        if not methods:
            continue

        contract_nid = make_node_id("contract", fqn or name, "lifecycle")
        try:
            db.upsert_node("Contract", {
                "node_id": contract_nid,
                "name": f"{name} lifecycle",
                "contract_type": "lifecycle",
                "source_class": name,
                "source_fqn": fqn,
                "rules": json.dumps(methods),
                "file_path": fp,
                "line_start": 1,
            })
            contracts_extracted += 1
        except Exception as exc:
            logger.warning(
                "Failed to create lifecycle Contract node",
                fqn=fqn,
                error=str(exc),
            )
            continue

        # GOVERNS edge: Contract → Observer
        try:
            db.upsert_rel(
                "GOVERNS",
                from_label="Contract",
                from_id=contract_nid,
                to_label="Observer",
                to_id=nid,
                props={"role": "observes"},
            )
        except Exception as exc:
            logger.debug(
                "Failed to link Contract to Observer",
                contract_nid=contract_nid,
                observer_nid=nid,
                error=str(exc),
            )

        # GOVERNS edge: Contract → EloquentModel (model being observed)
        if model_fqn:
            try:
                model_rows2: list[dict[str, Any]] = db.execute(
                    "MATCH (m:EloquentModel {fqn: $fqn}) RETURN m.node_id AS mnid",
                    {"fqn": model_fqn},
                )
                for mrow in model_rows2:
                    mnid = mrow.get("mnid") or ""
                    if not mnid:
                        continue
                    try:
                        db.upsert_rel(
                            "GOVERNS",
                            from_label="Contract",
                            from_id=contract_nid,
                            to_label="EloquentModel",
                            to_id=mnid,
                            props={"role": "observes"},
                        )
                    except Exception:
                        pass
            except Exception:
                pass  # best-effort

    # ── 4. Mass-assignment contracts from EloquentModel nodes ─────────────
    try:
        model_rows_all: list[dict[str, Any]] = db.execute(
            "MATCH (m:EloquentModel) "
            "RETURN m.node_id AS nid, m.name AS name, m.fqn AS fqn, "
            "m.file_path AS fp, m.fillable AS fillable, m.guarded AS guarded"
        )
    except Exception as exc:
        logger.warning("Failed to fetch EloquentModel nodes for contracts", error=str(exc))
        model_rows_all = []

    for row in model_rows_all:
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        fqn = row.get("fqn") or ""
        fp = row.get("fp") or ""
        # Phase 13 stores fillable/guarded as JSON strings on the node already
        fillable_json = row.get("fillable") or "[]"
        guarded_json = row.get("guarded") or "[]"
        if not nid:
            continue

        # Parse the stored JSON; fall back to file reading if empty
        try:
            fillable: list[str] = json.loads(fillable_json) if fillable_json else []
        except (json.JSONDecodeError, TypeError):
            fillable = []

        try:
            guarded: list[str] = json.loads(guarded_json) if guarded_json else []
        except (json.JSONDecodeError, TypeError):
            guarded = []

        # If the node has no mass-assignment data but we have a file, parse it
        if not fillable and not guarded and fp:
            source = _read_file(fp)
            if source:
                parsed = _extract_mass_assignment(source)
                fillable = parsed.get("fillable", [])
                guarded = parsed.get("guarded", [])

        if not fillable and not guarded:
            continue

        rules_dict: dict[str, Any] = {}
        if fillable:
            rules_dict["fillable"] = fillable
        if guarded:
            rules_dict["guarded"] = guarded

        # Determine a meaningful line_start from the source file
        line_start = 0
        if fp:
            source = _read_file(fp)
            if source:
                if fillable:
                    m_fill = _FILLABLE_RE.search(source)
                    if m_fill:
                        line_start = source[: m_fill.start()].count("\n") + 1
                elif guarded:
                    m_guard = _GUARDED_RE.search(source)
                    if m_guard:
                        line_start = source[: m_guard.start()].count("\n") + 1

        contract_nid = make_node_id("contract", fqn or name, "mass_assignment")
        try:
            db.upsert_node("Contract", {
                "node_id": contract_nid,
                "name": f"{name} mass assignment",
                "contract_type": "mass_assignment",
                "source_class": name,
                "source_fqn": fqn,
                "rules": json.dumps(rules_dict),
                "file_path": fp,
                "line_start": line_start,
            })
            contracts_extracted += 1
        except Exception as exc:
            logger.warning(
                "Failed to create mass_assignment Contract node",
                fqn=fqn,
                error=str(exc),
            )
            continue

        # GOVERNS edge: Contract → EloquentModel
        try:
            role = "fillable" if fillable else "guarded"
            db.upsert_rel(
                "GOVERNS",
                from_label="Contract",
                from_id=contract_nid,
                to_label="EloquentModel",
                to_id=nid,
                props={"role": role},
            )
        except Exception as exc:
            logger.debug(
                "Failed to link Contract to EloquentModel",
                contract_nid=contract_nid,
                model_nid=nid,
                error=str(exc),
            )

    ctx.stats["contracts_extracted"] = contracts_extracted

    logger.info(
        "Contract extraction complete",
        contracts_extracted=contracts_extracted,
    )
