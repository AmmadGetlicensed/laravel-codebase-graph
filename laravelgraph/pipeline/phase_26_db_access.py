"""Phase 26 — Universal DB Access Analysis (static, zero AI cost).

Scans every PHP file and extracts raw evidence about how the database is
accessed from code.  This phase is intentionally dumb and cheap — it collects
facts; it does NOT interpret them.  The lazy MCP tools (phase D/E) feed this
raw evidence to the configured LLM when an agent actually asks for it.

What gets detected
──────────────────
1. DB::table() / DB::connection()->table()
   → QUERIES_TABLE edges  (operation, connection, via=query_builder)

2. DB::select/insert/update/delete/statement() with inline SQL strings
   → QUERIES_TABLE edges  (via=raw_sql, parsed from SQL text)

3. ->join() / ->leftJoin() / ->rightJoin() within query chains
   → QUERIES_TABLE edges  (operation=read, via=query_builder)

4. Eloquent Model::staticMethod() calls (Order::where, Payment::create …)
   → QUERIES_TABLE edges  (via=eloquent)

5. $model->some_id = $expr  and  'some_id' => $expr  in array context
   → write_path_evidence stored on DatabaseColumn
   → INFERRED_REFERENCES edges when target can be resolved

6. if ($col === 'value') / switch guard patterns around *_id column usage
   → guard_conditions stored on DatabaseColumn

7. Column pair detection: tables where both *_id and *_type exist side-by-side
   → polymorphic_candidate + sibling_type_column set on DatabaseColumn

All writes to the graph use SET where possible (avoids full node re-creation)
and silently skip on failure — this phase is best-effort / additive.
"""

from __future__ import annotations

import bisect
import json
import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Compiled patterns ─────────────────────────────────────────────────────────

# DB::table('name')  or  DB::connection('conn')->table('name')
_DB_TABLE_RE = re.compile(
    r"\bDB::(?:connection\(\s*['\"]([^'\"]+)['\"]\s*\)\s*->\s*)?table\(\s*['\"]([^'\"]+)['\"]\s*\)",
    re.MULTILINE,
)

# DB::select/insert/update/delete/statement('raw SQL ...')
_DB_RAW_RE = re.compile(
    r"\bDB::(select|insert|update|delete|statement)\s*\(\s*['\"]([^'\"]{0,500})['\"]",
    re.MULTILINE | re.DOTALL,
)

# FROM / INTO / UPDATE in raw SQL strings
_SQL_FROM_RE = re.compile(r"\bFROM\s+[`\"]?(\w+)[`\"]?", re.IGNORECASE)
_SQL_INTO_RE = re.compile(r"\bINTO\s+[`\"]?(\w+)[`\"]?", re.IGNORECASE)
_SQL_UPDATE_RE = re.compile(r"\bUPDATE\s+[`\"]?(\w+)[`\"]?", re.IGNORECASE)
_SQL_JOIN_RE = re.compile(r"\bJOIN\s+[`\"]?(\w+)[`\"]?", re.IGNORECASE)

# ->join('table', ...) / ->leftJoin / ->rightJoin / ->crossJoin
_JOIN_RE = re.compile(
    r"->\s*(?:left|right|cross|inner)?[Jj]oin\(\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)

# Eloquent: ClassName::method(  (ClassName starts with uppercase)
_ELOQUENT_STATIC_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]+)::"
    r"(where|whereIn|whereNotIn|whereHas|with|find|findOrFail|findMany"
    r"|create|forceCreate|insert|insertOrIgnore|insertGetId|upsert"
    r"|update|updateOrCreate|firstOrCreate|firstOrNew|firstOrFail|firstWhere"
    r"|delete|destroy|forceDelete|restore|truncate"
    r"|get|first|all|count|exists|doesntExist|paginate|simplePaginate"
    r"|chunk|chunkById|lazy|cursor|pluck|value|select|selectRaw"
    r"|orderBy|groupBy|having|join|leftJoin)\s*\(",
    re.MULTILINE,
)

_INSTANCE_WRITE_RE = re.compile(
    r"\$(\w+)\s*->\s*(save|update|delete|forceDelete|restore|push|increment|decrement"
    r"|fill|forceFill|saveOrFail|saveQuietly|updateQuietly|deleteQuietly)\s*\(",
    re.MULTILINE,
)

_RELATION_WRITE_RE = re.compile(
    r"\$\w+\s*->\s*(\w+)\s*\(\s*\)\s*->\s*(create|createMany|save|saveMany"
    r"|update|updateOrCreate|firstOrCreate|firstOrNew|attach|detach|sync"
    r"|syncWithoutDetaching|toggle|updateExistingPivot)\s*\(",
    re.MULTILINE,
)

