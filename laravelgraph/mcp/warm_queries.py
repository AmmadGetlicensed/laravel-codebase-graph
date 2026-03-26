"""Cache warming for live DB query results.

After phase 24 re-introspects the live DB, the query cache is empty.
This module pre-populates it with SELECT results for the tables most likely
to be asked about by AI agents:

  1. Top-N most-accessed tables  — tables with the most QUERIES_TABLE edges
     in the graph (code touches them a lot → agents will ask about them).
  2. Small lookup tables         — tables with row_count < ``lookup_threshold``
     (few rows → probably reference/enum data like course_delivery_types).

Both strategies are configurable.  The warm job runs silently; failures for
individual tables are logged as warnings and do not abort the process.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from laravelgraph.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_TOP_N = 20
_DEFAULT_LOOKUP_THRESHOLD = 500   # rows — tables smaller than this are lookup tables
_DEFAULT_SELECT_LIMIT = 500       # max rows to cache per table


def warm_query_cache(
    project_root: Path,
    config: Any,
    *,
    top_n: int = _DEFAULT_TOP_N,
    lookup_threshold: int = _DEFAULT_LOOKUP_THRESHOLD,
    select_limit: int = _DEFAULT_SELECT_LIMIT,
) -> dict[str, int]:
    """Pre-populate the query cache for the most valuable tables.

    Args:
        project_root:       Laravel project root (used to locate the graph DB
                            and the ``.laravelgraph/`` cache directory).
        config:             Loaded ``Config`` instance (needs ``config.databases``).
        top_n:              Number of most-accessed tables to warm per connection.
        lookup_threshold:   Tables with fewer rows than this are treated as
                            lookup/reference tables and always warmed.
        select_limit:       Max rows fetched per table (stored in cache).

    Returns:
        Dict with keys ``warmed``, ``skipped``, ``errors`` (counts).
    """
    from laravelgraph.config import index_dir as _index_dir
    from laravelgraph.core.graph import GraphDB
    from laravelgraph.mcp.query_cache import QueryResultCache, validate_sql
    from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql

    idx = _index_dir(project_root)
    db_path = idx / "graph.kuzu"

    if not db_path.exists():
        logger.warning("warm_query_cache: no graph index found, skipping", path=str(db_path))
        return {"warmed": 0, "skipped": 0, "errors": 0}

    db = GraphDB(db_path)
    qc = QueryResultCache(idx)
    db_configs = config.databases if hasattr(config, "databases") else []

    if not db_configs:
        return {"warmed": 0, "skipped": 0, "errors": 0}

    totals = {"warmed": 0, "skipped": 0, "errors": 0}

    for conn_cfg in db_configs:
        if conn_cfg.driver not in ("mysql",):
            continue

        conn_name = conn_cfg.name
        ttl = conn_cfg.query_cache_ttl if hasattr(conn_cfg, "query_cache_ttl") else 300
        if ttl == 0:
            logger.info("warm_query_cache: caching disabled for connection", connection=conn_name)
            continue

        # ── Collect candidate table names ─────────────────────────────────────

        candidates: dict[str, str] = {}   # table_name → reason

        # Strategy 1: top-N by QUERIES_TABLE access count
        try:
            rows = db.execute(
                "MATCH ()-[:QUERIES_TABLE]->(t:DatabaseTable) "
                f"WHERE t.connection = '{conn_name}' "
                "RETURN t.name AS tname, count(*) AS cnt "
                "ORDER BY cnt DESC "
                f"LIMIT {top_n}"
            )
            for r in rows:
                tname = r.get("tname")
                if tname:
                    candidates[tname] = f"top-accessed ({r.get('cnt')} edges)"
        except Exception as e:
            logger.debug("warm: top-accessed query failed", connection=conn_name, error=str(e))

        # Strategy 2: small lookup tables (row_count < threshold)
        try:
            rows = db.execute(
                "MATCH (t:DatabaseTable) "
                f"WHERE t.connection = '{conn_name}' "
                f"  AND t.row_count > 0 AND t.row_count < {lookup_threshold} "
                "RETURN t.name AS tname, t.row_count AS rc "
                "ORDER BY t.row_count ASC"
            )
            for r in rows:
                tname = r.get("tname")
                if tname and tname not in candidates:
                    candidates[tname] = f"lookup table ({r.get('rc')} rows)"
        except Exception as e:
            logger.debug("warm: lookup table query failed", connection=conn_name, error=str(e))

        if not candidates:
            logger.info("warm_query_cache: no candidates found", connection=conn_name)
            continue

        logger.info(
            "warm_query_cache: warming cache",
            connection=conn_name,
            tables=len(candidates),
        )

        # ── Connect once and run all SELECT queries ────────────────────────────

        try:
            mysql_conn = _connect_mysql(conn_cfg)
        except Exception as exc:
            logger.warning("warm_query_cache: connection failed", connection=conn_name, error=str(exc))
            totals["errors"] += len(candidates)
            continue

        try:
            for tname, reason in candidates.items():
                sql = f"SELECT * FROM `{tname}` LIMIT {select_limit}"
                err = validate_sql(sql)
                if err:
                    totals["skipped"] += 1
                    continue

                key = qc.make_key(conn_name, sql)

                # Skip if already live in cache
                if qc.get(key, ttl=ttl) is not None:
                    totals["skipped"] += 1
                    continue

                try:
                    with mysql_conn.cursor() as cur:
                        cur.execute(sql)
                        raw_rows = cur.fetchall()
                        columns = [d[0] for d in (cur.description or [])]
                    rows_data = [dict(zip(columns, row)) for row in raw_rows]
                    qc.set(key, sql, conn_name, columns, rows_data, ttl=ttl)
                    totals["warmed"] += 1
                    logger.debug("warm: cached", table=tname, reason=reason, rows=len(rows_data))
                except Exception as exc:
                    logger.warning("warm: query failed", table=tname, error=str(exc))
                    totals["errors"] += 1
        finally:
            try:
                mysql_conn.close()
            except Exception:
                pass

    logger.info(
        "warm_query_cache: complete",
        warmed=totals["warmed"],
        skipped=totals["skipped"],
        errors=totals["errors"],
    )
    return totals
