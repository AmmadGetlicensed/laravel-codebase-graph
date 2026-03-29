"""Phase 31 — N+1 and Query Pattern Detection.

Scans method source code for performance anti-patterns and creates
``PerformanceRisk`` nodes with ``HAS_PERFORMANCE_RISK`` edges from the
offending Method.

Patterns detected
-----------------
n_plus_one
    A foreach loop body that accesses a chained relationship property on the
    loop variable without a corresponding ``->with()`` / ``$with`` eager load.
missing_eager_load
    Same relationship access inside a loop, and the model has no matching
    eager_loads declared in the graph.
repeated_count
    ``->count()`` appears more than once in the same method body (likely
    redundant DB round-trips).
raw_query_bypass
    ``DB::select(`` or ``DB::statement(`` calls that bypass Eloquent entirely.

Only methods belonging to controller, service, repository, or model
``laravel_role`` classes are scanned.  Test files are skipped.

Stats: ``performance_risks_found``
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Compiled patterns ─────────────────────────────────────────────────────────

# foreach ($collection as $item) — captures loop variable name
_FOREACH_RE = re.compile(
    r"foreach\s*\(\s*\$\w+\s+as\s+(?:\$\w+\s*=>\s*)?\$(\w+)\s*\)"
)

# Chained relationship access on a variable: $item->relation->...
_CHAIN_ACCESS_RE = re.compile(r"\$(\w+)\s*->\s*\w+\s*->")

# ->count() calls
_COUNT_RE = re.compile(r"->\s*count\s*\(\s*\)")

# ->with( or $with eager-load declarations
_WITH_RE = re.compile(r"(?:->with\s*\(|\$with\s*=)")

# Raw query bypasses
_RAW_QUERY_RE = re.compile(r"\bDB\s*::\s*(?:select|statement)\s*\(")

# ── Constants ─────────────────────────────────────────────────────────────────

# Only scan methods whose class has one of these laravel_roles
_TARGET_ROLES: frozenset[str] = frozenset({
    "controller", "service", "repository", "model",
})

# Window (lines) to look ahead inside a foreach block for chained access
_FOREACH_LOOKAHEAD_LINES = 20

# ── Helpers ───────────────────────────────────────────────────────────────────


def _read_method_source(file_path: str, line_start: int, line_end: int) -> str | None:
    """Read the line range for a method from its source file."""
    if not file_path or line_start < 1:
        return None
    try:
        lines = Path(file_path).read_text(errors="replace").splitlines()
        start = max(0, line_start - 1)
        end = line_end if line_end and line_end >= line_start else len(lines)
        return "\n".join(lines[start:end])
    except OSError as exc:
        logger.debug("Cannot read file", path=file_path, error=str(exc))
        return None


def _risk_node_id(method_fqn: str, risk_type: str, evidence: str) -> str:
    """Generate a stable, unique node_id for a PerformanceRisk."""
    digest = hashlib.sha1(
        f"{method_fqn}:{risk_type}:{evidence}".encode()
    ).hexdigest()[:10]
    return make_node_id("perfrisk", method_fqn, risk_type, digest)


def _detect_n_plus_one(source: str) -> list[dict[str, Any]]:
    """Detect N+1 patterns: foreach loop with chained relationship access inside.

    Returns a list of dicts with keys: risk_type, description, severity,
    line_number, evidence.
    """
    risks: list[dict[str, Any]] = []
    lines = source.splitlines()

    for m in _FOREACH_RE.finditer(source):
        loop_var = m.group(1)
        # Find the line number of the foreach statement (1-based)
        loop_line = source[: m.start()].count("\n") + 1

        # Look ahead up to _FOREACH_LOOKAHEAD_LINES lines for chained access
        window_lines = lines[loop_line: loop_line + _FOREACH_LOOKAHEAD_LINES]
        window_text = "\n".join(window_lines)

        chain_match = _CHAIN_ACCESS_RE.search(window_text)
        if chain_match and chain_match.group(1) == loop_var:
            # Check whether there is an eager load guard anywhere in the method
            has_eager_load = bool(_WITH_RE.search(source))
            risk_type = "n_plus_one" if not has_eager_load else "missing_eager_load"
            severity = "HIGH" if risk_type == "n_plus_one" else "MEDIUM"
            evidence = (
                f"foreach (${loop_var}) at line {loop_line}; "
                f"chained access: {chain_match.group(0).strip()}"
            )
            risks.append({
                "risk_type": risk_type,
                "description": (
                    f"Possible N+1 query: loop variable ${loop_var} has chained "
                    "relationship access inside foreach without eager loading"
                    if risk_type == "n_plus_one"
                    else f"Relationship accessed on ${loop_var} inside loop; "
                    "eager load found elsewhere but not guarding this access"
                ),
                "severity": severity,
                "line_number": loop_line,
                "evidence": evidence,
            })

    return risks


def _detect_repeated_count(source: str) -> list[dict[str, Any]]:
    """Detect multiple ->count() calls in the same method."""
    matches = list(_COUNT_RE.finditer(source))
    if len(matches) < 2:
        return []

    first_line = source[: matches[0].start()].count("\n") + 1
    evidence = f"->count() called {len(matches)} times in same method"
    return [{
        "risk_type": "repeated_count",
        "description": (
            f"->count() is called {len(matches)} times without caching "
            "the result — each call issues a separate COUNT query"
        ),
        "severity": "MEDIUM",
        "line_number": first_line,
        "evidence": evidence,
    }]


def _detect_raw_query_bypass(source: str) -> list[dict[str, Any]]:
    """Detect DB::select / DB::statement bypasses."""
    risks: list[dict[str, Any]] = []
    for m in _RAW_QUERY_RE.finditer(source):
        line_number = source[: m.start()].count("\n") + 1
        evidence = m.group(0).rstrip("(").strip()
        risks.append({
            "risk_type": "raw_query_bypass",
            "description": (
                f"{evidence} bypasses Eloquent — raw SQL is harder to maintain, "
                "type-unsafe, and not linked to model nodes in the graph"
            ),
            "severity": "MEDIUM",
            "line_number": line_number,
            "evidence": evidence,
        })
    return risks


# ── Main phase ────────────────────────────────────────────────────────────────


def run(ctx: PipelineContext) -> None:
    """Detect N+1 and query anti-patterns across controller/service/repository/model methods."""
    db = ctx.db
    performance_risks_found = 0

    # ── Fetch target methods (only from target-role classes) ──────────────
    try:
        method_rows: list[dict[str, Any]] = db.execute(
            "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
            "WHERE c.laravel_role IN ['controller', 'service', 'repository', 'model'] "
            "  AND NOT c.file_path CONTAINS '/tests/' "
            "  AND NOT c.file_path CONTAINS '\\\\tests\\\\' "
            "RETURN m.node_id AS nid, m.fqn AS fqn, m.file_path AS fp, "
            "       m.line_start AS ls, m.line_end AS le"
        )
    except Exception as exc:
        logger.error("Failed to fetch Method nodes for pattern detection", error=str(exc))
        ctx.stats["performance_risks_found"] = 0
        return

    if not method_rows:
        logger.info(
            "No target-role methods found for query pattern detection — "
            "ensure laravel_role is set on Class_ nodes (phase 3)"
        )
        ctx.stats["performance_risks_found"] = 0
        return

    logger.info("Scanning methods for query anti-patterns", count=len(method_rows))

    for row in method_rows:
        method_nid = row.get("nid") or ""
        method_fqn = row.get("fqn") or ""
        file_path = row.get("fp") or ""
        line_start = int(row.get("ls") or 0)
        line_end = int(row.get("le") or 0)

        if not method_nid or not file_path:
            continue

        # Skip test file paths defensively (belt-and-braces)
        norm_fp = file_path.replace("\\", "/").lower()
        if "/tests/" in norm_fp:
            continue

        source = _read_method_source(file_path, line_start, line_end)
        if not source:
            continue

        # Aggregate all detected risks for this method
        risks: list[dict[str, Any]] = []
        risks.extend(_detect_n_plus_one(source))
        risks.extend(_detect_repeated_count(source))
        risks.extend(_detect_raw_query_bypass(source))

        for risk in risks:
            risk_nid = _risk_node_id(method_fqn, risk["risk_type"], risk["evidence"])

            try:
                db.upsert_node("PerformanceRisk", {
                    "node_id": risk_nid,
                    "risk_type": risk["risk_type"],
                    "description": risk["description"],
                    "severity": risk["severity"],
                    "file_path": file_path,
                    "line_number": risk["line_number"],
                    "method_fqn": method_fqn,
                    "evidence": risk["evidence"],
                })
                performance_risks_found += 1
            except Exception as exc:
                logger.warning(
                    "Failed to create PerformanceRisk node",
                    method_fqn=method_fqn,
                    risk_type=risk["risk_type"],
                    error=str(exc),
                )
                continue

            try:
                db.upsert_rel(
                    "HAS_PERFORMANCE_RISK",
                    from_label="Method",
                    from_id=method_nid,
                    to_label="PerformanceRisk",
                    to_id=risk_nid,
                    props={},
                )
            except Exception as exc:
                logger.debug(
                    "Failed to create HAS_PERFORMANCE_RISK edge",
                    method_nid=method_nid,
                    risk_nid=risk_nid,
                    error=str(exc),
                )

    ctx.stats["performance_risks_found"] = performance_risks_found

    logger.info(
        "Query pattern detection complete",
        performance_risks_found=performance_risks_found,
    )