# Read-only Eloquent/QB operations
_READ_METHODS = frozenset({
    "where", "whereIn", "whereNotIn", "whereHas", "with", "find", "findOrFail",
    "findMany", "get", "first", "all", "count", "exists", "doesntExist",
    "paginate", "simplePaginate", "chunk", "chunkById", "lazy", "cursor",
    "pluck", "value", "select", "selectRaw", "orderBy", "groupBy", "having",
    "join", "leftJoin", "firstWhere", "firstOrFail",
})
_WRITE_METHODS = frozenset({
    "create", "forceCreate", "insert", "insertOrIgnore", "insertGetId", "upsert",
    "update", "updateOrCreate", "firstOrCreate", "firstOrNew",
    "delete", "destroy", "forceDelete", "restore", "truncate",
})

# Write-path: direct property assignment  $model->col_id = $expr;
_PROP_WRITE_RE = re.compile(
    r"\$(\w+)\s*->\s*(\w+)\s*=\s*([^;=\r\n][^;\r\n]{0,120}?)\s*;",
    re.MULTILINE,
)

# Write-path: array key in create/fill/update context  'col_id' => $expr
_ARRAY_KV_RE = re.compile(
    r"['\"](\w+)['\"]\s*=>\s*([^\,\]\}\r\n]{1,120}?)(?=\s*[,\]\}])",
    re.MULTILINE,
)

# Guard: if ($var === 'value')  or  if ($var == 'value')
_IF_GUARD_RE = re.compile(
    r"if\s*\([^)]*?\$(\w+)\s*=={1,2}=?\s*['\"]([^'\"]{1,40})['\"]",
    re.MULTILINE,
)

# Guard: switch ($var)
_SWITCH_VAR_RE = re.compile(r"switch\s*\(\s*\$(\w+)\s*\)", re.MULTILINE)

# Guard: case 'value':
_CASE_VAL_RE = re.compile(r"case\s+['\"]([^'\"]{1,40})['\"]\s*:", re.MULTILINE)

# RHS type inference helpers
_RHS_MODEL_PROP_RE = re.compile(r"^\$(\w+)->(\w+)$")   # $order->id
_RHS_STATIC_RE = re.compile(r"^([A-Z]\w+)::(\w+)$")     # Model::CONST


# ── Lookup builders ───────────────────────────────────────────────────────────

def _build_method_file_map(ctx: PipelineContext) -> dict[str, list[tuple[int, int, str, str]]]:
    """Return {file_path: [(line_start, line_end, node_id, fqn)]} sorted by line_start."""
    result: dict[str, list[tuple[int, int, str, str]]] = {}
    try:
        rows = ctx.db.execute(
            "MATCH (m:Method) "
            "RETURN m.file_path AS fp, m.line_start AS ls, m.line_end AS le, "
            "m.node_id AS nid, m.fqn AS fqn"
        )
        for r in rows:
            fp = r.get("fp") or ""
            ls = int(r.get("ls") or 0)
            le = int(r.get("le") or 0)
            nid = r.get("nid") or ""
            fqn = r.get("fqn") or ""
            if fp and nid:
                result.setdefault(fp, []).append((ls, le, nid, fqn))
    except Exception as exc:
        logger.debug("Method map build failed", error=str(exc))

    for v in result.values():
        v.sort()
    return result


def _build_model_lookup(ctx: PipelineContext) -> dict[str, dict[str, Any]]:
    """Return {ShortClassName: {nid, fqn, db_table, connection}} for all EloquentModels."""
    result: dict[str, dict[str, Any]] = {}
    try:
        rows = ctx.db.execute(
            "MATCH (m:EloquentModel) "
            "RETURN m.node_id AS nid, m.name AS name, m.fqn AS fqn, "
            "m.db_table AS db_table"
        )
        for r in rows:
            name = r.get("name") or ""
            if name:
                result[name] = {
                    "nid": r.get("nid") or "",
                    "fqn": r.get("fqn") or "",
                    "db_table": r.get("db_table") or "",
                }
    except Exception as exc:
        logger.debug("Model lookup build failed", error=str(exc))
    return result


