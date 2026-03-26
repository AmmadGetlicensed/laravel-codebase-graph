"""Phase 24 — Live Database Introspection.

Connects to each configured database (config.databases) and pulls the ground
truth schema directly from information_schema:

  - Tables + columns (with full type info, comments, keys)
  - Foreign key constraints (enforced at DB level)
  - Stored procedures (with body for later SQL parsing)
  - Database views (with definition SQL)
  - Stored triggers (optional, off by default)

Nodes created: DatabaseConnection, DatabaseTable, DatabaseColumn,
               StoredProcedure, DatabaseView
Edges created: HAS_TABLE, HAS_COLUMN, HAS_PROCEDURE, HAS_VIEW,
               REFERENCES_TABLE (live FKs)

This phase runs after the migration-based phase_19, and its nodes use
connection-prefixed node_ids (e.g. "table:default:users") so they coexist
with migration-derived nodes without collision.  Phase 25 then links
EloquentModel → live DatabaseTable via USES_TABLE.

If no databases are configured (config.databases is empty), this phase
exits silently — the migration-based schema from phase_19 is used as-is.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


# ── Connection helpers ────────────────────────────────────────────────────────

def _resolve_env(value: str) -> str:
    """Expand ${VAR_NAME} references from environment variables."""
    import os
    return re.sub(r"\$\{([^}]+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def _connect_mysql(cfg: Any) -> Any:
    """Return an open pymysql connection for the given DatabaseConnectionConfig."""
    try:
        import pymysql  # type: ignore[import]
    except ImportError:
        raise ImportError(
            "pymysql is required for live database introspection.\n"
            "Install it: pip install pymysql\n"
            "Or reinstall laravelgraph after adding it: pipx reinstall laravelgraph"
        )

    ssl_opts: dict | None = {"ssl": {}} if cfg.ssl else None

    if cfg.dsn:
        parsed = urlparse(cfg.dsn)
        return pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=parsed.username or "",
            password=_resolve_env(parsed.password or ""),
            database=(parsed.path or "").lstrip("/"),
            ssl=ssl_opts,
            connect_timeout=15,
            charset="utf8mb4",
        )

    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.username,
        password=_resolve_env(cfg.password),
        database=cfg.database,
        ssl=ssl_opts,
        connect_timeout=15,
        charset="utf8mb4",
    )


def _db_name_from_cfg(cfg: Any) -> str:
    """Extract the schema/database name from a connection config."""
    if cfg.dsn:
        parsed = urlparse(cfg.dsn)
        return (parsed.path or "").lstrip("/")
    return cfg.database


# ── SQL type normalisation ────────────────────────────────────────────────────

_TYPE_NORM: dict[str, str] = {
    "tinyint": "tinyinteger",
    "smallint": "smallinteger",
    "mediumint": "integer",
    "int": "integer",
    "bigint": "biginteger",
    "float": "float",
    "double": "double",
    "decimal": "decimal",
    "numeric": "decimal",
    "char": "string",
    "varchar": "string",
    "tinytext": "text",
    "text": "text",
    "mediumtext": "text",
    "longtext": "text",
    "tinyblob": "binary",
    "blob": "binary",
    "mediumblob": "binary",
    "longblob": "binary",
    "binary": "binary",
    "varbinary": "binary",
    "date": "date",
    "datetime": "datetime",
    "timestamp": "timestamp",
    "time": "time",
    "year": "year",
    "json": "json",
    "enum": "enum",
    "set": "set",
    "bit": "boolean",
    "bool": "boolean",
    "boolean": "boolean",
    "uuid": "string",
}


def _normalise_type(data_type: str) -> str:
    return _TYPE_NORM.get(data_type.lower(), data_type.lower())


# ── SQL body parser — extract table access from procedure/view bodies ─────────

_SQL_READ_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO\s+\w+\s+SELECT|EXISTS\s*\(SELECT)\s+[`\"]?(\w+)[`\"]?",
    re.IGNORECASE,
)
_SQL_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+(?:IGNORE\s+)?INTO|UPDATE|DELETE\s+FROM|REPLACE\s+INTO|TRUNCATE(?:\s+TABLE)?)\s+[`\"]?(\w+)[`\"]?",
    re.IGNORECASE,
)
_PARAM_RE = re.compile(
    r"\b(IN|OUT|INOUT)\s+(\w+)\s+([\w()]+)",
    re.IGNORECASE,
)


def _parse_sql_tables(body: str) -> tuple[list[str], list[str]]:
    """Return (reads, writes) table name lists extracted from a SQL body."""
    reads = list({m.group(1).lower() for m in _SQL_READ_RE.finditer(body)})
    writes = list({m.group(1).lower() for m in _SQL_WRITE_RE.finditer(body)})
    # Remove false positives (SQL keywords that look like table names)
    _keywords = {"dual", "values", "set", "null", "true", "false"}
    reads = [t for t in reads if t not in _keywords]
    writes = [t for t in writes if t not in _keywords]
    return reads, writes


# ── Per-database introspection ────────────────────────────────────────────────

def _introspect_one(ctx: PipelineContext, cfg: Any) -> dict[str, int]:
    """Introspect a single database connection and write nodes to the graph."""
    db = ctx.db
    conn_name = cfg.name
    schema_name = _db_name_from_cfg(cfg)

    stats: dict[str, int] = {
        "tables": 0, "columns": 0, "procedures": 0, "views": 0, "fks": 0,
    }

    logger.info("Connecting to database", connection=conn_name, schema=schema_name)

    try:
        mysql_conn = _connect_mysql(cfg)
    except Exception as exc:
        logger.error(
            "Failed to connect to database — skipping",
            connection=conn_name,
            error=str(exc),
        )
        ctx.errors.append(f"DB introspection: could not connect to '{conn_name}': {exc}")
        return stats

    try:
        with mysql_conn.cursor() as cur:
            # ── DatabaseConnection node ───────────────────────────────────────
            conn_nid = make_node_id("dbconn", conn_name)
            db.upsert_node("DatabaseConnection", {
                "node_id": conn_nid,
                "name": conn_name,
                "driver": cfg.driver,
                "host": cfg.host if not cfg.dsn else (urlparse(cfg.dsn).hostname or ""),
                "port": cfg.port if not cfg.dsn else (urlparse(cfg.dsn).port or 3306),
                "database": schema_name,
            })

            # ── Tables ────────────────────────────────────────────────────────
            cur.execute(
                """
                SELECT TABLE_NAME, ENGINE, TABLE_COLLATION, TABLE_COMMENT, TABLE_ROWS
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                ORDER BY TABLE_NAME
                """,
                (schema_name,),
            )
            tables = cur.fetchall()
            table_nid_map: dict[str, str] = {}

            for (tbl_name, engine, collation, tbl_comment, tbl_rows) in tables:
                tbl_nid = make_node_id("table", conn_name, tbl_name)
                table_nid_map[tbl_name] = tbl_nid

                db.upsert_node("DatabaseTable", {
                    "node_id": tbl_nid,
                    "name": tbl_name,
                    "connection": conn_name,
                    "created_in": "",
                    "engine": engine or "",
                    "charset": collation or "",
                    "table_comment": tbl_comment or "",
                    "source": "live_db",
                    "row_count": int(tbl_rows) if tbl_rows is not None else 0,
                })

                db.upsert_rel("HAS_TABLE", "DatabaseConnection", conn_nid, "DatabaseTable", tbl_nid)
                stats["tables"] += 1

            # ── Columns ───────────────────────────────────────────────────────
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, DATA_TYPE,
                       IS_NULLABLE, COLUMN_DEFAULT,
                       CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION,
                       COLUMN_KEY, EXTRA, COLUMN_COMMENT
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (schema_name,),
            )
            columns = cur.fetchall()

            for (
                tbl_name, col_name, col_type, data_type,
                is_nullable, col_default, char_len, num_prec,
                col_key, extra, col_comment,
            ) in columns:
                tbl_nid = table_nid_map.get(tbl_name)
                if not tbl_nid:
                    continue

                col_nid = make_node_id("column", conn_name, f"{tbl_name}.{col_name}")
                length = int(char_len) if char_len else (int(num_prec) if num_prec else 0)

                db.upsert_node("DatabaseColumn", {
                    "node_id": col_nid,
                    "name": col_name,
                    "table_name": tbl_name,
                    "connection": conn_name,
                    "type": _normalise_type(data_type),
                    "full_type": col_type or "",
                    "nullable": is_nullable == "YES",
                    "default_value": str(col_default) if col_default is not None else "",
                    "unique": col_key == "UNI",
                    "indexed": col_key in ("MUL", "UNI", "PRI"),
                    "unsigned": "unsigned" in (col_type or "").lower(),
                    "length": length,
                    "column_comment": col_comment or "",
                    "extra": extra or "",
                    "column_key": col_key or "",
                })

                db.upsert_rel("HAS_COLUMN", "DatabaseTable", tbl_nid, "DatabaseColumn", col_nid)
                stats["columns"] += 1

            # ── Foreign keys (enforced at DB level) ───────────────────────────
            cur.execute(
                """
                SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME,
                       kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME,
                       kcu.CONSTRAINT_NAME,
                       rc.UPDATE_RULE, rc.DELETE_RULE
                FROM information_schema.KEY_COLUMN_USAGE kcu
                JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
                    ON  kcu.CONSTRAINT_NAME   = rc.CONSTRAINT_NAME
                    AND kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
                WHERE kcu.TABLE_SCHEMA = %s
                  AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                """,
                (schema_name,),
            )
            fks = cur.fetchall()

            for (from_tbl, from_col, to_tbl, to_col, cname, on_update, on_delete) in fks:
                from_nid = table_nid_map.get(from_tbl)
                to_nid = table_nid_map.get(to_tbl)

                if not from_nid:
                    continue
                if not to_nid:
                    # Referenced table may be in another DB — create a stub
                    to_nid = make_node_id("table", conn_name, to_tbl)
                    try:
                        db.upsert_node("DatabaseTable", {
                            "node_id": to_nid,
                            "name": to_tbl,
                            "connection": conn_name,
                            "created_in": "",
                            "engine": "",
                            "charset": "",
                            "table_comment": "",
                            "source": "live_db",
                        })
                        table_nid_map[to_tbl] = to_nid
                    except Exception:
                        pass

                try:
                    db.upsert_rel(
                        "REFERENCES_TABLE",
                        "DatabaseTable", from_nid,
                        "DatabaseTable", to_nid,
                        {
                            "from_column": from_col or "",
                            "to_column": to_col or "",
                            "on_delete": on_delete or "",
                            "on_update": on_update or "",
                            "constraint_name": cname or "",
                            "enforced": True,
                        },
                    )
                    stats["fks"] += 1
                except Exception as exc:
                    logger.debug("FK edge failed", from_tbl=from_tbl, to_tbl=to_tbl, error=str(exc))

            # ── Stored procedures ─────────────────────────────────────────────
            if cfg.analyze_procedures:
                cur.execute(
                    """
                    SELECT ROUTINE_NAME, ROUTINE_TYPE, ROUTINE_DEFINITION,
                           ROUTINE_COMMENT
                    FROM information_schema.ROUTINES
                    WHERE ROUTINE_SCHEMA = %s
                      AND ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')
                    ORDER BY ROUTINE_NAME
                    """,
                    (schema_name,),
                )
                routines = cur.fetchall()

                # Fetch parameters separately (cleaner than parsing body)
                cur.execute(
                    """
                    SELECT SPECIFIC_NAME, PARAMETER_MODE, PARAMETER_NAME, DTD_IDENTIFIER
                    FROM information_schema.PARAMETERS
                    WHERE SPECIFIC_SCHEMA = %s
                      AND PARAMETER_MODE IS NOT NULL
                    ORDER BY SPECIFIC_NAME, ORDINAL_POSITION
                    """,
                    (schema_name,),
                )
                param_rows = cur.fetchall()
                params_by_proc: dict[str, list[dict]] = {}
                for (proc_name, mode, param_name, dtype) in param_rows:
                    params_by_proc.setdefault(proc_name, []).append({
                        "mode": mode or "IN",
                        "name": param_name or "",
                        "type": dtype or "",
                    })

                for (proc_name, routine_type, body, comment) in routines:
                    proc_nid = make_node_id("procedure", conn_name, proc_name)
                    params = params_by_proc.get(proc_name, [])
                    body_str = body or ""
                    preview = body_str[:1000] if body_str else ""

                    db.upsert_node("StoredProcedure", {
                        "node_id": proc_nid,
                        "name": proc_name,
                        "connection": conn_name,
                        "database": schema_name,
                        "routine_type": routine_type or "PROCEDURE",
                        "parameters": json.dumps(params),
                        "body_preview": preview,
                        "full_body": body_str,
                        "comment": comment or "",
                    })

                    db.upsert_rel(
                        "HAS_PROCEDURE",
                        "DatabaseConnection", conn_nid,
                        "StoredProcedure", proc_nid,
                    )

                    # Parse body to find which tables the procedure reads / writes
                    if body_str:
                        reads, writes = _parse_sql_tables(body_str)
                        for tbl in reads:
                            tbl_nid = table_nid_map.get(tbl)
                            if tbl_nid:
                                try:
                                    db.upsert_rel(
                                        "PROCEDURE_READS",
                                        "StoredProcedure", proc_nid,
                                        "DatabaseTable", tbl_nid,
                                        {"confidence": 0.85},
                                    )
                                except Exception:
                                    pass
                        for tbl in writes:
                            tbl_nid = table_nid_map.get(tbl)
                            if tbl_nid:
                                try:
                                    db.upsert_rel(
                                        "PROCEDURE_WRITES",
                                        "StoredProcedure", proc_nid,
                                        "DatabaseTable", tbl_nid,
                                        {"confidence": 0.85},
                                    )
                                except Exception:
                                    pass

                    stats["procedures"] += 1

            # ── Views ─────────────────────────────────────────────────────────
            if cfg.analyze_views:
                cur.execute(
                    """
                    SELECT TABLE_NAME, VIEW_DEFINITION, IS_UPDATABLE
                    FROM information_schema.VIEWS
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY TABLE_NAME
                    """,
                    (schema_name,),
                )
                views = cur.fetchall()

                for (view_name, definition, is_updatable) in views:
                    view_nid = make_node_id("view", conn_name, view_name)

                    db.upsert_node("DatabaseView", {
                        "node_id": view_nid,
                        "name": view_name,
                        "connection": conn_name,
                        "database": schema_name,
                        "definition": definition or "",
                        "is_updatable": is_updatable or "NO",
                    })

                    db.upsert_rel(
                        "HAS_VIEW",
                        "DatabaseConnection", conn_nid,
                        "DatabaseView", view_nid,
                    )
                    stats["views"] += 1

    except Exception as exc:
        logger.error("Introspection failed mid-run", connection=conn_name, error=str(exc))
        ctx.errors.append(f"DB introspection '{conn_name}' failed: {exc}")
    finally:
        try:
            mysql_conn.close()
        except Exception:
            pass

    return stats


# ── Phase entry point ─────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> None:
    """Connect to each configured database and introspect its live schema."""
    db_configs = ctx.config.databases
    if not db_configs:
        logger.info(
            "No databases configured — skipping live DB introspection. "
            "Run: laravelgraph db-connections add"
        )
        return

    total_tables = total_columns = total_procs = total_views = total_fks = 0

    for cfg in db_configs:
        if cfg.driver not in ("mysql", "pgsql"):
            logger.warning(
                "Unsupported driver — only mysql is supported in this version",
                connection=cfg.name,
                driver=cfg.driver,
            )
            ctx.errors.append(f"DB introspection: unsupported driver '{cfg.driver}' for '{cfg.name}'")
            continue

        if cfg.driver == "pgsql":
            logger.warning(
                "PostgreSQL support coming soon — skipping",
                connection=cfg.name,
            )
            ctx.errors.append(f"DB introspection: PostgreSQL not yet supported for '{cfg.name}'")
            continue

        stats = _introspect_one(ctx, cfg)
        total_tables += stats["tables"]
        total_columns += stats["columns"]
        total_procs += stats["procedures"]
        total_views += stats["views"]
        total_fks += stats["fks"]

        # Invalidate query + DB-context caches for this connection — the live
        # schema just changed so cached SELECT results and LLM annotations are
        # stale.  Both caches self-populate lazily on the next access.
        if stats["tables"] > 0:
            try:
                from laravelgraph.config import index_dir as _index_dir
                from laravelgraph.mcp.query_cache import QueryResultCache
                from laravelgraph.mcp.db_cache import DBContextCache
                _idx = _index_dir(ctx.project_root)
                _qc_removed = QueryResultCache(_idx).invalidate_connection(cfg.name)
                _dc_removed = DBContextCache(_idx).invalidate_connection(cfg.name)
                if _qc_removed or _dc_removed:
                    logger.info(
                        "Caches invalidated after re-introspection",
                        connection=cfg.name,
                        query_cache_removed=_qc_removed,
                        db_context_removed=_dc_removed,
                    )
            except Exception as _e:
                logger.debug("Cache invalidation skipped", connection=cfg.name, error=str(_e))

    ctx.stats["live_db_tables"] = total_tables
    ctx.stats["live_db_columns"] = total_columns
    ctx.stats["live_db_procedures"] = total_procs
    ctx.stats["live_db_views"] = total_views
    ctx.stats["live_db_fks"] = total_fks

    logger.info(
        "Live DB introspection complete",
        connections=len(db_configs),
        tables=total_tables,
        columns=total_columns,
        procedures=total_procs,
        views=total_views,
        fks=total_fks,
    )