def _build_table_lookup(ctx: PipelineContext) -> dict[str, str]:
    """Return {table_name: node_id} preferring live_db nodes over migration stubs."""
    # Two-pass: first collect all, then prefer live_db over migration/stub
    by_name: dict[str, tuple[str, str]] = {}  # name → (nid, source)
    try:
        rows = ctx.db.execute(
            "MATCH (t:DatabaseTable) "
            "RETURN t.name AS name, t.node_id AS nid, t.source AS source"
        )
        for r in rows:
            name = r.get("name") or ""
            nid = r.get("nid") or ""
            source = r.get("source") or "migration"
            if not name or not nid:
                continue
            existing = by_name.get(name)
            if existing is None:
                by_name[name] = (nid, source)
            elif source == "live_db" and existing[1] != "live_db":
                by_name[name] = (nid, source)
    except Exception as exc:
        logger.debug("Table lookup build failed", error=str(exc))

    return {name: nid for name, (nid, _) in by_name.items()}


def _build_column_lookup(ctx: PipelineContext) -> dict[str, str]:
    """Return {table.column: node_id} for all columns in the graph."""
    result: dict[str, str] = {}
    try:
        rows = ctx.db.execute(
            "MATCH (c:DatabaseColumn) "
            "RETURN c.table_name AS tbl, c.name AS col, c.node_id AS nid"
        )
        for r in rows:
            tbl = r.get("tbl") or ""
            col = r.get("col") or ""
            nid = r.get("nid") or ""
            if tbl and col and nid:
                result[f"{tbl}.{col}"] = nid
    except Exception as exc:
        logger.debug("Column lookup build failed", error=str(exc))
    return result


# ── Method context lookup ─────────────────────────────────────────────────────

def _method_at_line(
    file_methods: list[tuple[int, int, str, str]],
    line: int,
) -> tuple[str, str] | None:
    """Binary-search the method containing `line`. Returns (node_id, fqn) or None."""
    if not file_methods:
        return None
    starts = [m[0] for m in file_methods]
    idx = bisect.bisect_right(starts, line) - 1
    if idx < 0:
        return None
    ls, le, nid, fqn = file_methods[idx]
    if ls <= line <= le:
        return nid, fqn
    return None


# ── Operation classification ──────────────────────────────────────────────────

def _classify_operation(method_name: str) -> str:
    if method_name in _READ_METHODS:
        return "read"
    if method_name in _WRITE_METHODS:
        return "write"
    return "readwrite"


def _sql_operation(db_method: str) -> str:
    if db_method in ("select",):
        return "read"
    if db_method in ("insert", "delete",):
        return "write"
    if db_method == "update":
        return "write"
    return "readwrite"  # statement — unknown


# ── RHS type inference ────────────────────────────────────────────────────────

def _infer_rhs(rhs: str, model_lookup: dict[str, dict]) -> dict[str, Any]:
    """
    Return {type, target_table, target_column, confidence} for an RHS expression.
    Examples:
      "$order->id"       → {type: model_prop, target_table: orders, target_column: id, confidence: 0.85}
      "$request->input()" → {type: external, confidence: 0.1}
      "null"             → {type: literal_null, confidence: 1.0}
    """
    rhs = rhs.strip().rstrip(";").strip()

    # Null / literal
    if rhs.lower() in ("null", "true", "false", "0", "1", "''", '""'):
        return {"type": "literal", "value": rhs, "confidence": 1.0}

    # $model->property
    m = _RHS_MODEL_PROP_RE.match(rhs)
    if m:
        var_name, prop = m.group(1), m.group(2)
        # Try to match variable name → model (e.g. $order → Order)
        candidate = var_name.capitalize()
        model_info = model_lookup.get(candidate)
        if model_info and model_info.get("db_table"):
            return {
                "type": "model_property",
                "var": var_name,
                "prop": prop,
                "target_table": model_info["db_table"],
                "target_column": prop,
                "confidence": 0.85,
            }
        # Unknown model but pattern is recognisable
        return {
            "type": "model_property",
            "var": var_name,
            "prop": prop,
            "target_table": f"{var_name}s",  # naive plural guess
            "target_column": prop,
            "confidence": 0.35,
        }

    # $request->input / $request->get / $data[...] → external
    if re.match(r"^\$\w+->(?:input|get|query|post|all|validated)\s*\(", rhs):
        return {"type": "external_input", "confidence": 0.05}

    # Integer / numeric literal
    if re.match(r"^\d+$", rhs):
        return {"type": "literal_int", "value": rhs, "confidence": 1.0}

    # Just a plain variable $someId — no info
    if re.match(r"^\$\w+$", rhs):
        return {"type": "variable", "var": rhs, "confidence": 0.1}

    return {"type": "unknown", "rhs": rhs, "confidence": 0.05}


# ── Graph write helpers ───────────────────────────────────────────────────────

def _upsert_queries_table(
    ctx: PipelineContext,
    method_nid: str,
    table_nid: str,
    operation: str,
    connection: str,
    via: str,
    confidence: float,
    line: int,
) -> None:
    try:
        ctx.db.upsert_rel(
            "QUERIES_TABLE",
            "Method", method_nid,
            "DatabaseTable", table_nid,
            {
                "operation": operation,
                "connection": connection,
                "via": via,
                "confidence": confidence,
                "line": line,
            },
        )
    except Exception as exc:
        logger.debug("QUERIES_TABLE upsert failed", error=str(exc))


def _set_column_property(ctx: PipelineContext, col_nid: str, prop: str, value: Any) -> None:
    """SET a single property on an existing DatabaseColumn node via Cypher."""
    try:
        if isinstance(value, bool):
            ctx.db.execute(
                f"MATCH (c:DatabaseColumn {{node_id: $nid}}) SET c.{prop} = {str(value).lower()}",
                {"nid": col_nid},
            )
        elif isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            ctx.db.execute(
                f"MATCH (c:DatabaseColumn {{node_id: $nid}}) SET c.{prop} = '{escaped}'",
                {"nid": col_nid},
            )
        elif isinstance(value, (int, float)):
            ctx.db.execute(
                f"MATCH (c:DatabaseColumn {{node_id: $nid}}) SET c.{prop} = {value}",
                {"nid": col_nid},
            )
    except Exception as exc:
        logger.debug("Column SET failed", col=col_nid, prop=prop, error=str(exc))


# ── Per-file scanner ──────────────────────────────────────────────────────────

def _get_line_number(source: str, pos: int) -> int:
    """Return 1-based line number for a byte offset in source."""
    return source[:pos].count("\n") + 1


def _context_window(source: str, pos: int, radius: int = 3) -> str:
    """Return `radius` lines around the match position for evidence storage."""
    lines = source.splitlines()
    line_no = source[:pos].count("\n")
    start = max(0, line_no - radius)
    end = min(len(lines), line_no + radius + 1)
    return "\n".join(lines[start:end])


def _scan_file(
    ctx: PipelineContext,
    php_path: Path,
    method_file_map: dict[str, list[tuple[int, int, str, str]]],
    model_lookup: dict[str, dict],
    table_lookup: dict[str, str],
    column_lookup: dict[str, str],
    # accumulators written back to graph after scanning
    write_evidence: dict[str, list[dict]],   # col_nid → [evidence]
    guard_evidence: dict[str, list[dict]],   # col_nid → [guard]
) -> dict[str, int]:
    stats = {"queries_table": 0, "write_evidence": 0, "guard_hits": 0, "dynamic_table_refs": 0}
    file_str = str(php_path)
    file_methods = method_file_map.get(file_str, [])

    try:
        source = php_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return stats

    db = ctx.db
    default_connection = ctx.config.databases[0].name if ctx.config.databases else "default"

    # ── 1. DB::table() and DB::connection()->table() ──────────────────────────
    for m in _DB_TABLE_RE.finditer(source):
        explicit_conn = m.group(1) or ""
        table_name = m.group(2)
        line = _get_line_number(source, m.start())
        connection = explicit_conn or default_connection

        table_nid = table_lookup.get(table_name)
        if not table_nid:
            # Create a stub so the edge can be made
            table_nid = make_node_id("table", connection, table_name)
            try:
                db.upsert_node("DatabaseTable", {
                    "node_id": table_nid,
                    "name": table_name,
                    "connection": connection,
                    "created_in": "",
                    "engine": "",
                    "charset": "",
                    "table_comment": "",
                    "source": "stub",
                })
                table_lookup[table_name] = table_nid
            except Exception:
                continue

        # Detect operation from surrounding chain (look 200 chars ahead)
        window = source[m.end():m.end() + 200]
        chain_match = re.search(r"->\s*(\w+)\s*\(", window)
        operation = "readwrite"
        if chain_match:
            operation = _classify_operation(chain_match.group(1))

        method_ctx = _method_at_line(file_methods, line)
        if method_ctx:
            method_nid, _ = method_ctx
            _upsert_queries_table(ctx, method_nid, table_nid, operation, connection, "query_builder", 0.95, line)
            stats["queries_table"] += 1

    _dynamic_table_re = re.compile(
        r"\bDB::(?:connection\([^)]*\)\s*->\s*)?table\(\s*\$(\w+)",
        re.MULTILINE,
    )
    for m in _dynamic_table_re.finditer(source):
        line = _get_line_number(source, m.start())
        method_ctx = _method_at_line(file_methods, line)
        if method_ctx:
            method_nid, method_fqn = method_ctx
            try:
                ctx.db.execute(
                    "MATCH (m:Method {node_id: $nid}) SET m.has_dynamic_table_ref = true",
                    {"nid": method_nid},
                )
            except Exception:
                pass
            stats["dynamic_table_refs"] += 1

    for m in _JOIN_RE.finditer(source):
        table_name = m.group(1)
        line = _get_line_number(source, m.start())
        table_nid = table_lookup.get(table_name)
        if not table_nid:
            continue
        method_ctx = _method_at_line(file_methods, line)
        if method_ctx:
            method_nid, _ = method_ctx
            _upsert_queries_table(ctx, method_nid, table_nid, "read", default_connection, "query_builder", 0.90, line)
            stats["queries_table"] += 1

    # ── 3. DB::select/insert/update/delete/statement('raw SQL') ──────────────
    for m in _DB_RAW_RE.finditer(source):
        db_method = m.group(1)
        sql_fragment = m.group(2)
        line = _get_line_number(source, m.start())
        operation = _sql_operation(db_method)

        # Extract table names from the SQL fragment
        tables_in_sql: set[str] = set()
        for pattern, op_override in [
            (_SQL_FROM_RE, "read"),
            (_SQL_INTO_RE, "write"),
            (_SQL_UPDATE_RE, "write"),
            (_SQL_JOIN_RE, "read"),
        ]:
            for sql_m in pattern.finditer(sql_fragment):
                tbl = sql_m.group(1).lower()
                if tbl not in {"dual", "null", "values", "set"}:
                    tables_in_sql.add(tbl)

        method_ctx = _method_at_line(file_methods, line)
        if not method_ctx:
            continue
        method_nid, _ = method_ctx

        for tbl in tables_in_sql:
            tbl_nid = table_lookup.get(tbl)
            if tbl_nid:
                _upsert_queries_table(ctx, method_nid, tbl_nid, operation, default_connection, "raw_sql", 0.75, line)
                stats["queries_table"] += 1

    # ── 4. Eloquent Model::staticMethod() ────────────────────────────────────
    for m in _ELOQUENT_STATIC_RE.finditer(source):
        class_name = m.group(1)
        method_name = m.group(2)
        model_info = model_lookup.get(class_name)
        if not model_info:
            continue

        db_table = model_info.get("db_table") or ""
        if not db_table:
            continue

        line = _get_line_number(source, m.start())
        tbl_nid = table_lookup.get(db_table)
        if not tbl_nid:
            continue

        operation = _classify_operation(method_name)
        method_ctx = _method_at_line(file_methods, line)
        if method_ctx:
            method_nid, _ = method_ctx
            _upsert_queries_table(ctx, method_nid, tbl_nid, operation, default_connection, "eloquent", 0.90, line)
            stats["queries_table"] += 1

    for m in _INSTANCE_WRITE_RE.finditer(source):
        var_name = m.group(1)
        method_name = m.group(2)
        line = _get_line_number(source, m.start())

        var_class = var_name[0].upper() + var_name[1:] if var_name else ""
        model_info = model_lookup.get(var_class)
        if not model_info:
            for candidate in (var_name.capitalize(), var_name.title().replace("_", "")):
                model_info = model_lookup.get(candidate)
                if model_info:
                    break
        if not model_info or not model_info.get("db_table"):
            continue

        db_table = model_info["db_table"]
        tbl_nid = table_lookup.get(db_table)
        if not tbl_nid:
            continue

        operation = "write" if method_name in ("save", "update", "delete", "forceDelete",
                                                 "saveOrFail", "saveQuietly", "updateQuietly",
                                                 "deleteQuietly", "push", "fill", "forceFill") else "write"
        operation = "delete" if "delete" in method_name.lower() else "write"
        method_ctx = _method_at_line(file_methods, line)
        if method_ctx:
            method_nid, _ = method_ctx
            _upsert_queries_table(ctx, method_nid, tbl_nid, operation, default_connection,
                                  "eloquent_instance", 0.85, line)
            stats["queries_table"] += 1

    for m in _RELATION_WRITE_RE.finditer(source):
        relation_name = m.group(1)
        chain_method = m.group(2)
        line = _get_line_number(source, m.start())

        method_ctx = _method_at_line(file_methods, line)
        if not method_ctx:
            continue
        method_nid, method_fqn = method_ctx

        class_fqn = "::".join(method_fqn.split("::")[:-1]) if "::" in method_fqn else ""
        if not class_fqn:
            continue

        try:
            rel_rows = ctx.db.execute(
                "MATCH (c:Class_)-[:DEFINES]->(m:Method {name: $rname}) "
                "WHERE c.fqn = $cfqn "
                "RETURN m.node_id AS mnid LIMIT 1",
                {"cfqn": class_fqn, "rname": relation_name},
            )
        except Exception:
            rel_rows = []

        if not rel_rows:
            continue

        try:
            related_rows = ctx.db.execute(
                "MATCH (m:Method {node_id: $mnid})<-[:DEFINES]-(c)-[:HAS_RELATIONSHIP]->"
                "(related:EloquentModel) "
                "WHERE m.name = $rname "
                "RETURN related.db_table AS tbl LIMIT 1",
                {"mnid": rel_rows[0].get("mnid", ""), "rname": relation_name},
            )
        except Exception:
            related_rows = []

        if not related_rows:
            for model_name, info in model_lookup.items():
                snake = re.sub(r"(?<!^)(?=[A-Z])", "_", model_name).lower()
                if relation_name == snake or relation_name == snake + "s" or relation_name == model_name[0].lower() + model_name[1:] + "s":
                    db_table = info.get("db_table", "")
                    if db_table:
                        tbl_nid = table_lookup.get(db_table)
                        if tbl_nid:
                            operation = "write" if chain_method in ("create", "createMany", "save",
                                                                     "saveMany", "update", "updateOrCreate",
                                                                     "firstOrCreate") else "write"
                            _upsert_queries_table(ctx, method_nid, tbl_nid, operation,
                                                  default_connection, "eloquent_relation", 0.75, line)
                            stats["queries_table"] += 1
                    break
            continue

        db_table = related_rows[0].get("tbl", "")
        if db_table:
            tbl_nid = table_lookup.get(db_table)
            if tbl_nid:
                operation = "write" if chain_method in ("create", "createMany", "save",
                                                         "saveMany", "update", "updateOrCreate",
                                                         "firstOrCreate") else "write"
                _upsert_queries_table(ctx, method_nid, tbl_nid, operation,
                                      default_connection, "eloquent_relation", 0.80, line)
                stats["queries_table"] += 1

    for m in _PROP_WRITE_RE.finditer(source):
        var_name = m.group(1)
        col_name = m.group(2)
        rhs = m.group(3).strip()
        line = _get_line_number(source, m.start())

        # Only care about *_id, *_uuid, *_key columns — high-value reference columns
        if not (col_name.endswith("_id") or col_name.endswith("_uuid") or col_name.endswith("_key")):
            continue

        method_ctx = _method_at_line(file_methods, line)
        method_fqn = method_ctx[1] if method_ctx else ""

        # Try to resolve which table this belongs to via variable type
        # $order->user_id = ... → we need to know $order is an Order model
        var_class = var_name.capitalize()
        model_info = model_lookup.get(var_class)
        table_name = model_info["db_table"] if model_info else ""

        rhs_info = _infer_rhs(rhs, model_lookup)
        context_snippet = _context_window(source, m.start())

        evidence = {
            "method_fqn": method_fqn,
            "line": line,
            "var": var_name,
            "rhs": rhs[:100],
            "rhs_info": rhs_info,
            "context": context_snippet,
            "confidence": rhs_info.get("confidence", 0.3),
        }

        # Find column node
        if table_name:
            col_key = f"{table_name}.{col_name}"
            col_nid = column_lookup.get(col_key)
            if col_nid:
                write_evidence.setdefault(col_nid, []).append(evidence)
                stats["write_evidence"] += 1

    # ── 6. Write-path: array key 'col_id' => $expr ───────────────────────────
    for m in _ARRAY_KV_RE.finditer(source):
        col_name = m.group(1)
        rhs = m.group(2).strip()
        line = _get_line_number(source, m.start())

        if not (col_name.endswith("_id") or col_name.endswith("_uuid")):
            continue

        method_ctx = _method_at_line(file_methods, line)
        method_fqn = method_ctx[1] if method_ctx else ""
        rhs_info = _infer_rhs(rhs, model_lookup)
        context_snippet = _context_window(source, m.start())

        evidence = {
            "method_fqn": method_fqn,
            "line": line,
            "rhs": rhs[:100],
            "rhs_info": rhs_info,
            "context": context_snippet,
            "confidence": rhs_info.get("confidence", 0.25),
            "via": "array",
        }

        # We don't know the table here without more context — store against any
        # known column with this name across all tables
        for col_key, col_nid in column_lookup.items():
            if col_key.endswith(f".{col_name}"):
                write_evidence.setdefault(col_nid, []).append(evidence)
                stats["write_evidence"] += 1
                break  # first match — avoid duplicating evidence

    # ── 7. Guard patterns: if ($type === 'order') ─────────────────────────────
    for m in _IF_GUARD_RE.finditer(source):
        condition_var = m.group(1)
        condition_val = m.group(2)
        line = _get_line_number(source, m.start())
        method_ctx = _method_at_line(file_methods, line)
        method_fqn = method_ctx[1] if method_ctx else ""

        # Find *_id column usage within the next 300 chars after this guard
        window = source[m.end():m.end() + 300]
        for col_m in re.finditer(r"['\"](\w+_id)['\"]|->(\w+_id)\b", window):
            col_name = col_m.group(1) or col_m.group(2)
            guard = {
                "condition_var": condition_var,
                "condition_val": condition_val,
                "method_fqn": method_fqn,
                "line": line,
            }
            for col_key, col_nid in column_lookup.items():
                if col_key.endswith(f".{col_name}"):
                    guard_evidence.setdefault(col_nid, []).append(guard)
                    stats["guard_hits"] += 1
                    break

    return stats


# ── Column pair detection ─────────────────────────────────────────────────────

def _detect_polymorphic_pairs(
    ctx: PipelineContext,
    column_lookup: dict[str, str],
) -> int:
    """
    Find tables where both col_name and col_name[:-3]+'_type' exist.
    e.g. reference_id + reference_type, entity_id + entity_type.
    Mark the *_id column as polymorphic_candidate.
    """
    # Group columns by table
    by_table: dict[str, set[str]] = {}
    for col_key in column_lookup:
        tbl, col = col_key.split(".", 1)
        by_table.setdefault(tbl, set()).add(col)

    marked = 0
    for tbl, cols in by_table.items():
        for col in cols:
            if not col.endswith("_id"):
                continue
            # Check for sibling *_type column
            base = col[:-3]  # strip '_id'
            type_col = base + "_type"
            if type_col in cols:
                col_nid = column_lookup.get(f"{tbl}.{col}")
                type_col_nid = column_lookup.get(f"{tbl}.{type_col}")
                if col_nid:
                    _set_column_property(ctx, col_nid, "polymorphic_candidate", True)
                    _set_column_property(ctx, col_nid, "sibling_type_column", type_col)
                    marked += 1
            # Also check *able_type / *able_id pattern (Laravel morph convention)
            elif col.endswith("able_id"):
                able_base = col[:-7]  # strip 'able_id'
                morph_type = able_base + "able_type"
                if morph_type in cols:
                    col_nid = column_lookup.get(f"{tbl}.{col}")
                    if col_nid:
                        _set_column_property(ctx, col_nid, "polymorphic_candidate", True)
                        _set_column_property(ctx, col_nid, "sibling_type_column", morph_type)
                        marked += 1

    return marked


# ── Inferred relationship edges ───────────────────────────────────────────────

def _build_inferred_relationships(
    ctx: PipelineContext,
    write_evidence: dict[str, list[dict]],
    table_lookup: dict[str, str],
    column_lookup: dict[str, str],
    guard_evidence: dict[str, list[dict]],
) -> int:
    """
    For each column with write-path evidence, group by target table and
    create INFERRED_REFERENCES edges with a confidence score.
    """
    db = ctx.db
    edges_created = 0

    # col_nid → {target_table: {evidence_list, conditions}}
    inference_map: dict[str, dict[str, dict]] = {}

    for col_nid, evidence_list in write_evidence.items():
        for ev in evidence_list:
            rhs_info = ev.get("rhs_info", {})
            target_table = rhs_info.get("target_table", "")
            confidence = float(rhs_info.get("confidence", 0.0))
            if not target_table or confidence < 0.2:
                continue

            entry = inference_map.setdefault(col_nid, {}).setdefault(target_table, {
                "confidence_sum": 0.0,
                "count": 0,
                "evidence_types": set(),
                "conditions": [],
            })
            entry["confidence_sum"] += confidence
            entry["count"] += 1
            entry["evidence_types"].add("write_path")

    # Merge guard conditions
    for col_nid, guards in guard_evidence.items():
        for g in guards:
            # Each guard potentially maps condition → target table
            # We can't resolve the target table without the write-path evidence
            # so just annotate existing entries with the condition
            for target_table, entry in inference_map.get(col_nid, {}).items():
                entry["conditions"].append({
                    "when_var": g["condition_var"],
                    "when_val": g["condition_val"],
                })
                entry["evidence_types"].add("guard_pattern")

    # Create graph edges
    for col_nid, targets in inference_map.items():
        for target_table, entry in targets.items():
            target_nid = table_lookup.get(target_table)
            if not target_nid:
                continue

            count = entry["count"]
            confidence_avg = min(entry["confidence_sum"] / count, 1.0) if count else 0.0
            # Boost confidence if seen multiple times
            confidence_boosted = min(confidence_avg + 0.05 * min(count - 1, 4), 1.0)

            evidence_types = list(entry["evidence_types"])
            conditions = entry["conditions"]
            condition_str = conditions[0]["when_val"] if len(conditions) == 1 else ""

            summary_parts = [f"Seen {count}x in write-path analysis"]
            if conditions:
                cond_vals = list({c["when_val"] for c in conditions})
                summary_parts.append(f"Guard conditions: {', '.join(cond_vals[:5])}")
            evidence_summary = ". ".join(summary_parts)

            try:
                db.upsert_rel(
                    "INFERRED_REFERENCES",
                    "DatabaseColumn", col_nid,
                    "DatabaseTable", target_nid,
                    {
                        "confidence": round(confidence_boosted, 3),
                        "condition": condition_str,
                        "evidence_type": ",".join(evidence_types),
                        "evidence_detail": evidence_summary,
                    },
                )
                edges_created += 1
            except Exception as exc:
                logger.debug("INFERRED_REFERENCES edge failed", error=str(exc))

    return edges_created


# ── Phase entry point ─────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> None:
    """Scan all PHP files for DB access patterns — static analysis, zero AI cost."""
    if not ctx.php_files:
        logger.info("No PHP files to scan — skipping DB access analysis")
        return

    logger.info("Building DB access analysis lookups")
    method_file_map = _build_method_file_map(ctx)
    model_lookup = _build_model_lookup(ctx)
    table_lookup = _build_table_lookup(ctx)
    column_lookup = _build_column_lookup(ctx)

    # Accumulators — flush to graph after scanning all files
    write_evidence: dict[str, list[dict]] = {}
    guard_evidence: dict[str, list[dict]] = {}

    total_queries = 0
    total_write_ev = 0
    total_guard = 0
    total_dynamic = 0
    files_scanned = 0

    logger.info("Scanning PHP files for DB access patterns", files=len(ctx.php_files))

    for php_path in ctx.php_files:
        try:
            stats = _scan_file(
                ctx, php_path,
                method_file_map, model_lookup, table_lookup, column_lookup,
                write_evidence, guard_evidence,
            )
            total_queries += stats["queries_table"]
            total_write_ev += stats["write_evidence"]
            total_guard += stats["guard_hits"]
            total_dynamic += stats.get("dynamic_table_refs", 0)
            files_scanned += 1
        except Exception as exc:
            logger.debug("File scan failed", path=str(php_path), error=str(exc))

    # ── Flush write-path evidence to column nodes ─────────────────────────────
    logger.info("Writing evidence to graph", columns_with_evidence=len(write_evidence))
    for col_nid, ev_list in write_evidence.items():
        # Cap at 20 most confident pieces of evidence to keep JSON small
        ev_list.sort(key=lambda e: e.get("confidence", 0), reverse=True)
        _set_column_property(ctx, col_nid, "write_path_evidence", json.dumps(ev_list[:20]))

    # ── Flush guard evidence to column nodes ──────────────────────────────────
    for col_nid, guard_list in guard_evidence.items():
        # Deduplicate by (condition_var, condition_val)
        seen: set[tuple] = set()
        deduped = []
        for g in guard_list:
            key = (g["condition_var"], g["condition_val"])
            if key not in seen:
                seen.add(key)
                deduped.append(g)
        _set_column_property(ctx, col_nid, "guard_conditions", json.dumps(deduped[:30]))

    # ── Detect polymorphic column pairs ───────────────────────────────────────
    pairs_marked = _detect_polymorphic_pairs(ctx, column_lookup)

    # ── Build inferred relationship edges ─────────────────────────────────────
    inferred = _build_inferred_relationships(
        ctx, write_evidence, table_lookup, column_lookup, guard_evidence
    )

    ctx.stats["db_access_queries_table"] = total_queries
    ctx.stats["db_access_dynamic_table_refs"] = total_dynamic
    ctx.stats["db_access_write_evidence"] = total_write_ev
    ctx.stats["db_access_guard_hits"] = total_guard
    ctx.stats["db_access_polymorphic_pairs"] = pairs_marked
    ctx.stats["db_access_inferred_refs"] = inferred

    logger.info(
        "DB access analysis complete",
        files=files_scanned,
        queries_table_edges=total_queries,
        write_evidence_columns=len(write_evidence),
        guard_hits=total_guard,
        polymorphic_pairs=pairs_marked,
        inferred_references=inferred,
    )
