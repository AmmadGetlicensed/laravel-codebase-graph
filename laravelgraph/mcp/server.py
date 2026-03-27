"""LaravelGraph MCP server built with FastMCP.

Exposes all graph intelligence as MCP tools and resources for AI agents.
Supports both stdio and HTTP/SSE transports.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from laravelgraph.config import Config, index_dir
from laravelgraph.core.graph import GraphDB
from laravelgraph.logging import configure, get_logger, get_mcp_logger
from laravelgraph.mcp.cache import SummaryCache
from laravelgraph.mcp.db_cache import DBContextCache
from laravelgraph.mcp.query_cache import QueryResultCache, validate_sql
from laravelgraph.mcp.summarize import generate_summary

logger = get_logger(__name__)
mcp_logger = get_mcp_logger()


def _load_db(project_root: Path) -> GraphDB | None:
    db_path = index_dir(project_root) / "graph.kuzu"
    if not db_path.exists():
        return None
    return GraphDB(db_path)


def create_server(project_root: Path, config: Config | None = None) -> Any:
    """Create and configure the FastMCP server with all tools and resources."""
    try:
        from fastmcp import FastMCP
    except ImportError:
        raise RuntimeError("fastmcp is required: pip install fastmcp")

    cfg = config or Config.load(project_root)
    configure(cfg.log.level, cfg.log.dir)

    mcp = FastMCP(
        name="LaravelGraph",
        version="0.1.0",
        instructions="""LaravelGraph is a graph-powered code intelligence engine for Laravel/PHP codebases.

Use these tools to understand any Laravel codebase:

## Feature exploration (start here)
- laravelgraph_feature_context: ONE CALL for the complete picture — routes table, controller source,
  models (discovered via BFS call chain, not just name matching), events, jobs dispatched
  (including from service layer), views, config. Always start here.
- laravelgraph_explain: Best-anchor end-to-end explanation — uses semantic search to pick the
  right entry point (service class, route, or event) instead of blindly matching route names.
- laravelgraph_request_flow: Full HTTP lifecycle with BFS call-chain traversal (3 hops deep) —
  controller → service → dependencies, events dispatched at every level, queued jobs section.

## Symbol lookup
- laravelgraph_query: Hybrid search (BM25 + semantic + fuzzy) across all indexed symbols
- laravelgraph_context: 360° view of any symbol — source, summary, callers, relationships
- laravelgraph_impact: Blast radius of a change
- laravelgraph_routes: Browse the full route table
- laravelgraph_models: Eloquent model relationships with linked DB tables
- laravelgraph_events: Event/listener/job dispatch graph
- laravelgraph_dead_code: Unreachable code report
- laravelgraph_bindings: Service container binding map
- laravelgraph_config_usage: Config/env dependency map

## Database intelligence
- laravelgraph_schema: Full schema (live DB + migrations) with code access summary
- laravelgraph_db_context: Full picture of a table — columns, FK/inferred relations, code access,
  value distribution for discriminator columns (type/status/state), lazy LLM annotation
- laravelgraph_resolve_column: Deep-dive on a single column — polymorphic detection, write-path
  evidence, guard conditions, live value distribution for enum/tinyint columns
- laravelgraph_procedure_context: Stored procedure details with table access map
- laravelgraph_connection_map: All configured DB connections, table counts, cross-DB access
- laravelgraph_db_query: Live read-only SQL (SELECT/SHOW/DESCRIBE) — see actual data values,
  lookup rows, enum IDs. Results are cached (TTL-based).
- laravelgraph_db_impact: Cross-layer trace — DB write site → events dispatched → listeners → jobs

## Change analysis
- laravelgraph_detect_changes: Map git diff to affected symbols
- laravelgraph_suggest_tests: Find tests to run after a change
- laravelgraph_cypher: Raw read-only Cypher queries

## Utilities
- laravelgraph_provider_status: LLM providers configured for semantic summaries

---
IMPORTANT WORKFLOW:

1. For any feature question → laravelgraph_feature_context FIRST (single call covers routes,
   source, models from service layer, events, jobs, config).

2. For "how does X work end-to-end" → laravelgraph_explain (picks best anchor — may be a
   service class, not a route, if the service scores higher semantically).

3. For "trace this HTTP request" → laravelgraph_request_flow (walks 3 hops deep into service
   layer, collects events + jobs at every level).

4. For DB questions → laravelgraph_connection_map → laravelgraph_db_context → laravelgraph_db_query
   for actual values. For mystery columns → laravelgraph_resolve_column.

5. When feature_context or request_flow shows empty events/jobs → the index may be stale.
   Tell the user to run: laravelgraph analyze --full
""",
    )

    # Lazy semantic summary cache — stored in .laravelgraph/summaries.json
    _summary_cache = SummaryCache(index_dir(project_root))

    # Lazy DB context cache — stored in .laravelgraph/db_context.json
    _db_cache = DBContextCache(index_dir(project_root))

    # TTL-based query result cache — stored in .laravelgraph/query_cache.json
    _query_cache = QueryResultCache(index_dir(project_root))

    # Evict expired query cache entries on startup — cheap disk cleanup so
    # stale results from previous sessions don't accumulate indefinitely.
    _expired_on_startup = _query_cache.evict_expired()
    if _expired_on_startup:
        logger.info("Query cache: evicted expired entries on startup", count=_expired_on_startup)

    db_ref: list[GraphDB | None] = [None]

    def _db() -> GraphDB:
        if db_ref[0] is None:
            db_ref[0] = _load_db(project_root)
        if db_ref[0] is None:
            raise ValueError(
                f"No index found at {project_root}. Run: laravelgraph analyze {project_root}"
            )
        return db_ref[0]

    def _log_tool(name: str, params: dict, result_count: int, duration_ms: float) -> None:
        mcp_logger.info(
            "Tool called",
            tool=name,
            params=params,
            result_count=result_count,
            duration_ms=round(duration_ms, 2),
        )

    def _next_steps(*hints: str) -> str:
        return "\n\n---\n**Next steps:**\n" + "\n".join(f"- {h}" for h in hints)

    # ── Discriminator column helpers ─────────────────────────────────────────

    def _is_discriminator_column(col_name: str, col_type: str, guard_conditions: str = "") -> bool:
        """Return True if this column likely encodes an enum/type/status discriminator."""
        name = col_name.lower()
        ctype = col_type.lower()
        # Has guard conditions = definitely used as a discriminator
        if guard_conditions and guard_conditions not in ("[]", "null", "", "None"):
            return True
        # Name pattern: *_type, *_status, *_state, *_kind, *_mode, is_*, has_*
        if any(name.endswith(s) for s in ("_type", "_status", "_state", "_kind", "_mode", "_flag")):
            return True
        if name.startswith("is_") or name.startswith("has_"):
            return True
        # Type pattern: small integer types and enum are typically discriminators
        if any(t in ctype for t in ("tinyint", "smallint", "enum")):
            return True
        return False

    def _fetch_col_distribution(
        table: str,
        column: str,
        conn_cfg: Any,
    ) -> list[dict] | None:
        """Fetch value distribution for a discriminator column via QueryResultCache.

        Returns a list of {val, cnt} dicts ordered by value, or None on failure.
        Results are cached using the standard query TTL.
        """
        safe_table = table.replace("`", "")
        safe_col = column.replace("`", "")
        sql = (
            f"SELECT `{safe_col}` AS val, COUNT(*) AS cnt "
            f"FROM `{safe_table}` "
            f"GROUP BY `{safe_col}` "
            f"ORDER BY `{safe_col}` LIMIT 100"
        )
        ttl = getattr(conn_cfg, "query_cache_ttl", 300)
        key = _query_cache.make_key(conn_cfg.name, sql)

        # Check cache first
        cached = _query_cache.get(key, ttl=ttl)
        if cached is not None:
            return cached.get("rows", [])

        if ttl == 0:
            return None  # caching disabled — don't hit DB for a non-essential enrichment

        try:
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
            mc = _connect_mysql(conn_cfg)
            try:
                with mc.cursor() as cur:
                    cur.execute(sql)
                    raw = cur.fetchall()
                    cols_desc = [d[0] for d in (cur.description or [])]
                rows_data = [dict(zip(cols_desc, row)) for row in raw]
            finally:
                try:
                    mc.close()
                except Exception:
                    pass
            _query_cache.set(key, sql, conn_cfg.name, cols_desc, rows_data, ttl=ttl)
            return rows_data
        except Exception as e:
            logger.debug("_fetch_col_distribution failed", table=table, column=column, error=str(e))
            return None

    def _get_conn_cfg_for_table(conn_name: str) -> Any | None:
        """Return the DatabaseConnectionConfig for a given connection name, or None."""
        for c in (cfg.databases if hasattr(cfg, "databases") else []):
            if c.name == conn_name:
                return c
        return None

    def _fetch_varchar_sample(
        table: str, column: str, conn_cfg: Any, max_distinct: int = 30
    ) -> list[dict] | None:
        """Sample distinct values for a varchar/text column via the query cache.

        Only runs when the result fits within `max_distinct` distinct values —
        i.e. the column is categorical enough to be useful.  Returns a list of
        {val, cnt} dicts ordered by count desc, or None when unavailable.
        """
        safe_table = table.replace("`", "")
        safe_col = column.replace("`", "")
        sql = (
            f"SELECT `{safe_col}` AS val, COUNT(*) AS cnt "
            f"FROM `{safe_table}` "
            f"WHERE `{safe_col}` IS NOT NULL AND `{safe_col}` != '' "
            f"GROUP BY `{safe_col}` "
            f"ORDER BY cnt DESC "
            f"LIMIT {max_distinct + 1}"  # fetch one extra to detect overflow
        )
        ttl = getattr(conn_cfg, "query_cache_ttl", 300)
        key = _query_cache.make_key(conn_cfg.name, sql)

        cached = _query_cache.get(key, ttl=ttl)
        if cached is not None:
            rows = cached.get("rows", [])
            if len(rows) > max_distinct:
                return None  # too many distinct values — not useful
            return rows

        if ttl == 0:
            return None

        try:
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
            mc = _connect_mysql(conn_cfg)
            try:
                with mc.cursor() as cur:
                    cur.execute(sql)
                    raw = cur.fetchall()
                    cols_desc = [d[0] for d in (cur.description or [])]
                rows_data = [dict(zip(cols_desc, row)) for row in raw]
            finally:
                try:
                    mc.close()
                except Exception:
                    pass
            _query_cache.set(key, sql, conn_cfg.name, cols_desc, rows_data, ttl=ttl)
            if len(rows_data) > max_distinct:
                return None  # too many distinct values
            return rows_data
        except Exception as e:
            logger.debug("_fetch_varchar_sample failed", table=table, column=column, error=str(e))
            return None

    # ── Tool: laravelgraph_query ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_query(
        query: str = "",
        q: str = "",
        limit: int = 20,
        role_filter: str = "",
        file_filter: str = "",
    ) -> str:
        """Hybrid search (BM25 + semantic + fuzzy) across all indexed Laravel symbols.

        Args:
            query: Search query — symbol name, concept, or natural language phrase
            q: Alias for query — use either parameter name
            limit: Maximum results to return (default 20)
            role_filter: Filter by Laravel role (model|controller|middleware|job|event|listener|route|...)
            file_filter: Filter to symbols in files matching this path fragment
        """
        if not query and q:
            query = q
        start = time.perf_counter()
        try:
            from laravelgraph.search.hybrid import HybridSearch
            search = HybridSearch(_db(), cfg.search)
            search.build_index()
            results = search.search(
                query,
                limit=limit,
                file_filter=file_filter or None,
                role_filter=role_filter or None,
            )
        except Exception as e:
            logger.error("laravelgraph_query failed", error=str(e))
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_query", {"query": query, "limit": limit}, len(results), elapsed)

        if not results:
            return f"No results found for '{query}'." + _next_steps(
                "Try a broader query or different keywords",
                "Use laravelgraph_routes to browse available routes",
                "Use laravelgraph_models to browse Eloquent models",
            )

        lines = [f"## Search Results for '{query}' ({len(results)} found)\n"]
        for r in results:
            lines.append(f"### {r.label}: `{r.name}`")
            lines.append(f"- **FQN:** `{r.fqn}`")
            lines.append(f"- **File:** `{r.file_path}`")
            if r.laravel_role:
                lines.append(f"- **Role:** {r.laravel_role}")
            lines.append(f"- **Score:** {r.score:.3f}")
            if r.snippet:
                lines.append(f"- **Snippet:** {r.snippet}")
            lines.append("")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_context(symbol_id) for a 360° view of any result",
            "Use laravelgraph_impact(symbol_id) to see the blast radius of changes",
            "Use laravelgraph_request_flow(route_name) to trace a full HTTP request",
        )

    # ── Tool: laravelgraph_context ───────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_context(symbol: str, include_source: bool = False) -> str:
        """360° view of any symbol: semantic summary, callers, callees, relationships.

        Returns metadata and a semantic summary for the symbol plus all graph
        relationships. The file path and line numbers are always included so you
        can read the source directly when needed.

        Source code behaviour:
        - First time a symbol is queried (no cached summary): source is included
          automatically so you can see what the summary is based on.
        - Subsequent queries (cached summary): source is omitted by default —
          use include_source=True to force it when you need to read or edit code.
        - If the source file changes the cache is invalidated and source is
          included again on the next query.

        Args:
            symbol: Symbol identifier — FQN, node_id, class name, or method name
                    (e.g. "UserController::store")
            include_source: Set True to always include the PHP source snippet,
                            even when a cached summary is available.
        """
        start = time.perf_counter()
        db = _db()

        node = _resolve_symbol(db, symbol)
        if not node:
            return f"Symbol '{symbol}' not found. Try laravelgraph_query('{symbol}') to search."

        node_id  = node.get("node_id", "")
        label    = node.get("_label", "unknown")
        fqn      = node.get("fqn", node.get("name", symbol))
        fp       = node.get("file_path", "")
        ls       = node.get("line_start") or 0
        le       = node.get("line_end") or 0
        raw_doc  = node.get("docblock", "")

        lines = [f"## Context: `{fqn}`\n"]
        lines.append(f"- **Type:** {label}")
        lines.append(f"- **File:** {fp or 'unknown'}")
        if ls:
            lines.append(f"- **Line:** {ls}–{le or '?'}")
        if node.get("laravel_role"):
            lines.append(f"- **Laravel Role:** {node['laravel_role']}")
        if node.get("is_dead_code"):
            lines.append("- **⚠️ Dead Code:** This symbol has no incoming references")
        comm_id = node.get("community_id")
        if comm_id is not None and str(comm_id) not in ("-1", "", "None"):
            lines.append(f"- **Community:** {comm_id}")
        lines.append("")

        # ── Semantic summary (cached) ────────────────────────────────────────
        cached_summary = _summary_cache.get(node_id, file_path=fp)
        if cached_summary:
            lines.append(f"**Summary:** {cached_summary}")
            lines.append("")
        else:
            # No cached summary — show cleaned docblock as interim description
            from laravelgraph.mcp.explain import clean_docblock
            description = clean_docblock(raw_doc)
            if description:
                lines.append(f"**Purpose:** {description}")

        # ── Source code ──────────────────────────────────────────────────────
        # Include when: cache is cold (first query) OR caller explicitly requests it.
        # When cache is warm and include_source=False, omit source to save tokens.
        # The file path + line numbers above are always present for direct reading.
        should_include_source = fp and ls and (not cached_summary or include_source)
        if should_include_source:
            from laravelgraph.mcp.explain import _append_source_block
            _append_source_block(fp, ls, le, project_root, lines)

        lines.append("")

        # ── Generate and cache summary if we have source and API key ────────
        if not cached_summary and fp and ls and cfg.summary.enabled:
            from laravelgraph.mcp.explain import read_source_snippet
            source_text = read_source_snippet(fp, ls, le, project_root)
            if source_text:
                node_type = label.lower().replace("_", " ").replace("eloquentmodel", "Eloquent model")
                summary, provider_used = generate_summary(
                    fqn=fqn,
                    node_type=node_type,
                    source=source_text,
                    docblock=raw_doc,
                    summary_cfg=cfg.summary,
                )
                if summary:
                    _summary_cache.set(node_id, summary, provider_used, file_path=fp)
                    # Replace the docblock/source with the fresh summary
                    lines.append(f"**Summary (just generated):** {summary}")
                    lines.append("")

        # ── Callers ──────────────────────────────────────────────────────────
        try:
            callers = db.execute(
                "MATCH (caller)-[r:CALLS]->(target) WHERE target.node_id = $id "
                "RETURN caller.fqn AS caller_fqn, r.confidence AS conf LIMIT 20",
                {"id": node_id},
            )
            if callers:
                lines.append(f"### Callers ({len(callers)})")
                for c in callers:
                    conf = c.get("conf")
                    conf_str = f" (conf: {conf:.2f})" if conf is not None else ""
                    lines.append(f"- `{c.get('caller_fqn', '?')}`{conf_str}")
                lines.append("")
        except Exception:
            pass

        # ── Callees ──────────────────────────────────────────────────────────
        try:
            callees = db.execute(
                "MATCH (source)-[r:CALLS]->(target) WHERE source.node_id = $id "
                "RETURN target.fqn AS target_fqn, r.confidence AS conf LIMIT 20",
                {"id": node_id},
            )
            if callees:
                lines.append(f"### Calls ({len(callees)})")
                for c in callees:
                    conf = c.get("conf")
                    conf_str = f" (conf: {conf:.2f})" if conf is not None else ""
                    lines.append(f"- `{c.get('target_fqn', '?')}`{conf_str}")
                lines.append("")
        except Exception:
            pass

        # ── Dispatches (events/jobs) ──────────────────────────────────────────
        try:
            dispatches = db.execute(
                "MATCH (n)-[d:DISPATCHES]->(t) WHERE n.node_id = $id "
                "RETURN t.name AS name, t.fqn AS fqn, d.dispatch_type AS dtype, "
                "d.is_queued AS queued, d.condition AS cond LIMIT 20",
                {"id": node_id},
            )
            if dispatches:
                has_conditions = any(d.get("cond") for d in dispatches)
                multi = len(dispatches) > 1
                header = f"### Dispatches ({len(dispatches)})"
                if multi and has_conditions:
                    header += " — conditional dispatch (not all targets fire on every call)"
                lines.append(header)
                for d in dispatches:
                    dtype = d.get("dtype") or "event"
                    q = " *(queued)*" if d.get("queued") else ""
                    cond = d.get("cond") or ""
                    cond_str = f" `when: {cond}`" if cond else ""
                    lines.append(f"- **{dtype}:** `{d.get('name', '?')}`{q}{cond_str}")
                if multi and not has_conditions:
                    lines.append(
                        "_Multiple dispatch targets detected — read source to understand "
                        "branching conditions (use `include_source=True`)._"
                    )
                lines.append("")
        except Exception:
            pass

        # ── Eloquent relationships (if model) ─────────────────────────────────
        if label in ("EloquentModel", "Class_"):
            try:
                rels = db.execute(
                    "MATCH (m)-[r:HAS_RELATIONSHIP]->(related) WHERE m.node_id = $id "
                    "RETURN r.relationship_type AS rel_type, r.method_name AS method, "
                    "related.name AS related_model LIMIT 20",
                    {"id": node_id},
                )
                if rels:
                    lines.append(f"### Eloquent Relationships ({len(rels)})")
                    for r in rels:
                        lines.append(f"- `{r.get('method')}()` → {r.get('rel_type')} → `{r.get('related_model')}`")
                    lines.append("")
            except Exception:
                pass

        # ── Rendered views ────────────────────────────────────────────────────
        try:
            views = db.execute(
                "MATCH (n)-[:RENDERS_TEMPLATE]->(t:BladeTemplate) WHERE n.node_id = $id "
                "RETURN t.name AS name LIMIT 10",
                {"id": node_id},
            )
            if views:
                names = [v.get("name", "?") for v in views]
                lines.append(f"### Renders Views: {', '.join(f'`{n}`' for n in names)}")
                lines.append("")
        except Exception:
            pass

        # ── Inheritance ───────────────────────────────────────────────────────
        try:
            parent = db.execute(
                "MATCH (c)-[:EXTENDS_CLASS]->(p) WHERE c.node_id = $id RETURN p.fqn AS parent",
                {"id": node_id},
            )
            if parent:
                lines.append(f"### Extends: `{parent[0].get('parent', '?')}`")
                lines.append("")

            children = db.execute(
                "MATCH (c)-[:EXTENDS_CLASS]->(p) WHERE p.node_id = $id "
                "RETURN c.fqn AS child LIMIT 10",
                {"id": node_id},
            )
            if children:
                lines.append(f"### Extended By ({len(children)})")
                for ch in children:
                    lines.append(f"- `{ch.get('child', '?')}`")
                lines.append("")
        except Exception:
            pass

        # ── Methods (if class) ────────────────────────────────────────────────
        if label in ("Class_", "EloquentModel", "Controller"):
            try:
                methods = db.execute(
                    "MATCH (c)-[:DEFINES]->(m:Method) WHERE c.node_id = $id "
                    "RETURN m.name AS name, m.visibility AS vis, m.is_dead_code AS dead, "
                    "m.laravel_role AS role LIMIT 20",
                    {"id": node_id},
                )
                if methods:
                    lines.append(f"### Methods ({len(methods)})")
                    for m in methods:
                        dead = " *(dead)*" if m.get("dead") else ""
                        role = f" `[{m.get('role')}]`" if m.get("role") else ""
                        vis = m.get("vis") or "public"
                        lines.append(f"- `{vis} {m.get('name', '?')}(){role}`{dead}")
                    lines.append("")
            except Exception:
                pass

        # ── Git coupling ──────────────────────────────────────────────────────
        try:
            coupled = db.execute(
                "MATCH (f1)-[r:COUPLED_WITH]->(f2) WHERE f1.node_id = $id "
                "RETURN f2.relative_path AS coupled_file, r.strength AS strength, "
                "r.co_changes AS changes LIMIT 8",
                {"id": node_id},
            )
            if coupled:
                lines.append(f"### Frequently Changed Together ({len(coupled)})")
                for c in coupled:
                    lines.append(
                        f"- `{c.get('coupled_file')}` "
                        f"(strength: {c.get('strength', 0):.2f}, {c.get('changes', 0)} co-changes)"
                    )
                lines.append("")
        except Exception:
            pass

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_context", {"symbol": symbol}, 1, elapsed)

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_feature_context(feature) for a complete feature picture",
            "Use laravelgraph_impact(symbol) to see the full blast radius",
            "Use laravelgraph_suggest_tests(symbol) to find related tests",
        )

    # ── Tool: laravelgraph_impact ────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_impact(symbol: str, depth: int = 3) -> str:
        """Blast radius analysis — all symbols affected by changing this one, grouped by depth.

        Args:
            symbol: Symbol FQN, node_id, or name
            depth: BFS depth to trace (default 3, max 5)
        """
        start = time.perf_counter()
        depth = min(depth, 5)
        db = _db()

        node = _resolve_symbol(db, symbol)
        if not node:
            return f"Symbol '{symbol}' not found."

        node_id = node.get("node_id", "")
        fqn = node.get("fqn", node.get("name", symbol))

        from laravelgraph.analysis.impact import ImpactAnalyzer
        analyzer = ImpactAnalyzer(db)
        impact = analyzer.analyze(node_id, depth=depth)

        lines = [f"## Impact Analysis: `{fqn}`\n"]
        lines.append(f"Changes to this symbol may affect **{impact.total}** downstream symbols.\n")

        for d in range(1, depth + 1):
            affected = impact.by_depth.get(d, [])
            if not affected:
                continue
            label = {1: "🔴 Direct (will break)", 2: "🟡 Indirect (may break)", 3: "🟢 Transitive (review)"}.get(
                d, f"Depth {d}"
            )
            lines.append(f"### {label} — {len(affected)} symbols")
            for sym in affected[:20]:
                conf = sym.get("confidence", 1.0)
                lines.append(f"- `{sym.get('fqn', sym.get('name', '?'))}` (confidence: {conf:.2f})")
            if len(affected) > 20:
                lines.append(f"  _...and {len(affected) - 20} more_")
            lines.append("")

        # Laravel-specific impacts
        if impact.route_impacts:
            lines.append(f"### 🌐 Route Impacts ({len(impact.route_impacts)})")
            for r in impact.route_impacts:
                lines.append(f"- `{r.get('method', '?')} {r.get('uri', '?')}` ({r.get('name', '')})")
            lines.append("")

        if impact.model_impacts:
            lines.append(f"### 🗄️ Eloquent Model Impacts ({len(impact.model_impacts)})")
            for m in impact.model_impacts:
                lines.append(f"- `{m.get('fqn', '?')}` via {m.get('relationship', '?')}")
            lines.append("")

        # ── Route entry-point detection ───────────────────────────────────────
        # Webhook handlers, API endpoints, and other route handlers are called by
        # the HTTP router — not by PHP code — so they naturally have 0 callers and
        # may have 0 impact according to the static call graph.  Detect this and
        # explain it rather than leaving the agent with a silent "0 symbols" result.
        if impact.total == 0:
            try:
                route_rows = db.execute(
                    "MATCH (r:Route)-[:ROUTES_TO]->(n) WHERE n.node_id = $nid "
                    "RETURN r.method AS method, r.uri AS uri, r.name AS rname LIMIT 5",
                    {"nid": node_id},
                )
            except Exception:
                route_rows = []

            if route_rows:
                lines.append(
                    "> **Note:** This symbol is a **route entry point** — it is invoked by the "
                    "HTTP router, not called from other PHP code. The static call graph has no "
                    "callers pointing toward it, so impact analysis cannot trace upstream. "
                    "Use `laravelgraph_request_flow(route)` to trace what this handler "
                    "dispatches downstream.\n"
                )
                for r in route_rows:
                    rname = f" — `{r.get('rname')}`" if r.get("rname") else ""
                    lines.append(f"> Route: `{r.get('method', '?')} /{r.get('uri', '?')}`{rname}")
                lines.append("")

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_impact", {"symbol": symbol, "depth": depth}, impact.total, elapsed)

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_suggest_tests(symbol) to find tests covering these symbols",
            "Use laravelgraph_context(symbol) on any affected symbol for more details",
            "Run your test suite on the suggested test files before committing",
        )

    # ── Tool: laravelgraph_routes ────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_routes(
        filter: str = "",
        filter_method: str = "",
        filter_uri: str = "",
        filter_middleware: str = "",
        limit: int = 50,
    ) -> str:
        """Full route map with middleware stacks, controller bindings, and parameters.

        Args:
            filter: General search — filters by URI fragment OR controller name (shorthand for filter_uri)
            filter_method: Filter by HTTP method (GET|POST|PUT|PATCH|DELETE)
            filter_uri: Filter routes containing this URI fragment
            filter_middleware: Filter routes using this middleware
            limit: Max routes to return (default 50)
        """
        # filter is a shorthand for filter_uri
        if filter and not filter_uri:
            filter_uri = filter
        start = time.perf_counter()
        db = _db()

        try:
            query = "MATCH (r:Route) RETURN r.* LIMIT $limit"
            routes = db.execute(query, {"limit": limit * 3})  # over-fetch for filtering
        except Exception as e:
            return f"Error querying routes: {e}"

        # Apply filters
        filtered = routes
        if filter_method:
            filtered = [r for r in filtered if filter_method.upper() in (r.get("r.http_method", "") or "").upper()]
        if filter_uri:
            filtered = [r for r in filtered if filter_uri in (r.get("r.uri", "") or "")]
        if filter_middleware:
            filtered = [r for r in filtered if filter_middleware in (r.get("r.middleware_stack", "") or "")]
        filtered = filtered[:limit]

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_routes", {"filter_method": filter_method}, len(filtered), elapsed)

        if not filtered:
            return "No routes found matching the filters."

        lines = [f"## Route Map ({len(filtered)} routes)\n"]
        lines.append("| Method | URI | Controller | Middleware | Name |")
        lines.append("|--------|-----|------------|------------|------|")

        for r in filtered:
            method = r.get("r.http_method", "?")
            uri = r.get("r.uri", "?")
            controller = r.get("r.controller_fqn", "")
            action = r.get("r.action_method", "")
            if controller and action:
                binding = f"{controller.split('\\\\')[-1]}::{action}"
            elif controller:
                binding = controller.split("\\\\")[-1]
            else:
                binding = "Closure"
            middleware = r.get("r.middleware_stack", "[]")
            if middleware and middleware != "[]":
                try:
                    mw_list = json.loads(middleware)
                    middleware = ", ".join(mw_list[:3])
                    if len(mw_list) > 3:
                        middleware += f" +{len(mw_list) - 3}"
                except Exception:
                    pass
            name = r.get("r.name", "")
            lines.append(f"| `{method}` | `{uri}` | `{binding}` | {middleware} | {name} |")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_request_flow(route_name) to trace the full lifecycle of any route",
            "Use laravelgraph_context(controller_name) to inspect a controller in detail",
            "Use laravelgraph_query('authentication') to find auth-related routes",
        )

    # ── Tool: laravelgraph_models ────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_models(model_name: str = "", name: str = "", model: str = "") -> str:
        """Eloquent model relationship graph with foreign keys and pivot tables.

        Args:
            model_name: Optional — filter to a specific model (name or FQN). Omit for all models.
            name: Alias for model_name
            model: Alias for model_name
        """
        if not model_name:
            model_name = name or model
        start = time.perf_counter()
        db = _db()

        try:
            if model_name:
                query = (
                    "MATCH (m:EloquentModel) WHERE m.name = $name OR m.fqn = $name "
                    "RETURN m.* LIMIT 1"
                )
                models = db.execute(query, {"name": model_name})
            else:
                models = db.execute("MATCH (m:EloquentModel) RETURN m.* LIMIT 50")
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_models", {"model_name": model_name}, len(models), elapsed)

        if not models:
            return "No Eloquent models found. Ensure the project has been indexed."

        lines = [f"## Eloquent Model Map ({len(models)} models)\n"]

        for m in models:
            name = m.get("m.name", "?")
            fqn = m.get("m.fqn", "?")
            table = m.get("m.db_table", "?")
            lines.append(f"### `{name}` → table: `{table}`")
            lines.append(f"- **FQN:** `{fqn}`")

            if m.get("m.fillable") and m["m.fillable"] != "[]":
                try:
                    fillable = json.loads(m["m.fillable"])
                    lines.append(f"- **Fillable:** {', '.join(fillable[:8])}")
                except Exception:
                    pass

            if m.get("m.soft_deletes"):
                lines.append("- **Soft Deletes:** Yes")

            # Linked DB table (live or migration-derived)
            try:
                linked_tables = db.execute(
                    "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) WHERE m.fqn = $fqn "
                    "RETURN t.name AS tname, t.connection AS tconn, t.source AS tsrc, t.row_count AS rows",
                    {"fqn": fqn},
                )
                if linked_tables:
                    for lt in linked_tables:
                        source = lt.get("tsrc", "") or "migration"
                        conn = f" on `{lt.get('tconn')}`" if lt.get("tconn") else ""
                        rows = f" (~{lt.get('rows'):,} rows)" if lt.get("rows") else ""
                        lines.append(f"- **DB Table:** `{lt.get('tname')}`{conn}{rows} _{source}_")
            except Exception:
                pass

            # Relationships
            try:
                rels = db.execute(
                    "MATCH (model)-[r:HAS_RELATIONSHIP]->(related) WHERE model.fqn = $fqn "
                    "RETURN r.relationship_type AS type, r.method_name AS method, "
                    "related.name AS related, r.foreign_key AS fk, r.pivot_table AS pivot",
                    {"fqn": fqn},
                )
                if rels:
                    lines.append("- **Relationships:**")
                    for rel in rels:
                        pivot = f" (pivot: {rel.get('pivot')})" if rel.get("pivot") else ""
                        fk = f" FK: {rel.get('fk')}" if rel.get("fk") else ""
                        lines.append(
                            f"  - `{rel.get('method')}()` → {rel.get('type')} → `{rel.get('related')}`{fk}{pivot}"
                        )
            except Exception:
                pass

            lines.append("")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_db_context(table) for full DB table analysis (columns, access patterns, semantics)",
            "Use laravelgraph_context(ModelName) to see which controllers/jobs use this model",
            "Use laravelgraph_impact(ModelName) to see the blast radius of changing this model",
        )

    # ── Tool: laravelgraph_request_flow ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_request_flow(route: str) -> str:
        """Trace a complete HTTP request lifecycle for a given route.

        Shows: middleware → controller → form request → service → model → event → listener

        Args:
            route: Route name (e.g. "users.store"), URI (e.g. "/api/users"), or pattern
        """
        start = time.perf_counter()
        db = _db()

        # Find the route — strip optional "METHOD /uri" prefix (e.g. "POST /v1/booking" → "/v1/booking")
        _http_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        route_parts = route.strip().split(None, 1)
        if len(route_parts) == 2 and route_parts[0].upper() in _http_methods:
            route_uri = route_parts[1]
            route_method = route_parts[0].upper()
        else:
            route_uri = route
            route_method = None

        try:
            if route_method:
                routes = db.execute(
                    "MATCH (r:Route) WHERE (r.name = $q OR r.uri = $q OR r.uri CONTAINS $q) "
                    "AND r.http_method = $method RETURN r.* LIMIT 1",
                    {"q": route_uri, "method": route_method},
                )
                # Fall back without method filter if nothing found
                if not routes:
                    routes = db.execute(
                        "MATCH (r:Route) WHERE r.name = $q OR r.uri = $q OR r.uri CONTAINS $q "
                        "RETURN r.* LIMIT 1",
                        {"q": route_uri},
                    )
            else:
                routes = db.execute(
                    "MATCH (r:Route) WHERE r.name = $q OR r.uri = $q OR r.uri CONTAINS $q "
                    "RETURN r.* LIMIT 1",
                    {"q": route_uri},
                )
        except Exception as e:
            return f"Error: {e}"

        if not routes:
            return f"Route '{route}' not found. Use laravelgraph_routes() to browse available routes."

        r = routes[0]
        controller_fqn = r.get("r.controller_fqn", "")
        action = r.get("r.action_method", "handle")
        uri = r.get("r.uri", route)
        method = r.get("r.http_method", "GET")
        middleware_stack = r.get("r.middleware_stack", "[]")

        lines = [f"## Request Flow: `{method} {uri}`\n"]

        # 1. Middleware stack
        try:
            mw_list = json.loads(middleware_stack) if middleware_stack else []
        except Exception:
            mw_list = []

        if mw_list:
            lines.append("### 1. Middleware Pipeline")
            for i, mw in enumerate(mw_list, 1):
                lines.append(f"   {i}. `{mw}`")
            lines.append("")

        # 2. Controller (or Closure)
        is_closure = not controller_fqn or controller_fqn in ("Closure", "\\Closure")
        if is_closure:
            lines.append("### 2. Handler: `Closure`")
            lines.append("")
            lines.append("> ⚠️ This route uses an inline Closure defined directly in the routes file.")
            lines.append("> No controller class to trace — the logic lives in the route definition itself.")
            lines.append("> To inspect it, open the routes file directly:")
            lines.append("")
            # Try to find the route file from the graph
            try:
                route_files = db.execute(
                    "MATCH (r:Route) WHERE r.uri = $uri RETURN r.file_path AS fp LIMIT 1",
                    {"uri": uri},
                )
                if route_files and route_files[0].get("fp"):
                    lines.append(f"   - **Routes file:** `{route_files[0]['fp']}`")
            except Exception:
                pass
        else:
            lines.append(f"### 2. Controller: `{controller_fqn}::{action}`")

            # Find form request usage
            try:
                ctrl_method_id = f"method:{controller_fqn}::{action}"
                form_reqs = db.execute(
                    "MATCH (m:Method)-[:VALIDATES_WITH]->(fr:FormRequest) WHERE m.node_id = $id "
                    "RETURN fr.name AS name, fr.fqn AS fqn",
                    {"id": ctrl_method_id},
                )
                if form_reqs:
                    lines.append("")
                    lines.append("### 3. Form Request Validation")
                    for fr in form_reqs:
                        lines.append(f"   - `{fr.get('name')}` ({fr.get('fqn')})")
            except Exception:
                pass

            # ── Deep call-chain traversal (BFS, max 3 hops) ─────────────────
            # Collects called methods/classes AND dispatched events/jobs at
            # every level — not just from the controller method directly.
            try:
                frontier: list[str] = [f"method:{controller_fqn}::{action}"]
                visited: set[str] = set(frontier)
                call_tree: list[tuple[int, str, str]] = []   # (depth, fqn, label)
                dispatched_events: list[dict] = []
                dispatched_jobs: list[dict] = []
                MAX_HOPS = 3
                MAX_CALLS_PER_LEVEL = 10

                for hop in range(MAX_HOPS):
                    if not frontier:
                        break
                    next_frontier: list[str] = []
                    for node_id in frontier:
                        # CALLS edges — follow the call chain
                        for _calls_label in ("Method", "Class_", "Function_", "Trait_"):
                            try:
                                called = db.execute(
                                    f"MATCH (m:Method)-[r:CALLS]->(target:{_calls_label}) WHERE m.node_id = $id "
                                    "RETURN target.node_id AS tid, target.fqn AS fqn, "
                                    f"r.confidence AS conf, '{_calls_label}' AS label "
                                    f"LIMIT {MAX_CALLS_PER_LEVEL}",
                                    {"id": node_id},
                                )
                                for c in called:
                                    tid = c.get("tid", "")
                                    fqn = c.get("fqn", "")
                                    if fqn and fqn not in visited:
                                        visited.add(fqn)
                                        call_tree.append((hop + 1, fqn, c.get("label", "?")))
                                        if tid:
                                            next_frontier.append(tid)
                            except Exception:
                                pass

                        # DISPATCHES — events and jobs from this method
                        for _disp_label in ("Event", "Job"):
                            try:
                                dispatches = db.execute(
                                    f"MATCH (m:Method)-[d:DISPATCHES]->(target:{_disp_label}) WHERE m.node_id = $id "
                                    f"RETURN target.name AS tname, target.fqn AS tfqn, "
                                    f"d.dispatch_type AS dtype, '{_disp_label}' AS tlabel LIMIT 10",
                                    {"id": node_id},
                                )
                                for d in dispatches:
                                    dtype = (d.get("dtype") or d.get("tlabel", "")).lower()
                                    entry = {
                                        "name": d.get("tname", "?"),
                                        "fqn": d.get("tfqn", ""),
                                        "depth": hop + 1,
                                        "from": node_id,
                                    }
                                    if "job" in dtype:
                                        dispatched_jobs.append(entry)
                                    else:
                                        dispatched_events.append(entry)
                            except Exception:
                                pass

                    frontier = next_frontier

                if call_tree:
                    lines.append("")
                    lines.append("### 4. Call Chain (controller → services → dependencies)\n")
                    for depth, fqn, label in call_tree[:30]:
                        indent = "   " * depth
                        lines.append(f"{indent}- `{fqn}` _{label}_")
                elif not dispatched_events and not dispatched_jobs:
                    # BFS found nothing — likely because the controller FQN in the
                    # route index is from a sub-namespace (e.g. Reseller\BookingController)
                    # while CALLS edges were indexed against a different FQN.
                    # Provide a fallback: search for the method by name across all classes.
                    lines.append("")
                    lines.append(
                        f"> ⚠ No call chain found for `{controller_fqn}::{action}` — "
                        "the route may store a sub-namespace FQN that doesn't match the indexed controller. "
                        f"Try: `laravelgraph_context(\"{controller_fqn.split(chr(92))[-1]}\")`"
                    )
                    try:
                        alt_methods = db.execute(
                            "MATCH (m:Method) WHERE m.name = $mname "
                            "RETURN m.node_id AS nid, m.fqn AS fqn, m.file_path AS fp LIMIT 5",
                            {"mname": action},
                        )
                        if alt_methods:
                            lines.append("\n**Possible matches by method name:**")
                            for am in alt_methods:
                                lines.append(f"- `{am.get('fqn')}` — {Path(am.get('fp', '?')).name}")
                    except Exception:
                        pass

                if dispatched_events:
                    lines.append("")
                    lines.append("### 5. Events Dispatched (across full call chain)\n")
                    seen_ev: set[str] = set()
                    for ev in dispatched_events:
                        fqn = ev.get("fqn", ev.get("name", "?"))
                        if fqn in seen_ev:
                            continue
                        seen_ev.add(fqn)
                        depth_note = f" (hop {ev['depth']})" if ev["depth"] > 1 else ""
                        lines.append(f"   - `{ev.get('name')}`{depth_note} (`{fqn}`)")
                        # Listeners
                        try:
                            listeners = db.execute(
                                "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.fqn = $fqn "
                                "RETURN l.name AS lname, l.fqn AS lfqn LIMIT 5",
                                {"fqn": fqn},
                            )
                            for li in listeners:
                                lines.append(f"     → `{li.get('lname')}` (listener)")
                        except Exception:
                            pass

                if dispatched_jobs:
                    lines.append("")
                    lines.append("### 6. Queued Jobs Dispatched (across full call chain)\n")
                    seen_job: set[str] = set()
                    for job in dispatched_jobs:
                        fqn = job.get("fqn", job.get("name", "?"))
                        if fqn in seen_job:
                            continue
                        seen_job.add(fqn)
                        depth_note = f" (hop {job['depth']})" if job["depth"] > 1 else ""
                        lines.append(f"   - `{job.get('name')}`{depth_note} (`{fqn}`)")

            except Exception:
                pass

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_request_flow", {"route": route}, 1, elapsed)

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_impact(ControllerName) to see what a controller change affects",
            "Use laravelgraph_context(MiddlewareName) to inspect any middleware",
            "Use laravelgraph_suggest_tests(route_name) to find tests for this route",
        )

    # ── Tool: laravelgraph_dead_code ─────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_dead_code(role_filter: str = "", file_filter: str = "") -> str:
        """All unreachable symbols grouped by type and file.

        Args:
            role_filter: Filter to a specific Laravel role (model|controller|service|...)
            file_filter: Filter to symbols in files matching this path fragment
        """
        start = time.perf_counter()
        db = _db()

        try:
            dead_methods = db.execute(
                "MATCH (m:Method {is_dead_code: true}) RETURN m.fqn AS fqn, m.file_path AS file, "
                "m.line_start AS line, m.laravel_role AS role LIMIT 200"
            )
            dead_functions = db.execute(
                "MATCH (f:Function_ {is_dead_code: true}) RETURN f.fqn AS fqn, f.file_path AS file, "
                "f.line_start AS line ORDER BY f.fqn LIMIT 100"
            )
        except Exception as e:
            return f"Error: {e}"

        all_dead = [
            {**d, "_type": "Method"} for d in dead_methods
        ] + [
            {**d, "_type": "Function"} for d in dead_functions
        ]

        if file_filter:
            all_dead = [d for d in all_dead if file_filter in (d.get("file", "") or "")]
        if role_filter:
            all_dead = [d for d in all_dead if role_filter in (d.get("role", "") or "")]

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_dead_code", {}, len(all_dead), elapsed)

        if not all_dead:
            return "✅ No dead code detected. (Or the index needs refreshing — run `laravelgraph analyze`.)"

        lines = [f"## Dead Code Report ({len(all_dead)} unreachable symbols)\n"]
        lines.append("> **Note:** Laravel-aware exemptions applied: route handlers, event listeners,")
        lines.append("> Artisan commands, magic methods, Eloquent accessors/scopes, and policy methods")
        lines.append("> are never flagged as dead code.\n")

        # Group by file
        by_file: dict[str, list] = {}
        for d in all_dead:
            f = d.get("file", "unknown")
            by_file.setdefault(f, []).append(d)

        for file_path, symbols in sorted(by_file.items())[:30]:
            lines.append(f"### `{file_path}`")
            for s in symbols:
                lines.append(f"- Line {s.get('line', '?')}: `{s.get('fqn', '?')}` ({s.get('_type', '?')})")
            lines.append("")

        return "\n".join(lines) + _next_steps(
            "Review each flagged symbol — dead code may still be useful if called via reflection",
            "Use laravelgraph_context(symbol) to double-check before deleting",
            "Run laravelgraph_detect_changes after cleanup to see the impact",
        )

    # ── Tool: laravelgraph_detect_changes ────────────────────────────────────

    @mcp.tool()
    def laravelgraph_detect_changes(diff: str = "", base: str = "HEAD~1", head: str = "HEAD") -> str:
        """Map a git diff to affected symbols, flows, and suggested tests.

        Args:
            diff: Raw git diff output (optional — if not provided, base..head is used)
            base: Base ref for comparison (default HEAD~1)
            head: Head ref for comparison (default HEAD)
        """
        start = time.perf_counter()
        db = _db()

        # Get changed files
        changed_files: list[str] = []
        if diff:
            import re
            changed_files = re.findall(r"^--- a/(.+)$", diff, re.MULTILINE)
        else:
            try:
                from git import Repo
                repo = Repo(str(project_root))
                diff_obj = repo.commit(base).diff(repo.commit(head))
                changed_files = [d.a_path for d in diff_obj if d.a_path]
            except Exception as e:
                return f"Could not load git diff: {e}. Pass diff= parameter directly."

        if not changed_files:
            return "No changed files detected in the diff."

        lines = [f"## Change Impact Analysis ({len(changed_files)} changed files)\n"]

        all_affected_symbols: list[dict] = []
        for file_path in changed_files[:20]:
            lines.append(f"### `{file_path}`")

            # Find symbols defined in this file
            try:
                symbols = db.execute(
                    "MATCH (n:Method) WHERE n.file_path CONTAINS $fp RETURN n.fqn AS fqn, n.node_id AS id LIMIT 20 "
                    "UNION MATCH (n:Class_) WHERE n.file_path CONTAINS $fp RETURN n.fqn AS fqn, n.node_id AS id LIMIT 5",
                    {"fp": file_path.split("/")[-1]},  # match by filename
                )
                if symbols:
                    lines.append(f"Changed symbols: {len(symbols)}")
                    for s in symbols[:5]:
                        lines.append(f"- `{s.get('fqn', '?')}`")
                        all_affected_symbols.append(s)
                    if len(symbols) > 5:
                        lines.append(f"  ...and {len(symbols) - 5} more")
            except Exception:
                pass
            lines.append("")

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_detect_changes", {"base": base, "head": head}, len(all_affected_symbols), elapsed)

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_impact(symbol) on any changed symbol for full blast radius",
            "Use laravelgraph_suggest_tests(symbol) to find which tests to run",
        )

    # ── Tool: laravelgraph_schema ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_schema(table_name: str = "", connection: str = "") -> str:
        """Database schema — live DB introspection preferred, migration fallback.

        Args:
            table_name: Optional — filter to a specific table name
            connection: Optional — filter to a specific DB connection name
        """
        db = _db()
        start = time.perf_counter()

        try:
            if table_name and connection:
                tables = db.execute(
                    "MATCH (t:DatabaseTable) WHERE t.name = $name AND t.connection = $conn RETURN t.* LIMIT 5",
                    {"name": table_name, "conn": connection},
                )
                # Fallback: match just by name if connection-specific misses
                if not tables:
                    tables = db.execute(
                        "MATCH (t:DatabaseTable) WHERE t.name = $name RETURN t.* LIMIT 5",
                        {"name": table_name},
                    )
            elif table_name:
                tables = db.execute(
                    "MATCH (t:DatabaseTable) WHERE t.name = $name RETURN t.* LIMIT 5",
                    {"name": table_name},
                )
            elif connection:
                tables = db.execute(
                    "MATCH (t:DatabaseTable) WHERE t.connection = $conn RETURN t.* LIMIT 60",
                    {"conn": connection},
                )
            else:
                tables = db.execute("MATCH (t:DatabaseTable) RETURN t.* LIMIT 60")
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_schema", {"table": table_name, "connection": connection}, len(tables), elapsed)

        if not tables:
            hint = f" (connection: {connection})" if connection else ""
            return (
                f"No database tables found{hint}. "
                "Ensure migrations are present and the project has been indexed. "
                "For live DB data, configure a connection with: laravelgraph db-connections add"
            )

        # Group tables by connection for display
        by_conn: dict[str, list[dict]] = {}
        for t in tables:
            conn_name = t.get("t.connection", "") or "migration"
            by_conn.setdefault(conn_name, []).append(t)

        lines = [f"## Database Schema ({len(tables)} tables)\n"]

        for conn_name, conn_tables in sorted(by_conn.items()):
            source_label = "live DB" if conn_name != "migration" else "migrations"
            lines.append(f"### Connection: `{conn_name}` ({source_label}, {len(conn_tables)} tables)\n")

            for t in conn_tables:
                t_name = t.get("t.name", "?")
                row_count = t.get("t.row_count")
                comment = t.get("t.table_comment", "") or ""
                header = f"#### `{t_name}`"
                if row_count is not None:
                    header += f"  _(~{row_count:,} rows)_"
                if comment:
                    header += f"  — {comment}"
                lines.append(header)

                # Check if any model uses this table
                try:
                    nid = t.get("t.node_id", "")
                    model_rows = db.execute(
                        "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) "
                        "WHERE t.node_id = $nid RETURN m.name AS mname LIMIT 3",
                        {"nid": nid},
                    ) if nid else []
                    if model_rows:
                        model_list = ", ".join(f"`{r.get('mname')}`" for r in model_rows)
                        lines.append(f"- **Model(s):** {model_list}")
                except Exception:
                    pass

                try:
                    cols = db.execute(
                        "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE t.node_id = $nid "
                        "RETURN c.name AS col, c.type AS type, c.full_type AS full_type, "
                        "c.nullable AS nullable, c.unique AS uniq, c.default_value AS default_val, "
                        "c.column_key AS col_key, c.polymorphic_candidate AS poly",
                        {"nid": t.get("t.node_id", "")},
                    ) if t.get("t.node_id") else db.execute(
                        "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE t.name = $name "
                        "RETURN c.name AS col, c.type AS type, c.full_type AS full_type, "
                        "c.nullable AS nullable, c.unique AS uniq, c.default_value AS default_val, "
                        "c.column_key AS col_key, c.polymorphic_candidate AS poly",
                        {"name": t_name},
                    )
                    if cols:
                        lines.append("| Column | Type | Nullable | Key | Poly? |")
                        lines.append("|--------|------|----------|-----|-------|")
                        for col in cols:
                            nullable = "Yes" if col.get("nullable") else "No"
                            full_t = col.get("full_type") or col.get("type", "?")
                            key = col.get("col_key", "") or ""
                            poly = "✓" if col.get("poly") else ""
                            lines.append(
                                f"| `{col.get('col')}` | {full_t} | {nullable} | {key} | {poly} |"
                            )
                except Exception:
                    pass

                # Show QUERIES_TABLE summary (which code accesses this table)
                try:
                    access_rows = db.execute(
                        "MATCH (src)-[q:QUERIES_TABLE]->(t:DatabaseTable) WHERE t.node_id = $nid "
                        "RETURN src.name AS src_name, q.operation AS op, q.via AS via LIMIT 6",
                        {"nid": t.get("t.node_id", "")},
                    ) if t.get("t.node_id") else []
                    if access_rows:
                        lines.append("\n**Code access:**")
                        for ar in access_rows:
                            lines.append(f"- `{ar.get('src_name')}` — {ar.get('op', '?')} via {ar.get('via', '?')}")
                except Exception:
                    pass

                lines.append("")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_db_context(table) for full semantic analysis of any table",
            "Use laravelgraph_models to see which Eloquent models correspond to these tables",
            "Use laravelgraph_connection_map to see all DB connections and cross-DB access",
        )

    # ── Tool: laravelgraph_db_context ────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_db_context(table: str, connection: str = "") -> str:
        """Full semantic picture of a database table.

        Returns column details, FK and inferred relationships, which code
        accesses this table, and a lazy LLM-generated semantic annotation
        (generated once on first call, cached thereafter).

        Args:
            table: Table name (e.g. "orders")
            connection: DB connection name (optional — omit if only one connection)
        """
        db = _db()
        start = time.perf_counter()

        # Find the table node — prefer live DB over migration
        try:
            if connection:
                rows = db.execute(
                    "MATCH (t:DatabaseTable) WHERE t.name = $name AND t.connection = $conn RETURN t.* LIMIT 1",
                    {"name": table, "conn": connection},
                )
            else:
                # Prefer live DB node (has connection set), fall back to migration node
                rows = db.execute(
                    "MATCH (t:DatabaseTable) WHERE t.name = $name RETURN t.* LIMIT 5",
                    {"name": table},
                )
                # Sort: live DB nodes first (have non-empty connection)
                rows = sorted(rows, key=lambda r: (0 if r.get("t.connection") else 1))
        except Exception as e:
            return f"Error querying table: {e}"

        if not rows:
            return (
                f"Table `{table}` not found. "
                "Run `laravelgraph analyze` to index the project. "
                "For live DB tables, add a connection with `laravelgraph db-connections add`."
            )

        t = rows[0]
        t_node_id = t.get("t.node_id", "")
        t_name = t.get("t.name", table)
        conn_name = t.get("t.connection", "") or "migration"
        source = t.get("t.source", "migration")
        row_count = t.get("t.row_count")
        comment = t.get("t.table_comment", "") or ""

        lines = [f"## Table: `{t_name}` (connection: `{conn_name}`, source: {source})\n"]
        if row_count is not None:
            lines.append(f"- **Approximate row count:** {row_count:,}")
        if comment:
            lines.append(f"- **Comment:** {comment}")

        # ── Columns ───────────────────────────────────────────────────────────
        cols: list[dict] = []
        try:
            cols = db.execute(
                "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE t.node_id = $nid "
                "RETURN c.node_id AS col_id, c.name AS col, c.type AS type, c.full_type AS full_type, "
                "c.nullable AS nullable, c.column_key AS col_key, c.default_value AS def_val, "
                "c.column_comment AS col_comment, c.polymorphic_candidate AS poly, "
                "c.sibling_type_column AS sibling_type, c.write_path_evidence AS wpe, "
                "c.guard_conditions AS guard",
                {"nid": t_node_id},
            )
        except Exception:
            pass

        if cols:
            lines.append("\n### Columns\n")
            lines.append("| Column | Type | Nullable | Key | Notes |")
            lines.append("|--------|------|----------|-----|-------|")
            for col in cols:
                full_t = col.get("full_type") or col.get("type", "?")
                nullable = "Yes" if col.get("nullable") else "No"
                key = col.get("col_key", "") or ""
                notes_parts = []
                if col.get("col_comment"):
                    notes_parts.append(col["col_comment"])
                if col.get("poly"):
                    sibling = col.get("sibling_type", "")
                    notes_parts.append(f"polymorphic (type col: `{sibling}`)" if sibling else "polymorphic candidate")
                if col.get("def_val"):
                    notes_parts.append(f"default={col['def_val']}")
                notes = "; ".join(notes_parts)
                lines.append(f"| `{col.get('col')}` | {full_t} | {nullable} | {key} | {notes} |")

        # ── Value semantics for discriminator columns ──────────────────────────
        # For columns like type, status, state — fetch actual value distribution
        # so agents can see real enum values instead of guessing from names.
        conn_cfg_obj = _get_conn_cfg_for_table(conn_name) if conn_name != "migration" else None
        if conn_cfg_obj and cols:
            disc_cols = [
                c for c in cols
                if _is_discriminator_column(
                    c.get("col", ""),
                    c.get("full_type") or c.get("type", ""),
                    c.get("guard", "") or "",
                )
            ]
            if disc_cols:
                value_sections: list[str] = []
                for dc in disc_cols[:5]:  # cap at 5 discriminator cols per table
                    dist = _fetch_col_distribution(t_name, dc.get("col", ""), conn_cfg_obj)
                    if dist:
                        col_name_d = dc.get("col", "")
                        guard_raw_d = dc.get("guard", "") or ""
                        guard_vals: list[str] = []
                        try:
                            parsed = json.loads(guard_raw_d) if guard_raw_d else []
                            guard_vals = [str(g) for g in (parsed if isinstance(parsed, list) else [parsed])]
                        except Exception:
                            pass
                        rows_str = ", ".join(
                            f"`{r.get('val')}` ({r.get('cnt'):,} rows)" for r in dist[:20]
                        )
                        sec = f"**`{col_name_d}`**: {rows_str}"
                        if guard_vals:
                            sec += f"\n  - Code checks: {', '.join(guard_vals[:8])}"
                        value_sections.append(sec)
                if value_sections:
                    lines.append("\n### Value Semantics (discriminator columns — live DB distribution)\n")
                    for vs in value_sections:
                        lines.append(vs)
                        lines.append("")

        # ── FK / inferred relationships ───────────────────────────────────────
        try:
            fk_rows = db.execute(
                "MATCH (src:DatabaseTable)-[r:REFERENCES_TABLE]->(tgt:DatabaseTable) "
                "WHERE src.node_id = $nid "
                "RETURN r.source_column AS src_col, tgt.name AS tgt_table, r.target_column AS tgt_col, "
                "r.enforced AS enforced, r.constraint_name AS cname",
                {"nid": t_node_id},
            )
            if fk_rows:
                lines.append("\n### Foreign Keys (enforced)\n")
                for fk in fk_rows:
                    enforced = "" if fk.get("enforced") else " ⚠ not enforced"
                    lines.append(
                        f"- `{fk.get('src_col')}` → `{fk.get('tgt_table')}.{fk.get('tgt_col')}`{enforced}"
                        + (f" (constraint: {fk.get('cname')})" if fk.get("cname") else "")
                    )
        except Exception:
            pass

        # ── Inferred relationships (from write-path analysis) ─────────────────
        try:
            inferred_rows = db.execute(
                "MATCH (c:DatabaseColumn)-[r:INFERRED_REFERENCES]->(tgt:DatabaseTable) "
                "WHERE c.node_id STARTS WITH $prefix "
                "RETURN c.name AS col_name, tgt.name AS tgt_table, r.confidence AS conf, "
                "r.condition AS cond, r.evidence_type AS ev_type",
                {"prefix": f"col:{conn_name}:{t_name}."},
            ) if t_node_id else []
            if inferred_rows:
                lines.append("\n### Inferred References (static write-path analysis)\n")
                for ir in sorted(inferred_rows, key=lambda r: -(r.get("conf") or 0)):
                    conf = ir.get("conf", 0) or 0
                    cond = f" when `{ir.get('cond')}`" if ir.get("cond") else ""
                    lines.append(
                        f"- `{ir.get('col_name')}` → `{ir.get('tgt_table')}`  "
                        f"confidence={conf:.0%}{cond}  _{ir.get('ev_type', '')}_"
                    )
        except Exception:
            pass

        # ── Code access (QUERIES_TABLE) ───────────────────────────────────────
        try:
            access_rows = db.execute(
                "MATCH (src)-[q:QUERIES_TABLE]->(t:DatabaseTable) WHERE t.node_id = $nid "
                "RETURN src.name AS src_name, src.fqn AS src_fqn, q.operation AS op, "
                "q.via AS via, q.line AS line LIMIT 20",
                {"nid": t_node_id},
            )
            if access_rows:
                lines.append("\n### Code Access Patterns\n")
                by_op: dict[str, list] = {}
                for ar in access_rows:
                    op = ar.get("op", "read") or "read"
                    by_op.setdefault(op, []).append(ar)
                for op, op_rows in sorted(by_op.items()):
                    lines.append(f"**{op.upper()}:**")
                    for ar in op_rows[:6]:
                        via = ar.get("via", "") or ""
                        line = f" (line {ar.get('line')})" if ar.get("line") else ""
                        lines.append(f"- `{ar.get('src_fqn') or ar.get('src_name')}`{line} via {via}")
        except Exception:
            pass

        # ── Linked models ─────────────────────────────────────────────────────
        try:
            model_rows = db.execute(
                "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) WHERE t.node_id = $nid "
                "RETURN m.name AS mname, m.fqn AS mfqn",
                {"nid": t_node_id},
            )
            if model_rows:
                lines.append("\n### Eloquent Models\n")
                for mr in model_rows:
                    lines.append(f"- `{mr.get('mname')}` (`{mr.get('mfqn')}`)")
        except Exception:
            pass

        # ── Lazy LLM semantic annotation ──────────────────────────────────────
        col_hash = DBContextCache.schema_hash(
            [{"name": c.get("col", ""), "type": c.get("full_type") or c.get("type", "")} for c in cols]
        )
        cache_key = f"dbctx:table:{conn_name}:{t_name}"
        annotation = _db_cache.get(cache_key, current_hash=col_hash)

        if not annotation and cfg.summary.enabled:
            # Build a rich prompt — include columns, models, and top callers so the
            # LLM doesn't have to guess from the table name alone (e.g. "locations"
            # could be course events, not geography).
            col_summary = ", ".join(
                f"{c.get('col')} ({c.get('full_type') or c.get('type', '?')})"
                for c in cols[:30]
            )

            # Pull linked model names and top callers to ground the annotation
            model_names_prompt = ""
            try:
                _mrows = db.execute(
                    "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) WHERE t.node_id = $nid "
                    "RETURN m.name AS mname",
                    {"nid": t_node_id},
                )
                if _mrows:
                    model_names_prompt = "Linked Eloquent models: " + ", ".join(
                        r.get("mname", "") for r in _mrows
                    ) + "\n"
            except Exception:
                pass

            callers_prompt = ""
            try:
                _arows = db.execute(
                    "MATCH (src)-[q:QUERIES_TABLE]->(t:DatabaseTable) WHERE t.node_id = $nid "
                    "RETURN src.name AS sname, q.operation AS op LIMIT 8",
                    {"nid": t_node_id},
                )
                if _arows:
                    callers_prompt = "Code that accesses this table: " + ", ".join(
                        f"{r.get('sname')} ({r.get('op', '?')})" for r in _arows
                    ) + "\n"
            except Exception:
                pass

            prompt_source = (
                f"Database table `{t_name}` (connection: {conn_name})\n"
                + (f"MySQL comment: {comment}\n" if comment else "")
                + (f"Columns: {col_summary}\n" if col_summary else "WARNING: column data not yet available.\n")
                + model_names_prompt
                + callers_prompt
                + "\nIMPORTANT: Do not infer purpose from the table name alone — "
                "names can be misleading (e.g. a table named 'locations' may actually store "
                "course event scheduling data, not geographic locations). "
                "Use column names, linked model names, and calling code to determine "
                "the real business purpose. Be specific. If genuinely uncertain, say so."
            )
            annotation, used_provider = generate_summary(
                fqn=f"db.table.{t_name}",
                node_type="database table",
                source=prompt_source,
                summary_cfg=cfg.summary,
            )
            if annotation:
                _db_cache.set(cache_key, annotation, used_provider, schema_hash=col_hash)

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_db_context", {"table": table, "connection": connection}, len(cols), elapsed)

        if annotation:
            lines.append(f"\n### Semantic Annotation\n\n> {annotation}")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_resolve_column(table, column) for deep analysis of any mystery column",
            "Use laravelgraph_models to see how Eloquent models map to this table",
            "Use laravelgraph_schema to browse the full schema",
        )

    # ── Tool: laravelgraph_resolve_column ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_resolve_column(table: str, column: str, connection: str = "") -> str:
        """Deep-dive into a single database column.

        Especially useful for polymorphic/unconstrained columns like
        `reference_id`, `entity_id`, `owner_id` that lack FK constraints.

        Returns write-path evidence, polymorphic hints, guard conditions,
        inferred target tables with confidence scores, and a lazy LLM
        resolution (generated once on first call, cached thereafter).

        Args:
            table: Table name (e.g. "activity_logs")
            column: Column name (e.g. "reference_id")
            connection: DB connection name (optional)
        """
        db = _db()
        start = time.perf_counter()

        # Find the column node
        col_id_prefix = f"col:{connection or ''}:{table}.{column}" if connection else None
        col_data: dict | None = None

        try:
            if connection:
                col_rows = db.execute(
                    "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) "
                    "WHERE t.name = $tname AND t.connection = $conn AND c.name = $cname "
                    "RETURN c.*",
                    {"tname": table, "conn": connection, "cname": column},
                )
            else:
                col_rows = db.execute(
                    "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) "
                    "WHERE t.name = $tname AND c.name = $cname "
                    "RETURN c.*",
                    {"tname": table, "cname": column},
                )
            if col_rows:
                raw = col_rows[0]
                col_data = {(k[2:] if k.startswith("c.") else k): v for k, v in raw.items()}
        except Exception as e:
            return f"Error querying column: {e}"

        if not col_data:
            return (
                f"Column `{table}.{column}` not found in the graph. "
                "Run `laravelgraph analyze` and ensure the table is indexed."
            )

        col_node_id = col_data.get("node_id", "")
        conn_name = connection or col_node_id.split(":")[1] if col_node_id.count(":") >= 2 else "?"
        full_t = col_data.get("full_type") or col_data.get("type", "?")

        lines = [f"## Column: `{table}.{column}` (`{full_t}`)\n"]
        lines.append(f"- **Connection:** `{conn_name}`")
        lines.append(f"- **Nullable:** {'Yes' if col_data.get('nullable') else 'No'}")
        if col_data.get("column_comment"):
            lines.append(f"- **Comment:** {col_data['column_comment']}")
        if col_data.get("column_key"):
            lines.append(f"- **Key:** {col_data['column_key']}")

        # ── Polymorphic hints ─────────────────────────────────────────────────
        if col_data.get("polymorphic_candidate"):
            sibling = col_data.get("sibling_type_column", "")
            lines.append(f"\n### Polymorphic Detection\n")
            lines.append(
                f"This column is a **polymorphic ID** candidate. "
                f"The sibling type discriminator column is `{sibling or '(unknown)'}`. "
                "Use laravelgraph_resolve_column on the type column to see all guarded values."
            )

        # ── Guard conditions ──────────────────────────────────────────────────
        guard_raw = col_data.get("guard_conditions", "")
        if guard_raw:
            try:
                guards = json.loads(guard_raw) if isinstance(guard_raw, str) else guard_raw
                if guards:
                    lines.append("\n### Guard Conditions (detected in code)\n")
                    for g in (guards if isinstance(guards, list) else [guards])[:10]:
                        lines.append(f"- `{g}`")
            except Exception:
                if guard_raw:
                    lines.append(f"\n**Guard:** `{guard_raw}`")

        # ── Write-path evidence ───────────────────────────────────────────────
        wpe_raw = col_data.get("write_path_evidence", "")
        if wpe_raw:
            try:
                evidence = json.loads(wpe_raw) if isinstance(wpe_raw, str) else wpe_raw
                if evidence:
                    lines.append("\n### Write-Path Evidence\n")
                    lines.append("These are the expressions this column was assigned in PHP code:\n")
                    for ev in (evidence if isinstance(evidence, list) else [evidence])[:10]:
                        if isinstance(ev, dict):
                            lines.append(f"- `{ev.get('rhs', ev)}` in `{ev.get('file', '')}:{ev.get('line', '')}`")
                        else:
                            lines.append(f"- `{ev}`")
            except Exception:
                pass

        # ── Inferred references from this column ──────────────────────────────
        try:
            inferred = db.execute(
                "MATCH (c:DatabaseColumn)-[r:INFERRED_REFERENCES]->(tgt:DatabaseTable) "
                "WHERE c.node_id = $nid "
                "RETURN tgt.name AS tgt_table, tgt.connection AS tgt_conn, "
                "r.confidence AS conf, r.condition AS cond, r.evidence_type AS ev_type",
                {"nid": col_node_id},
            ) if col_node_id else []
            if inferred:
                lines.append("\n### Inferred Target Tables\n")
                lines.append("| Target Table | Connection | Confidence | Condition | Evidence |")
                lines.append("|-------------|------------|------------|-----------|----------|")
                for ir in sorted(inferred, key=lambda r: -(r.get("conf") or 0)):
                    conf = ir.get("conf", 0) or 0
                    cond = ir.get("cond", "") or ""
                    ev = ir.get("ev_type", "") or ""
                    lines.append(
                        f"| `{ir.get('tgt_table')}` | {ir.get('tgt_conn') or '?'} | {conf:.0%} | {cond} | {ev} |"
                    )
        except Exception:
            pass

        # ── Value semantics (discriminator detection + varchar sampling) ─────────
        disc_dist: list[dict] | None = None
        resolve_conn_cfg = _get_conn_cfg_for_table(conn_name)

        if _is_discriminator_column(column, full_t, guard_raw if isinstance(guard_raw, str) else ""):
            if resolve_conn_cfg:
                disc_dist = _fetch_col_distribution(table, column, resolve_conn_cfg)
            if disc_dist:
                lines.append("\n### Value Distribution (live DB)\n")
                lines.append("| Value | Row Count |")
                lines.append("|-------|-----------|")
                for drow in disc_dist[:30]:
                    lines.append(f"| `{drow.get('val')}` | {drow.get('cnt'):,} |")

        # For plain varchar/text columns that aren't already covered by the
        # discriminator path, try to sample distinct values from the live DB.
        # Skipped for polymorphic ID columns (would return IDs, not meaningful values).
        _ctype_lower = full_t.lower()
        _is_varchar = (
            any(t in _ctype_lower for t in ("varchar", "char", "text"))
            and "enum" not in _ctype_lower
        )
        if _is_varchar and not col_data.get("polymorphic_candidate") and disc_dist is None:
            if resolve_conn_cfg:
                varchar_sample = _fetch_varchar_sample(table, column, resolve_conn_cfg)
                if varchar_sample:
                    lines.append("\n### Value Sample (live DB)\n")
                    lines.append("| Value | Count |")
                    lines.append("|-------|-------|")
                    for srow in varchar_sample:
                        lines.append(f"| `{srow.get('val')}` | {srow.get('cnt'):,} |")

        # ── Lazy LLM resolution ───────────────────────────────────────────────
        schema_sig = f"{full_t}:{col_data.get('polymorphic_candidate', False)}:{wpe_raw}"
        col_hash = hashlib.sha1(schema_sig.encode()).hexdigest()[:12]
        cache_key = f"dbctx:column:{conn_name}:{table}.{column}"
        annotation = _db_cache.get(cache_key, current_hash=col_hash)

        if not annotation and cfg.summary.enabled:
            guard_summary = guard_raw[:200] if isinstance(guard_raw, str) else ""
            wpe_summary = wpe_raw[:400] if isinstance(wpe_raw, str) else ""
            dist_summary = ""
            if disc_dist:
                dist_summary = "Value distribution: " + ", ".join(
                    f"{r.get('val')}={r.get('cnt')}" for r in disc_dist[:20]
                ) + "\n"
            prompt_source = (
                f"Database column: {table}.{column} ({full_t})\n"
                f"Nullable: {col_data.get('nullable', True)}\n"
                f"Polymorphic candidate: {col_data.get('polymorphic_candidate', False)}\n"
                + (f"Sibling type column: {col_data.get('sibling_type_column', '')}\n" if col_data.get("sibling_type_column") else "")
                + (f"Guard conditions (code checks these values): {guard_summary}\n" if guard_summary else "")
                + dist_summary
                + (f"Write-path expressions: {wpe_summary}\n" if wpe_summary else "")
                + "\nIMPORTANT: Lead with what code DOES with this column (guard conditions, "
                "write-path assignments, value distribution), not what the name implies. "
                "If guard conditions or value distribution are present, use them as the "
                "primary evidence. Be concrete about what each value means in the codebase."
            )
            annotation, used_provider = generate_summary(
                fqn=f"db.column.{table}.{column}",
                node_type="database column",
                source=prompt_source,
                summary_cfg=cfg.summary,
            )
            if annotation:
                _db_cache.set(cache_key, annotation, used_provider, schema_hash=col_hash)

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_resolve_column", {"table": table, "column": column}, 1, elapsed)

        if annotation:
            lines.append(f"\n### Semantic Resolution\n\n> {annotation}")

        return "\n".join(lines) + _next_steps(
            f"Use laravelgraph_db_context('{table}') for the full table picture",
            "Use laravelgraph_schema to see FK constraints across all tables",
        )

    # ── Tool: laravelgraph_procedure_context ──────────────────────────────────

    @mcp.tool()
    def laravelgraph_procedure_context(name: str, connection: str = "") -> str:
        """Stored procedure details with table access map and semantic annotation.

        Returns the procedure body, which tables it reads/writes, and a lazy
        LLM-generated annotation (generated once on first call, cached thereafter).

        Args:
            name: Procedure name (e.g. "calculate_order_totals")
            connection: DB connection name (optional)
        """
        db = _db()
        start = time.perf_counter()

        try:
            if connection:
                procs = db.execute(
                    "MATCH (p:StoredProcedure) WHERE p.name = $name AND p.connection = $conn RETURN p.* LIMIT 1",
                    {"name": name, "conn": connection},
                )
            else:
                procs = db.execute(
                    "MATCH (p:StoredProcedure) WHERE p.name = $name RETURN p.* LIMIT 1",
                    {"name": name},
                )
        except Exception as e:
            return f"Error: {e}"

        if not procs:
            return (
                f"Stored procedure `{name}` not found. "
                "Live DB introspection requires a configured connection — "
                "run `laravelgraph db-connections add` and re-analyze."
            )

        p = procs[0]
        p_nid = p.get("p.node_id", "")
        conn_name = p.get("p.connection", "") or connection or "?"
        body = p.get("p.body", "") or ""
        params_raw = p.get("p.parameters", "") or ""
        security = p.get("p.security_type", "") or ""
        definer = p.get("p.definer", "") or ""

        lines = [f"## Stored Procedure: `{name}` (connection: `{conn_name}`)\n"]
        if params_raw:
            lines.append(f"- **Parameters:** `{params_raw}`")
        if security:
            lines.append(f"- **Security:** {security}")
        if definer:
            lines.append(f"- **Definer:** {definer}")

        # Table access map
        try:
            reads = db.execute(
                "MATCH (p:StoredProcedure)-[:PROCEDURE_READS]->(t:DatabaseTable) WHERE p.node_id = $nid "
                "RETURN t.name AS tname",
                {"nid": p_nid},
            )
            writes = db.execute(
                "MATCH (p:StoredProcedure)-[:PROCEDURE_WRITES]->(t:DatabaseTable) WHERE p.node_id = $nid "
                "RETURN t.name AS tname",
                {"nid": p_nid},
            )
            if reads:
                lines.append("\n### Reads From\n")
                for r in reads:
                    lines.append(f"- `{r.get('tname')}`")
            if writes:
                lines.append("\n### Writes To\n")
                for w in writes:
                    lines.append(f"- `{w.get('tname')}`")
        except Exception:
            pass

        # Procedure body (truncated)
        if body:
            truncated = body[:2000]
            lines.append("\n### Body\n")
            lines.append("```sql")
            lines.append(truncated)
            if len(body) > 2000:
                lines.append(f"... ({len(body) - 2000} more chars)")
            lines.append("```")

        # ── Lazy LLM annotation ───────────────────────────────────────────────
        body_hash = hashlib.sha1(body[:500].encode()).hexdigest()[:12]
        cache_key = f"dbctx:proc:{conn_name}:{name}"
        annotation = _db_cache.get(cache_key, current_hash=body_hash)

        if not annotation and cfg.summary.enabled:
            prompt_source = (
                f"Stored procedure: {name}\n"
                f"Connection: {conn_name}\n"
                + (f"Parameters: {params_raw}\n" if params_raw else "")
                + (f"Body (truncated):\n{body[:800]}\n" if body else "")
            )
            annotation, used_provider = generate_summary(
                fqn=f"db.procedure.{name}",
                node_type="stored procedure",
                source=prompt_source,
                summary_cfg=cfg.summary,
            )
            if annotation:
                _db_cache.set(cache_key, annotation, used_provider, schema_hash=body_hash)

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_procedure_context", {"name": name, "connection": connection}, 1, elapsed)

        if annotation:
            lines.append(f"\n### Semantic Annotation\n\n> {annotation}")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_db_context(table) for any of the tables accessed by this procedure",
            "Use laravelgraph_connection_map to see all procedures across all connections",
        )

    # ── Tool: laravelgraph_connection_map ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_connection_map() -> str:
        """Map of all configured database connections with table counts, procedures,
        views, and any cross-database query patterns detected in code.
        """
        db = _db()
        start = time.perf_counter()

        lines = ["## Database Connection Map\n"]

        # ── Configured connections from config ────────────────────────────────
        configured = cfg.databases if hasattr(cfg, "databases") else []
        if configured:
            lines.append("### Configured Connections\n")
            lines.append("| Name | Host | Database | Driver | SSL |")
            lines.append("|------|------|----------|--------|-----|")
            for conn_cfg in configured:
                host = conn_cfg.host if hasattr(conn_cfg, "host") else "?"
                dbname = conn_cfg.database if hasattr(conn_cfg, "database") else "?"
                driver = conn_cfg.driver if hasattr(conn_cfg, "driver") else "mysql"
                ssl = "Yes" if (hasattr(conn_cfg, "ssl") and conn_cfg.ssl) else "No"
                lines.append(f"| `{conn_cfg.name}` | {host} | {dbname} | {driver} | {ssl} |")
            lines.append("")
        else:
            lines.append(
                "> No DB connections configured. "
                "Run `laravelgraph db-connections add` to add a MySQL/RDS connection.\n"
            )

        # ── Live DB nodes in graph ─────────────────────────────────────────────
        try:
            conn_summary = db.execute(
                "MATCH (t:DatabaseTable) WHERE t.connection IS NOT NULL AND t.connection <> '' "
                "RETURN t.connection AS conn, count(*) AS table_count"
            )
            if conn_summary:
                lines.append("### Live DB Data in Graph\n")
                lines.append("| Connection | Tables |")
                lines.append("|------------|--------|")
                for row in conn_summary:
                    lines.append(f"| `{row.get('conn')}` | {row.get('table_count')} |")
                lines.append("")
        except Exception:
            pass

        # ── Migration-derived tables ───────────────────────────────────────────
        try:
            mig_rows = db.execute(
                "MATCH (t:DatabaseTable) WHERE t.source = 'migration' OR t.connection IS NULL "
                "RETURN count(*) AS cnt"
            )
            if mig_rows:
                cnt = mig_rows[0].get("cnt", 0)
                if cnt:
                    lines.append(f"- **Migration-derived tables:** {cnt} (no live DB connection required)\n")
        except Exception:
            pass

        # ── Stored procedures per connection ──────────────────────────────────
        try:
            proc_summary = db.execute(
                "MATCH (p:StoredProcedure) RETURN p.connection AS conn, count(*) AS cnt"
            )
            if proc_summary:
                lines.append("### Stored Procedures\n")
                for row in proc_summary:
                    conn_label = row.get("conn") or "unknown"
                    lines.append(f"- `{conn_label}`: {row.get('cnt')} procedure(s)")
                lines.append("")
        except Exception:
            pass

        # ── Views per connection ───────────────────────────────────────────────
        try:
            view_summary = db.execute(
                "MATCH (v:DatabaseView) RETURN v.connection AS conn, count(*) AS cnt"
            )
            if view_summary:
                lines.append("### Database Views\n")
                for row in view_summary:
                    conn_label = row.get("conn") or "unknown"
                    lines.append(f"- `{conn_label}`: {row.get('cnt')} view(s)")
                lines.append("")
        except Exception:
            pass

        # ── Cross-DB access patterns ───────────────────────────────────────────
        try:
            cross_db = db.execute(
                "MATCH (src)-[q:QUERIES_TABLE]->(t:DatabaseTable) "
                "WHERE q.connection IS NOT NULL AND q.connection <> '' "
                "RETURN q.connection AS db_conn, count(*) AS access_count "
                "ORDER BY access_count DESC LIMIT 10"
            )
            if cross_db:
                lines.append("### Cross-DB Access (code explicitly names a connection)\n")
                for row in cross_db:
                    lines.append(f"- `{row.get('db_conn')}`: {row.get('access_count')} queries from code")
                lines.append("")
        except Exception:
            pass

        # ── QUERIES_TABLE summary ──────────────────────────────────────────────
        try:
            qt_total = db.execute("MATCH ()-[q:QUERIES_TABLE]->() RETURN count(*) AS cnt")
            if qt_total:
                cnt = qt_total[0].get("cnt", 0)
                lines.append(f"- **Total QUERIES_TABLE edges:** {cnt} (code → table access points detected)")
        except Exception:
            pass

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_connection_map", {}, 1, elapsed)

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_db_context(table) for full analysis of any table",
            "Use laravelgraph_schema(connection='name') to browse tables on a specific connection",
            "Use laravelgraph_procedure_context(name) for stored procedure details",
        )

    # ── Tool: laravelgraph_db_query ───────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_db_query(
        sql: str,
        connection: str = "",
        limit: int = 50,
        bypass_cache: bool = False,
    ) -> str:
        """Run a read-only SQL query against a configured live database.

        Executes SELECT, SHOW, DESCRIBE, or EXPLAIN statements against any
        configured database connection. Results are cached for 5 minutes by
        default — the same query called multiple times in a session hits the
        cache, not the DB.

        Use this when you need to see actual data values: lookup tables, enum
        meanings, reference rows, live counts. The graph tells you structure;
        this tool tells you content.

        Args:
            sql:          Read-only SQL — SELECT, SHOW, DESCRIBE, or EXPLAIN only.
                          Do NOT include a LIMIT clause — use the ``limit`` param.
            connection:   Connection name from config (default: first configured).
            limit:        Max rows to return (default 50, max 500).
            bypass_cache: Set True to skip cache and force a fresh DB query.
        """
        start = time.perf_counter()

        # ── Safety check ──────────────────────────────────────────────────────
        err = validate_sql(sql)
        if err:
            return f"**SQL rejected:** {err}"

        # ── Resolve connection ─────────────────────────────────────────────────
        db_configs = cfg.databases if hasattr(cfg, "databases") else []
        if not db_configs:
            return (
                "**No database connections configured.**\n"
                "Run `laravelgraph db-connections add` to add a MySQL/RDS connection."
            )

        if connection:
            conn_cfg = next((c for c in db_configs if c.name == connection), None)
            if not conn_cfg:
                names = ", ".join(f"`{c.name}`" for c in db_configs)
                return f"**Connection `{connection}` not found.** Available: {names}"
        else:
            conn_cfg = db_configs[0]

        conn_name = conn_cfg.name

        # ── Enforce row limit ──────────────────────────────────────────────────
        from laravelgraph.mcp.query_cache import _MAX_ROWS
        safe_limit = max(1, min(limit, _MAX_ROWS))

        # Inject LIMIT if not already present (only for SELECT)
        sql_for_exec = sql.strip().rstrip(";")
        if sql_for_exec.upper().lstrip().startswith("SELECT") and \
                "LIMIT" not in sql_for_exec.upper():
            sql_for_exec = f"{sql_for_exec} LIMIT {safe_limit}"

        # ── Cache lookup ───────────────────────────────────────────────────────
        cache_key = _query_cache.make_key(conn_name, sql_for_exec)
        ttl = conn_cfg.query_cache_ttl if hasattr(conn_cfg, "query_cache_ttl") else 300

        if not bypass_cache and ttl > 0:
            cached = _query_cache.get(cache_key, ttl=ttl)
            if cached:
                age = int(time.time() - cached["cached_at"])
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_db_query", {"sql": sql[:80], "connection": conn_name, "cached": True}, cached["row_count"], elapsed)
                return _format_query_result(
                    cached["columns"],
                    cached["rows"],
                    conn_name,
                    sql,
                    from_cache=True,
                    cache_age_sec=age,
                )

        # ── Live query ─────────────────────────────────────────────────────────
        try:
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
            mysql_conn = _connect_mysql(conn_cfg)
        except ImportError:
            return "**PyMySQL not installed.** Run: `pip install pymysql`"
        except Exception as exc:
            return f"**Connection failed** (`{conn_name}`): {exc}"

        try:
            with mysql_conn.cursor() as cur:
                cur.execute(sql_for_exec)
                raw_rows = cur.fetchall()
                columns: list[str] = [d[0] for d in (cur.description or [])]
        except Exception as exc:
            return f"**Query failed:** {exc}\n\nSQL: `{sql_for_exec}`"
        finally:
            try:
                mysql_conn.close()
            except Exception:
                pass

        # Convert tuples → dicts for storage
        rows = [dict(zip(columns, row)) for row in raw_rows]

        # ── Store in cache ─────────────────────────────────────────────────────
        if ttl > 0:
            _query_cache.set(cache_key, sql, conn_name, columns, rows, ttl=ttl)

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_db_query", {"sql": sql[:80], "connection": conn_name, "cached": False}, len(rows), elapsed)

        return _format_query_result(columns, rows, conn_name, sql, from_cache=False)

    def _format_query_result(
        columns: list[str],
        rows: list[dict],
        connection: str,
        sql: str,
        from_cache: bool = False,
        cache_age_sec: int = 0,
    ) -> str:
        """Render query results as a Markdown table."""
        cache_note = f" *(cached {cache_age_sec}s ago)*" if from_cache else " *(live)*"
        lines = [
            f"## Query Results — `{connection}`{cache_note}\n",
            f"```sql\n{sql.strip()}\n```\n",
        ]

        if not rows:
            lines.append("*No rows returned.*")
            return "\n".join(lines)

        lines.append(f"**{len(rows)} row(s)**\n")

        # Markdown table
        lines.append("| " + " | ".join(str(c) for c in columns) + " |")
        lines.append("|" + "|".join("---" for _ in columns) + "|")
        for row in rows:
            cells = [str(row.get(c, "")) for c in columns]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_db_context(table) to see how this table is used in code",
            "Use laravelgraph_schema(table) for column definitions and FK relationships",
            f"Run again with bypass_cache=True to force a fresh query",
        )

    # ── Tool: laravelgraph_db_impact ──────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_db_impact(
        table: str,
        operation: str = "write",
        connection: str = "",
    ) -> str:
        """Trace what happens in code when a database table is written to.

        Answers the question: "when this table gets a new/updated row, what
        events fire, what jobs are dispatched, what listeners run?"

        Walks the complete chain:
          DB write site → dispatched events/jobs → listeners → downstream jobs

        This closes the cross-layer gap that pure graph analysis misses — e.g.
        "when a trainee completes a type-6 event, what jobs fire?" requires
        connecting the DB write in VirtualCctvEventsServiceRevamp to the
        DISPATCHES edges that follow it.

        Args:
            table:      Table name (e.g. "course_events")
            operation:  "write" (default), "read", or "any"
            connection: DB connection name (optional)
        """
        db = _db()
        start = time.perf_counter()

        # ── Find write/read sites ──────────────────────────────────────────────
        try:
            if operation == "any":
                op_filter = ""
            elif operation == "read":
                op_filter = "AND q.operation = 'read' "
            else:
                op_filter = "AND q.operation <> 'read' "

            if connection:
                conn_filter = "AND t.connection = $conn "
                params: dict = {"tname": table, "conn": connection}
            else:
                conn_filter = ""
                params = {"tname": table}

            sites = db.execute(
                "MATCH (src:Method)-[q:QUERIES_TABLE]->(t:DatabaseTable) "
                "WHERE t.name = $tname "
                + conn_filter
                + op_filter
                + "RETURN src.name AS src_name, src.fqn AS src_fqn, "
                "q.operation AS op, q.via AS via, q.line AS line "
                "ORDER BY src.fqn LIMIT 30",
                params,
            )
        except Exception as e:
            return f"Error querying write sites: {e}"

        if not sites:
            op_label = "access" if operation == "any" else operation
            return (
                f"No `{op_label}` sites found for table `{table}`. "
                "Run `laravelgraph analyze` to index DB access patterns."
            )

        lines = [
            f"## DB Impact: `{table}` ({operation} paths)\n",
            f"**{len(sites)} code site(s) found** that {operation} this table.\n",
        ]

        seen_sites: set[str] = set()

        for site in sites:
            src_fqn = site.get("src_fqn", "") or ""
            src_name = site.get("src_name", src_fqn)
            op = site.get("op", "?")
            via = site.get("via", "?") or "?"
            line = site.get("line")
            line_str = f" line {line}" if line else ""

            if src_fqn in seen_sites:
                continue
            seen_sites.add(src_fqn)

            lines.append(f"\n### `{src_name}`{line_str}  _{op} via {via}_")
            lines.append(f"**FQN:** `{src_fqn}`\n")

            # ── Dispatches from this method ────────────────────────────────────
            dispatches: list[dict] = []
            try:
                dispatches = db.execute(
                    "MATCH (m:Method)-[d:DISPATCHES]->(evt) WHERE m.fqn = $fqn "
                    "RETURN evt.name AS name, evt.fqn AS efqn, "
                    "d.dispatch_type AS dtype, d.is_queued AS queued",
                    {"fqn": src_fqn},
                ) or []
            except Exception:
                pass

            # Also check the parent class (method might be dispatched at class level)
            if not dispatches:
                class_fqn = "::".join(src_fqn.split("::")[:-1]) if "::" in src_fqn else ""
                if class_fqn:
                    try:
                        dispatches = db.execute(
                            "MATCH (c)-[d:DISPATCHES]->(evt) WHERE c.fqn = $fqn "
                            "RETURN evt.name AS name, evt.fqn AS efqn, "
                            "d.dispatch_type AS dtype, d.is_queued AS queued",
                            {"fqn": class_fqn},
                        ) or []
                    except Exception:
                        pass

            if not dispatches:
                lines.append("_No direct event/job dispatches detected from this method._")
                continue

            for disp in dispatches:
                evt_name = disp.get("name", "?")
                evt_fqn = disp.get("efqn", "")
                dtype = disp.get("dtype", "event") or "event"
                queued = " (queued)" if disp.get("queued") else ""
                lines.append(f"- **dispatches** `{evt_name}`{queued} [{dtype}]")

                # ── Listeners for this event ───────────────────────────────────
                if dtype in ("event", "Event"):
                    listeners: list[dict] = []
                    try:
                        listeners = db.execute(
                            "MATCH (evt:Event)<-[:LISTENS_TO]-(l:Listener) WHERE evt.fqn = $fqn "
                            "RETURN l.name AS lname, l.fqn AS lfqn",
                            {"fqn": evt_fqn},
                        ) or []
                    except Exception:
                        pass

                    for li in listeners:
                        lname = li.get("lname", "?")
                        lfqn = li.get("lfqn", "")
                        lines.append(f"  - **listened by** `{lname}`")

                        # ── Jobs dispatched from listener ──────────────────────
                        try:
                            jobs = db.execute(
                                "MATCH (l:Listener)-[d:DISPATCHES]->(j) WHERE l.fqn = $fqn "
                                "RETURN j.name AS jname, d.is_queued AS queued",
                                {"fqn": lfqn},
                            ) or []
                            for job in jobs:
                                jq = " (queued)" if job.get("queued") else ""
                                lines.append(f"    - **dispatches job** `{job.get('jname')}`{jq}")
                        except Exception:
                            pass

                        if not listeners:
                            lines.append(f"  _(no listeners registered for `{evt_name}`)_")

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_db_impact", {"table": table, "operation": operation}, len(sites), elapsed)

        return "\n".join(lines) + _next_steps(
            f"Use laravelgraph_db_context('{table}') for full table schema and column details",
            "Use laravelgraph_events() for the complete event → listener → job map",
            f"Use laravelgraph_impact(ClassName) to trace the blast radius from any of the write sites above",
        )

    # ── Tool: laravelgraph_events ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_events() -> str:
        """Full event → listener → job dispatch map plus scheduled task summary."""
        db = _db()
        start = time.perf_counter()

        try:
            events = db.execute(
                "MATCH (e:Event) RETURN e.name AS name, e.fqn AS fqn, e.broadcastable AS bcast LIMIT 50"
            )
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_events", {}, len(events), elapsed)

        lines: list[str] = []

        if not events:
            lines.append("No events found. Ensure the project has been indexed with event analysis.\n")
        else:
            lines.append(f"## Event → Listener → Job Map ({len(events)} events)\n")
            for ev in events:
                fqn = ev.get("fqn", "")
                bcast = " 📡 (broadcastable)" if ev.get("bcast") else ""
                lines.append(f"### `{ev.get('name', '?')}`{bcast}")
                lines.append(f"- FQN: `{fqn}`")

                try:
                    listeners = db.execute(
                        "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.fqn = $fqn "
                        "RETURN l.name AS name, l.fqn AS lfqn, l.is_queued AS queued",
                        {"fqn": fqn},
                    )
                    for li in listeners:
                        queued = " (queued)" if li.get("queued") else ""
                        lines.append(f"- → Listener: `{li.get('name')}`{queued}")

                        jobs = db.execute(
                            "MATCH (l:Listener)-[:DISPATCHES]->(j:Job) WHERE l.fqn = $fqn "
                            "RETURN j.name AS job_name",
                            {"fqn": li.get("lfqn", "")},
                        )
                        for job in jobs:
                            lines.append(f"    → Job: `{job.get('job_name')}`")
                except Exception:
                    pass
                lines.append("")

        # ── Scheduled tasks ───────────────────────────────────────────────────
        try:
            tasks = db.execute(
                "MATCH (t:ScheduledTask) "
                "RETURN t.name AS name, t.command AS cmd, t.frequency AS freq, "
                "t.cron_expression AS cron, t.file_path AS fp, t.line AS ln "
                "ORDER BY t.name LIMIT 100"
            )
        except Exception:
            tasks = []

        # Check registry for scheduler_disabled flag
        _sched_disabled = False
        _sched_commented = 0
        try:
            from laravelgraph.core.registry import Registry as _Reg
            _entry = _Reg().get(project_root)
            if _entry and _entry.stats:
                _sched_disabled = bool(_entry.stats.get("scheduler_disabled", False))
                _sched_commented = int(_entry.stats.get("scheduler_commented_tasks", 0))
        except Exception:
            pass

        if tasks or _sched_disabled:
            lines.append(f"## Scheduled Tasks ({len(tasks)} active)\n")
            if _sched_disabled:
                lines.append(
                    f"> **⚠ Scheduler disabled** — {_sched_commented} task definition(s) are "
                    "commented out in Kernel.php. All cleanup, notification, and maintenance "
                    "jobs are effectively dead. No automated processing will run until the "
                    "schedule() method is re-enabled (or tasks are moved to bootstrap/app.php "
                    "for Laravel 11+).\n"
                )
            if tasks:
                lines.append("| Task | Frequency | File |")
                lines.append("|------|-----------|------|")
                for t in tasks:
                    freq = t.get("cron") or t.get("freq") or "custom"
                    fname = t.get("fp", "").split("/")[-1] if t.get("fp") else "?"
                    ln = f":{t.get('ln')}" if t.get("ln") else ""
                    lines.append(f"| `{t.get('name')}` | `{freq}` | `{fname}{ln}` |")
            lines.append("")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_context(EventName) for full event details",
            "Use laravelgraph_impact(EventName) to see what breaks if the event changes",
        )

    # ── Tool: laravelgraph_bindings ──────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_bindings(abstract_filter: str = "") -> str:
        """Service container binding map: what's bound, where, how."""
        db = _db()
        start = time.perf_counter()

        try:
            if abstract_filter:
                bindings = db.execute(
                    "MATCH (b:ServiceBinding) WHERE b.abstract CONTAINS $f RETURN b.* LIMIT 50",
                    {"f": abstract_filter},
                )
            else:
                bindings = db.execute("MATCH (b:ServiceBinding) RETURN b.* LIMIT 100")
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_bindings", {}, len(bindings), elapsed)

        if not bindings:
            return "No service container bindings found."

        lines = [f"## Service Container Bindings ({len(bindings)})\n"]
        lines.append("| Abstract | Concrete | Type | Provider |")
        lines.append("|----------|----------|------|----------|")

        for b in bindings:
            abstract = (b.get("b.abstract", "") or "").split("\\")[-1]
            concrete = (b.get("b.concrete", "") or "").split("\\")[-1]
            binding_type = b.get("b.binding_type", "?")
            provider = (b.get("b.provider_fqn", "") or "").split("\\")[-1]
            lines.append(f"| `{abstract}` | `{concrete}` | {binding_type} | {provider} |")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_context(ClassName) to see where a bound class is injected",
            "Use laravelgraph_impact(InterfaceName) to see what depends on a binding",
        )

    # ── Tool: laravelgraph_config_usage ──────────────────────────────────────

    @mcp.tool()
    def laravelgraph_config_usage(key: str = "", symbol: str = "") -> str:
        """Show all code depending on a config key or environment variable.

        Args:
            key: Config key (e.g. "app.name") or env variable name (e.g. "APP_KEY")
            symbol: Alias for key — use either parameter name
        """
        if not key and symbol:
            key = symbol
        db = _db()
        start = time.perf_counter()

        is_env = key.upper() == key and "_" in key

        usages: list = []
        try:
            if is_env:
                for src_label in ("Method", "Function_", "Class_", "File"):
                    try:
                        rows = db.execute(
                            f"MATCH (n:{src_label})-[r:USES_ENV]->(e:EnvVariable) WHERE e.name = $k "
                            "RETURN n.fqn AS fqn, n.file_path AS file, r.line AS line",
                            {"k": key},
                        )
                        usages.extend(rows)
                    except Exception:
                        pass
            else:
                for src_label in ("Method", "Function_", "Class_", "File"):
                    try:
                        rows = db.execute(
                            f"MATCH (n:{src_label})-[r:USES_CONFIG]->(c:ConfigKey) WHERE c.key = $k "
                            "RETURN n.fqn AS fqn, n.file_path AS file, r.line AS line",
                            {"k": key},
                        )
                        usages.extend(rows)
                    except Exception:
                        pass
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_config_usage", {"key": key}, len(usages), elapsed)

        if not usages:
            return f"No code found depending on `{key}`."

        lines = [f"## Config Usage: `{key}` ({len(usages)} usages)\n"]
        for u in usages:
            lines.append(f"- `{u.get('fqn', '?')}` — {u.get('file', '?')}:{u.get('line', '?')}")

        return "\n".join(lines)

    # ── Tool: laravelgraph_suggest_tests ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_suggest_tests(symbol: str) -> str:
        """Given a symbol or file change, suggest which test files/methods to run.

        Args:
            symbol: Symbol FQN, node_id, or file path fragment
        """
        db = _db()
        start = time.perf_counter()

        # Find test files that reference this symbol
        test_patterns = [
            f"tests/Unit",
            f"tests/Feature",
            f"tests/Integration",
        ]

        try:
            # Find files that call methods of this class
            node = _resolve_symbol(db, symbol)
            suggestions: list[str] = []

            if node:
                fqn = node.get("fqn", node.get("name", symbol))
                # Look for test files that import or reference this class
                test_files = db.execute(
                    "MATCH (f:File) WHERE (f.relative_path STARTS WITH 'tests/' OR f.relative_path STARTS WITH 'test/') "
                    "AND f.name CONTAINS 'Test' RETURN f.path AS path, f.relative_path AS rel LIMIT 50"
                )
                class_short = fqn.split("\\")[-1] if "\\" in fqn else fqn

                for tf in test_files:
                    file_path = tf.get("path", "")
                    if file_path:
                        # Check if test file content mentions this class
                        try:
                            content = Path(file_path).read_text(errors="replace")
                            if class_short in content:
                                suggestions.append(tf.get("rel", file_path))
                        except Exception:
                            pass
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_suggest_tests", {"symbol": symbol}, len(suggestions), elapsed)

        if not suggestions:
            return (
                f"No specific test files found for `{symbol}`.\n\n"
                "**General recommendations:**\n"
                "- Run `php artisan test --filter=<ClassName>` to find related tests\n"
                "- Check `tests/Feature/` for HTTP/integration tests\n"
                "- Check `tests/Unit/` for isolated unit tests"
            )

        lines = [f"## Suggested Tests for `{symbol}` ({len(suggestions)} files)\n"]
        for s in suggestions:
            lines.append(f"- `{s}`")

        lines.append("\n**Run with:**")
        lines.append("```bash")
        lines.append("php artisan test --filter=" + symbol.split("\\")[-1])
        lines.append("# or")
        lines.append("./vendor/bin/pest " + " ".join(suggestions[:3]))
        lines.append("```")

        return "\n".join(lines)

    # ── Tool: laravelgraph_explain ────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_explain(feature: str) -> str:
        """End-to-end explanation of how a feature works.

        Uses a multi-anchor search strategy: runs HybridSearch upfront to find the
        BEST entry point for the feature — which may be a route, a service class,
        a job, or an event — and expands from that anchor.

        This avoids the common failure mode where simple substring matching on route
        URIs returns dozens of unrelated routes (e.g. "online" matches 46 routes).
        Instead, the highest-scoring symbol from semantic + BM25 + fuzzy search wins.

        Includes PHP source code, event chains, linked models, and DB tables accessed.

        Args:
            feature: Feature or concept to explain (e.g. "user registration", "CCTV virtual delivery", "payment processing")
        """
        db = _db()
        start = time.perf_counter()

        from laravelgraph.mcp.explain import (
            find_routes_for_feature,
            find_commands_for_feature,
            trace_method_flow,
            trace_event_chain,
            trace_model_summary,
            read_source_snippet,
        )
        from laravelgraph.search.hybrid import HybridSearch

        terms = [t.lower() for t in feature.split() if len(t) > 2]
        if not terms:
            terms = [feature.lower()]

        lines = [f"## How '{feature}' works\n"]

        # ── Step 1: Run HybridSearch upfront to score all symbol types ────────
        # This is the key architectural change: instead of route-first substring
        # matching, we use semantic scores to pick the BEST anchor.
        search_results: list = []
        try:
            search = HybridSearch(db, cfg.search)
            search.build_index()
            search_results = search.search(feature, limit=20)
        except Exception:
            pass

        # Separate results by type
        route_results = [r for r in search_results if r.label in ("Route",)]
        service_results = [
            r for r in search_results
            if r.label in ("Class_", "Method", "Service", "Job", "Listener", "Event")
        ]

        best_route_score = route_results[0].score if route_results else 0.0
        best_service_score = service_results[0].score if service_results else 0.0

        # Also run substring-based route matching for recall completeness
        matched_routes = find_routes_for_feature(db, terms)
        route_match_count = len(matched_routes)

        # ── Step 2: Decide the primary anchor ─────────────────────────────────
        # Route substring matching is high-recall but low-precision (matches
        # "online" in 46 routes). Use it only when it returns FEW routes (≤5)
        # OR when it scores clearly better than any service/class result.
        USE_ROUTE_ANCHOR = (
            route_match_count > 0 and route_match_count <= 5
        ) or (
            best_route_score > 0 and best_route_score >= best_service_score * 0.9
            and route_match_count <= 10
        )

        anchor_used = "route" if USE_ROUTE_ANCHOR and matched_routes else "service"

        if USE_ROUTE_ANCHOR and matched_routes:
            # ── Route-anchored path ───────────────────────────────────────────
            lines.append(f"### HTTP Entry Points ({route_match_count} routes)\n")
            for r in matched_routes[:5]:
                method = r.get("hm", "?")
                uri    = r.get("uri", "?")
                ctrl   = r.get("ctrl", "")
                action = r.get("action", "handle")
                name   = r.get("rname", "")

                try:
                    mw_list = json.loads(r.get("mw") or "[]")
                    mw_str = f" — middleware: {', '.join(mw_list[:3])}" if mw_list else ""
                except Exception:
                    mw_str = ""

                lines.append(f"#### `{method} {uri}`{' (' + name + ')' if name else ''}")
                if ctrl:
                    lines.append(f"→ `{ctrl}::{action}`{mw_str}\n")

                    method_nid = f"method:{ctrl}::{action}"
                    cached = _summary_cache.get(method_nid)
                    if cached:
                        lines.append(f"**Summary:** {cached}\n")
                    else:
                        trace_method_flow(db, ctrl, action, lines, project_root=project_root)
                        if cfg.summary.enabled:
                            try:
                                method_rows = db.execute(
                                    "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
                                    "WHERE c.fqn = $cfqn AND m.name = $mname "
                                    "RETURN m.node_id AS nid, m.docblock AS doc, "
                                    "m.file_path AS fp, m.line_start AS ls, m.line_end AS le",
                                    {"cfqn": ctrl, "mname": action},
                                )
                                if method_rows:
                                    row = method_rows[0]
                                    src = read_source_snippet(
                                        row.get("fp", ""), row.get("ls", 0),
                                        row.get("le", 0), project_root,
                                    )
                                    if src:
                                        summary, provider_used = generate_summary(
                                            fqn=f"{ctrl}::{action}",
                                            node_type="controller action",
                                            source=src,
                                            docblock=row.get("doc", ""),
                                            summary_cfg=cfg.summary,
                                        )
                                        if summary and row.get("nid"):
                                            _summary_cache.set(
                                                row["nid"], summary, provider_used,
                                                file_path=row.get("fp", ""),
                                            )
                            except Exception:
                                pass
                lines.append("")

        else:
            # ── Service/class-anchored path ───────────────────────────────────
            # The top hybrid search result (service, job, event, class) is more
            # relevant than a route match. Expand from it.
            anchor_used = "service"
            top_symbols = service_results[:5] or search_results[:5]

            if top_symbols:
                lines.append(f"### Best Match: `{top_symbols[0].fqn}` (score: {top_symbols[0].score:.3f})\n")
                lines.append(
                    f"> _{route_match_count} route(s) also matched the search terms but scored lower "
                    f"than semantic results — using class-anchored expansion._\n"
                    if route_match_count > 0 else ""
                )

                for sr in top_symbols[:3]:
                    node_id = sr.node_id or ""
                    fqn = sr.fqn or ""
                    label = sr.label or ""

                    lines.append(f"#### `{label}`: `{fqn}`")

                    # Show cached summary or snippet
                    cached_sum = _summary_cache.get(node_id, file_path=sr.file_path or "")
                    if cached_sum:
                        lines.append(f"**Summary:** {cached_sum}\n")
                    elif sr.snippet:
                        lines.append(f"**Snippet:** {sr.snippet}\n")

                    # For class/method nodes, show source and method-level detail
                    if label in ("Class_", "Method", "Service"):
                        try:
                            node_rows = db.execute(
                                "MATCH (n) WHERE n.node_id = $nid "
                                "RETURN n.file_path AS fp, n.line_start AS ls, n.line_end AS le, "
                                "n.fqn AS fqn, n.name AS name",
                                {"nid": node_id},
                            ) if node_id else []
                            if node_rows:
                                nr = node_rows[0]
                                src = read_source_snippet(
                                    nr.get("fp", ""), nr.get("ls", 0),
                                    nr.get("le", 0), project_root,
                                )
                                if src:
                                    rel = Path(nr.get("fp", "")).name
                                    lines.append(f"**Source** (`{rel}:{nr.get('ls', 0)}-{nr.get('le', 0)}`):")
                                    lines.append("```php")
                                    lines.append(src)
                                    lines.append("```")
                        except Exception:
                            pass

                    # Show DB tables this symbol queries
                    try:
                        db_tables = db.execute(
                            "MATCH (n)-[:QUERIES_TABLE]->(t:DatabaseTable) WHERE n.node_id = $nid "
                            "RETURN t.name AS tname, t.connection AS conn LIMIT 5",
                            {"nid": node_id},
                        ) if node_id else []
                        if db_tables:
                            lines.append(f"**DB tables accessed:** " + ", ".join(
                                f"`{r.get('tname')}`" for r in db_tables
                            ))
                    except Exception:
                        pass

                    # Show events dispatched
                    try:
                        dispatches = db.execute(
                            "MATCH (n)-[:DISPATCHES]->(e:Event) WHERE n.node_id = $nid "
                            "RETURN e.node_id AS enid, e.name AS ename LIMIT 3",
                            {"nid": node_id},
                        ) if node_id else []
                        if dispatches:
                            lines.append(f"**Events dispatched:** " + ", ".join(
                                f"`{r.get('ename')}`" for r in dispatches
                            ))
                            for ev in dispatches[:2]:
                                trace_event_chain(db, ev["enid"], ev.get("ename", "?"), lines, project_root=project_root)
                    except Exception:
                        pass

                    lines.append("")

                # Show routes that eventually reach any of these classes
                if route_match_count > 0:
                    lines.append(f"\n### Related Routes ({route_match_count} matched — shown for reference)\n")
                    for r in matched_routes[:3]:
                        method = r.get("hm", "?")
                        uri = r.get("uri", "?")
                        ctrl = r.get("ctrl", "")
                        action = r.get("action", "")
                        name = r.get("rname", "")
                        lines.append(
                            f"- `{method} {uri}`{' (' + name + ')' if name else ''}"
                            + (f" → `{ctrl}::{action}`" if ctrl else "")
                        )
                    lines.append("")

        # ── Artisan commands matching the feature ─────────────────────────────
        matched_commands = find_commands_for_feature(db, terms)
        if matched_commands:
            lines.append(f"### Artisan Commands ({len(matched_commands)})\n")
            for cmd in matched_commands[:3]:
                lines.append(f"- `{cmd.get('sig', cmd.get('name', '?'))}` — {cmd.get('desc', '')}")
            lines.append("")

        # ── Events matching the feature (term-based) ──────────────────────────
        try:
            events = db.execute(
                "MATCH (e:Event) RETURN e.node_id AS nid, e.name AS name, e.fqn AS fqn LIMIT 100"
            )
            matched_events = [
                e for e in events
                if any(t in (e.get("name") or "").lower() for t in terms)
            ]
            if matched_events and anchor_used == "route":
                # Only show in route-anchored path; service path shows events per-symbol
                lines.append(f"### Events ({len(matched_events)})\n")
                for ev in matched_events[:3]:
                    trace_event_chain(db, ev["nid"], ev.get("name", "?"), lines, project_root=project_root)
        except Exception:
            pass

        # ── Models matching the feature ───────────────────────────────────────
        try:
            models_rows = db.execute(
                "MATCH (m:EloquentModel) RETURN m.node_id AS nid, m.name AS name LIMIT 100"
            )
            matched_models = [
                m for m in models_rows
                if any(t in (m.get("name") or "").lower() for t in terms)
            ]
            if matched_models:
                lines.append(f"\n### Models ({len(matched_models)})\n")
                for m in matched_models[:3]:
                    trace_model_summary(db, m["nid"], m.get("name", "?"), lines)
        except Exception:
            pass

        # ── Nothing found at all ──────────────────────────────────────────────
        if not search_results and not matched_routes and not matched_commands:
            if search_results:
                lines.append("### Related Symbols (no strong match found)\n")
                for r in search_results[:5]:
                    cached = _summary_cache.get(r.node_id or "", file_path=r.file_path or "")
                    summary_str = f" — {cached}" if cached else (f" — {r.snippet}" if r.snippet else "")
                    lines.append(f"- **{r.label}** `{r.fqn}`{summary_str}")
                lines.append("")

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_explain", {"feature": feature, "anchor": anchor_used}, 1, elapsed)

        if len(lines) <= 2:
            return (
                f"No components found for '{feature}'.\n\n"
                f"Try: laravelgraph_query('{feature}') to search for related symbols."
            )

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_feature_context(feature) for a structured full-picture view",
            "Use laravelgraph_context(ClassName) to inspect any specific class in depth",
            "Use laravelgraph_request_flow(route) to trace a specific route lifecycle",
            "Use laravelgraph_query(feature) to search for related symbols directly",
        )

    # ── Tool: laravelgraph_feature_context ───────────────────────────────────

    @mcp.tool()
    def laravelgraph_feature_context(feature: str) -> str:
        """Complete structured context for a feature — routes, controllers (with source), models, events, jobs, config.

        This is the primary tool for feature exploration. Call this ONCE instead of
        making 10+ individual tool calls. Returns everything an AI agent needs to
        understand a feature: HTTP entry points, controller source code, Eloquent models
        with relationships, events and their listener chains, queued jobs, Blade views
        rendered, and config/env dependencies.

        Semantic summaries are shown for symbols that have been previously queried
        (lazy cache). Source code is always included as fallback.

        Args:
            feature: Feature name or keyword (e.g. "checkout", "user registration", "payment", "booking")
        """
        db = _db()
        start = time.perf_counter()

        from laravelgraph.mcp.explain import (
            find_routes_for_feature,
            read_source_snippet,
            clean_docblock,
            _append_source_block,
        )

        terms = [t.lower() for t in feature.split() if len(t) > 2]
        if not terms:
            terms = [feature.lower()]

        lines = [f"## Feature Context: `{feature}`\n"]
        symbol_count = 0

        # ── 1. HTTP Entry Points ──────────────────────────────────────────────
        matched_routes = find_routes_for_feature(db, terms)

        # Also search for controllers/classes matching the feature
        ctrl_fqns_seen: set[str] = set()
        model_nids_seen: set[str] = set()
        event_nids_seen: set[str] = set()
        job_nids_seen: set[str] = set()

        if matched_routes:
            lines.append(f"### HTTP Entry Points ({len(matched_routes)} routes)\n")
            lines.append("| Method | URI | Controller | Middleware |")
            lines.append("|--------|-----|------------|------------|")
            for r in matched_routes:
                method = r.get("hm", "?")
                uri    = r.get("uri", "?")
                ctrl   = r.get("ctrl", "")
                action = r.get("action", "handle")
                try:
                    mw_list = json.loads(r.get("mw") or "[]")
                    mw_str = ", ".join(mw_list[:3])
                except Exception:
                    mw_str = ""
                binding = f"`{ctrl.split(chr(92))[-1]}::{action}`" if ctrl else "Closure"
                lines.append(f"| `{method}` | `{uri}` | {binding} | {mw_str} |")
                if ctrl:
                    ctrl_fqns_seen.add(f"{ctrl}::{action}")
            lines.append("")

        # ── 2. Controller Actions ─────────────────────────────────────────────
        if ctrl_fqns_seen:
            lines.append(f"### Controller Actions ({len(ctrl_fqns_seen)})\n")
            for ctrl_action in list(ctrl_fqns_seen)[:5]:
                if "::" not in ctrl_action:
                    continue
                ctrl_fqn, action = ctrl_action.rsplit("::", 1)
                ctrl_short = ctrl_fqn.split("\\")[-1]
                lines.append(f"#### `{ctrl_short}::{action}`")

                # Check cache first
                method_nid = f"method:{ctrl_fqn}::{action}"
                cached = _summary_cache.get(method_nid)
                if cached:
                    lines.append(f"**Summary:** {cached}")
                    lines.append("")
                else:
                    try:
                        method_rows = db.execute(
                            "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
                            "WHERE c.fqn = $cfqn AND m.name = $mname "
                            "RETURN m.node_id AS nid, m.docblock AS doc, m.file_path AS fp, "
                            "m.line_start AS ls, m.line_end AS le, "
                            "m.param_types AS params, m.return_type AS rt",
                            {"cfqn": ctrl_fqn, "mname": action},
                        )
                        if method_rows:
                            row = method_rows[0]
                            fp = row.get("fp", "")
                            ls = row.get("ls") or 0
                            le = row.get("le") or 0

                            # Option 2: docblock
                            doc = clean_docblock(row.get("doc", ""))
                            if doc:
                                lines.append(f"**Purpose:** {doc}")

                            # Option 1: source code
                            if fp and ls:
                                _append_source_block(fp, ls, le, project_root, lines)

                            # Try to generate summary and cache
                            if cfg.summary.enabled and fp and ls:
                                src = read_source_snippet(fp, ls, le, project_root)
                                if src:
                                    summary, provider_used = generate_summary(
                                        fqn=ctrl_action,
                                        node_type="controller action",
                                        source=src,
                                        docblock=row.get("doc", ""),
                                        summary_cfg=cfg.summary,
                                    )
                                    if summary and row.get("nid"):
                                        _summary_cache.set(
                                            row["nid"], summary,
                                            provider_used, file_path=fp,
                                        )
                    except Exception:
                        pass

                # Injected deps
                try:
                    inj = db.execute(
                        "MATCH (c:Class_)-[:INJECTS]->(dep) WHERE c.fqn = $fqn "
                        "RETURN dep.name AS name LIMIT 6",
                        {"fqn": ctrl_fqn},
                    )
                    if inj:
                        names = [r.get("name", "?") for r in inj]
                        lines.append(f"**Injects:** {', '.join(f'`{n}`' for n in names)}")
                except Exception:
                    pass

                # Validates with
                try:
                    frs = db.execute(
                        "MATCH (m:Method)-[:VALIDATES_WITH]->(fr:FormRequest) "
                        "WHERE m.fqn = $fqn "
                        "RETURN fr.name AS name, fr.rules_summary AS rules",
                        {"fqn": ctrl_action},
                    )
                    for fr in frs:
                        name = fr.get("name", "?")
                        try:
                            fields = json.loads(fr.get("rules") or "[]")
                            field_str = f" (fields: {', '.join(str(f) for f in fields[:6])})" if fields else ""
                        except Exception:
                            field_str = ""
                        lines.append(f"**Validates:** `{name}`{field_str}")
                except Exception:
                    pass

                # Calls
                try:
                    calls = db.execute(
                        "MATCH (m:Method)-[c:CALLS]->(t) WHERE m.fqn = $fqn "
                        "RETURN t.fqn AS fqn, t.name AS name ORDER BY c.confidence DESC LIMIT 8",
                        {"fqn": ctrl_action},
                    )
                    if calls:
                        call_strs = [f"`{(r.get('fqn') or r.get('name') or '?').split(chr(92))[-1]}`" for r in calls]
                        lines.append(f"**Calls:** {', '.join(call_strs)}")
                except Exception:
                    pass

                # Dispatches (controller method, hop 0)
                try:
                    disps = db.execute(
                        "MATCH (m:Method)-[d:DISPATCHES]->(t) WHERE m.fqn = $fqn "
                        "RETURN t.node_id AS nid, t.name AS name, t.fqn AS fqn, "
                        "d.dispatch_type AS dtype, d.is_queued AS queued",
                        {"fqn": ctrl_action},
                    )
                    for d in disps:
                        dtype = (d.get("dtype") or "event").lower()
                        q = " *(queued)*" if d.get("queued") else ""
                        lines.append(f"**Dispatches {dtype}:** `{d.get('name', '?')}`{q}")
                        if dtype == "job" and d.get("nid"):
                            job_nids_seen.add(d["nid"])
                        elif d.get("nid"):
                            event_nids_seen.add(d["nid"])
                except Exception:
                    pass

                # Renders views
                try:
                    views = db.execute(
                        "MATCH (m:Method)-[:RENDERS_TEMPLATE]->(t:BladeTemplate) WHERE m.fqn = $fqn "
                        "RETURN t.name AS name LIMIT 5",
                        {"fqn": ctrl_action},
                    )
                    if views:
                        vnames = [v.get("name", "?") for v in views]
                        lines.append(f"**Renders:** {', '.join(f'`{n}`' for n in vnames)}")
                except Exception:
                    pass

                # ── BFS: walk CALLS 2 hops to collect models, events, jobs ──
                # A controller typically calls a service which calls models/dispatches.
                # 1-hop misses everything behind the service layer.
                try:
                    bfs_frontier_fqns: list[str] = [ctrl_action]
                    bfs_frontier_nids: list[str] = [f"method:{ctrl_fqn}::{action}"]
                    bfs_visited_fqns: set[str] = {ctrl_action}
                    for _hop in range(2):
                        next_fqns: list[str] = []
                        next_nids: list[str] = []
                        for _nid in bfs_frontier_nids:
                            # Follow CALLS — query each target label separately
                            # (KuzuDB does not support polymorphic target._label access)
                            for _target_lbl in ("Method", "Class_", "EloquentModel", "Function_"):
                                try:
                                    _called = db.execute(
                                        f"MATCH (m:Method)-[:CALLS]->(t:{_target_lbl}) WHERE m.node_id = $nid "
                                        f"RETURN t.node_id AS tid, t.fqn AS fqn, '{_target_lbl}' AS lbl LIMIT 12",
                                        {"nid": _nid},
                                    )
                                    for _c in _called:
                                        _fqn = _c.get("fqn", "")
                                        _lbl = _c.get("lbl", "")
                                        if not _fqn or _fqn in bfs_visited_fqns:
                                            continue
                                        bfs_visited_fqns.add(_fqn)
                                        if _lbl == "EloquentModel":
                                            if _c.get("tid"):
                                                model_nids_seen.add(_c["tid"])
                                        else:
                                            if _c.get("tid"):
                                                next_fqns.append(_fqn)
                                                next_nids.append(_c["tid"])
                                except Exception:
                                    pass
                            # Collect DISPATCHES from this node
                            try:
                                _disps = db.execute(
                                    "MATCH (m:Method)-[d:DISPATCHES]->(t) WHERE m.node_id = $nid "
                                    "RETURN t.node_id AS tnid, t.name AS tname, d.dispatch_type AS dtype",
                                    {"nid": _nid},
                                )
                                for _d in _disps:
                                    _dtype = (_d.get("dtype") or "event").lower()
                                    if _d.get("tnid"):
                                        if _dtype == "job":
                                            job_nids_seen.add(_d["tnid"])
                                        else:
                                            event_nids_seen.add(_d["tnid"])
                            except Exception:
                                pass
                        bfs_frontier_fqns = next_fqns
                        bfs_frontier_nids = next_nids
                        if not bfs_frontier_nids:
                            break
                except Exception:
                    pass

                lines.append("")
                symbol_count += 1

        # ── 3. Models Used ────────────────────────────────────────────────────
        # Also find models matching terms directly
        try:
            all_models = db.execute(
                "MATCH (m:EloquentModel) RETURN m.node_id AS nid, m.name AS name, "
                "m.fqn AS fqn, m.db_table AS tbl, m.fillable AS fillable, "
                "m.soft_deletes AS soft LIMIT 100"
            )
            term_models = [
                m for m in all_models
                if any(t in (m.get("name") or "").lower() for t in terms)
            ]
            for m in term_models:
                if m.get("nid"):
                    model_nids_seen.add(m["nid"])
        except Exception:
            pass

        if model_nids_seen:
            lines.append(f"### Models Used ({len(model_nids_seen)})\n")
            for mnid in list(model_nids_seen)[:6]:
                try:
                    mrows = db.execute(
                        "MATCH (m:EloquentModel) WHERE m.node_id = $nid "
                        "RETURN m.name AS name, m.fqn AS fqn, m.db_table AS tbl, "
                        "m.fillable AS fillable, m.soft_deletes AS soft",
                        {"nid": mnid},
                    )
                    if not mrows:
                        continue
                    m = mrows[0]
                    mname = m.get("name", "?")
                    tbl   = m.get("tbl", "?")
                    soft  = " *(soft deletes)*" if m.get("soft") else ""
                    lines.append(f"#### `{mname}` → table: `{tbl}`{soft}")

                    # Summary
                    cached = _summary_cache.get(mnid)
                    if cached:
                        lines.append(f"**Summary:** {cached}")

                    # Fillable
                    try:
                        fields = json.loads(m.get("fillable") or "[]")
                        if fields:
                            lines.append(f"**Fillable:** {', '.join(f'`{f}`' for f in fields[:10])}")
                    except Exception:
                        pass

                    # Relationships
                    rels = db.execute(
                        "MATCH (em:EloquentModel)-[r:HAS_RELATIONSHIP]->(rel:EloquentModel) "
                        "WHERE em.node_id = $nid "
                        "RETURN r.relationship_type AS rtype, r.method_name AS method, rel.name AS rname LIMIT 8",
                        {"nid": mnid},
                    )
                    for r in rels:
                        lines.append(f"- `{r.get('method', '?')}()` {r.get('rtype', '?')} → `{r.get('rname', '?')}`")
                    lines.append("")
                    symbol_count += 1
                except Exception:
                    pass

        # ── 4. Events and Listeners ───────────────────────────────────────────
        # Also find events matching terms
        try:
            all_events = db.execute(
                "MATCH (e:Event) RETURN e.node_id AS nid, e.name AS name, e.fqn AS fqn LIMIT 100"
            )
            for ev in all_events:
                if any(t in (ev.get("name") or "").lower() for t in terms):
                    if ev.get("nid"):
                        event_nids_seen.add(ev["nid"])
        except Exception:
            pass

        if event_nids_seen:
            lines.append(f"### Events & Listeners ({len(event_nids_seen)})\n")
            for enid in list(event_nids_seen)[:5]:
                try:
                    erows = db.execute(
                        "MATCH (e:Event) WHERE e.node_id = $nid "
                        "RETURN e.name AS name, e.fqn AS fqn, e.broadcastable AS bcast",
                        {"nid": enid},
                    )
                    if not erows:
                        continue
                    ev = erows[0]
                    bcast = " 📡 *(broadcastable)*" if ev.get("bcast") else ""
                    lines.append(f"#### Event: `{ev.get('name', '?')}`{bcast}")

                    listeners = db.execute(
                        "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.node_id = $nid "
                        "RETURN l.name AS name, l.fqn AS lfqn, l.is_queued AS queued, l.queue AS queue LIMIT 8",
                        {"nid": enid},
                    )
                    for li in listeners:
                        q = f" *(queue: {li.get('queue')})*" if li.get("queued") and li.get("queue") else " *(queued)*" if li.get("queued") else ""
                        lines.append(f"→ **Listener:** `{li.get('name', '?')}`{q}")
                    lines.append("")
                    symbol_count += 1
                except Exception:
                    pass

        # ── 5. Jobs (call-chain discovered + name term matching) ─────────────
        # job_nids_seen is populated by the BFS in the controller section above.
        # Also add any jobs whose name matches the search terms directly.
        try:
            all_jobs = db.execute(
                "MATCH (j:Job) RETURN j.node_id AS nid, j.name AS name, j.fqn AS fqn, "
                "j.queue AS queue, j.tries AS tries, j.timeout AS timeout LIMIT 200"
            )
            for j in all_jobs:
                if any(t in (j.get("name") or "").lower() for t in terms):
                    if j.get("nid"):
                        job_nids_seen.add(j["nid"])
        except Exception:
            pass

        if job_nids_seen:
            lines.append(f"### Jobs Dispatched ({len(job_nids_seen)})\n")
            for jnid in list(job_nids_seen)[:8]:
                try:
                    jrows = db.execute(
                        "MATCH (j:Job) WHERE j.node_id = $nid "
                        "RETURN j.name AS name, j.fqn AS fqn, j.queue AS queue, "
                        "j.tries AS tries, j.timeout AS timeout",
                        {"nid": jnid},
                    )
                    if not jrows:
                        continue
                    j = jrows[0]
                    q = f" (queue: `{j.get('queue')}`)" if j.get("queue") else ""
                    tries = f", tries: {j.get('tries')}" if j.get("tries") else ""
                    timeout = f", timeout: {j.get('timeout')}s" if j.get("timeout") else ""
                    lines.append(f"- `{j.get('name', '?')}`{q}{tries}{timeout}")
                    symbol_count += 1
                except Exception:
                    pass
            lines.append("")

        # ── 6. Config/Env dependencies ────────────────────────────────────────
        if ctrl_fqns_seen:
            config_keys: list[str] = []
            env_keys: list[str] = []
            for ctrl_action in list(ctrl_fqns_seen)[:3]:
                try:
                    cfgs = db.execute(
                        "MATCH (m:Method)-[:USES_CONFIG]->(c:ConfigKey) WHERE m.fqn = $fqn "
                        "RETURN c.key AS key LIMIT 10",
                        {"fqn": ctrl_action},
                    )
                    config_keys.extend(r.get("key", "") for r in cfgs)
                    envs = db.execute(
                        "MATCH (m:Method)-[:USES_ENV]->(e:EnvVariable) WHERE m.fqn = $fqn "
                        "RETURN e.name AS name LIMIT 10",
                        {"fqn": ctrl_action},
                    )
                    env_keys.extend(r.get("name", "") for r in envs)
                except Exception:
                    pass
            if config_keys or env_keys:
                lines.append("### Config/Env Dependencies\n")
                for k in sorted(set(config_keys))[:8]:
                    lines.append(f"- `config('{k}')`")
                for k in sorted(set(env_keys))[:8]:
                    lines.append(f"- `env('{k}')`")
                lines.append("")

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_feature_context", {"feature": feature}, symbol_count, elapsed)

        if symbol_count == 0 and not matched_routes:
            return (
                f"No components found for feature '{feature}'.\n\n"
                f"Try: laravelgraph_query('{feature}') to search for related symbols, "
                f"or laravelgraph_routes() to browse all routes."
            )

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_explain(feature) for a narrative end-to-end explanation",
            "Use laravelgraph_context(ClassName) for deep-dive on any specific symbol",
            "Use laravelgraph_impact(ClassName) to see blast radius before making changes",
        )

    # ── Tool: laravelgraph_cypher ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_cypher(query: str) -> str:
        """Execute a read-only Cypher query against the knowledge graph.

        Only MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT are allowed. No mutations.

        Args:
            query: Cypher query string
        """
        # Security: reject any mutation keywords
        forbidden = ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "DROP", "DETACH"]
        q_upper = query.upper()
        for kw in forbidden:
            if kw in q_upper:
                return f"❌ Mutation keyword '{kw}' is not allowed. Only read-only queries permitted."

        db = _db()
        start = time.perf_counter()

        try:
            results = db.execute(query)
        except Exception as e:
            return f"Query error: {e}\n\nUse laravelgraph://schema to see available node/relationship types."

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_cypher", {"query": query[:100]}, len(results), elapsed)

        if not results:
            return "Query returned no results."

        lines = [f"## Cypher Query Results ({len(results)} rows)\n"]
        if results:
            headers = list(results[0].keys())
            lines.append("| " + " | ".join(headers) + " |")
            lines.append("|" + "|".join(["---"] * len(headers)) + "|")
            for row in results[:50]:
                values = [str(row.get(h, ""))[:60] for h in headers]
                lines.append("| " + " | ".join(values) + " |")
            if len(results) > 50:
                lines.append(f"\n_...{len(results) - 50} more rows_")

        return "\n".join(lines)

    # ── Tool: laravelgraph_list_repos ─────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_list_repos() -> str:
        """List all indexed repositories with their stats."""
        from laravelgraph.core.registry import Registry
        import datetime
        import re as _re

        _TEMP_PATH_RE = _re.compile(
            r"[/\\]pytest-\d+[/\\]|[/\\]var[/\\]folders[/\\]|[/\\]T[/\\]pytest|"
            r"[/\\]tmp[/\\]|\\Temp\\|/__pycache__/"
        )

        registry = Registry()
        all_repos = registry.all()

        # Filter out pytest/CI temp paths and deduplicate by canonical path
        seen_paths: set[str] = set()
        repos = []
        for repo in sorted(all_repos, key=lambda r: -r.indexed_at):  # newest first
            if _TEMP_PATH_RE.search(repo.path):
                continue
            if repo.path in seen_paths:
                continue
            seen_paths.add(repo.path)
            repos.append(repo)

        if not repos:
            return "No repositories indexed yet. Run: `laravelgraph analyze /path/to/laravel-project`"

        lines = ["## Indexed Repositories\n"]
        for repo in repos:
            indexed_at = datetime.datetime.fromtimestamp(repo.indexed_at).strftime("%Y-%m-%d %H:%M")
            lines.append(f"### `{repo.name}`")
            lines.append(f"- **Path:** {repo.path}")
            lines.append(f"- **Laravel:** {repo.laravel_version}")
            lines.append(f"- **PHP:** {repo.php_version}")
            lines.append(f"- **Indexed:** {indexed_at}")
            if repo.stats:
                # Show key stats, skip internal counters
                _SKIP = {"scheduler_disabled", "scheduler_commented_tasks"}
                top_stats = [(k, v) for k, v in repo.stats.items() if k not in _SKIP][:8]
                lines.append("- **Stats:** " + ", ".join(f"{k}: {v}" for k, v in top_stats))
                if repo.stats.get("scheduler_disabled"):
                    n = repo.stats.get("scheduler_commented_tasks", "?")
                    lines.append(f"- **⚠ Scheduler disabled** — {n} task(s) commented out in Kernel.php")
            lines.append("")

        return "\n".join(lines)

    # ── MCP Resources ─────────────────────────────────────────────────────────

    @mcp.resource("laravelgraph://overview")
    def resource_overview() -> str:
        """Node and edge counts by type — overview of what's in the graph."""
        try:
            db = _db()
            stats = db.stats()
        except Exception as e:
            return f"Index not available: {e}"

        lines = ["# LaravelGraph Index Overview\n"]
        total = sum(stats.values())
        lines.append(f"**Total nodes:** {total}\n")
        lines.append("## Node Counts by Type\n")
        for label, count in sorted(stats.items(), key=lambda x: -x[1]):
            lines.append(f"- **{label}:** {count:,}")

        return "\n".join(lines)

    @mcp.resource("laravelgraph://schema")
    def resource_schema() -> str:
        """Graph schema reference — all node and relationship types."""
        from laravelgraph.core.schema import NODE_TYPES, REL_TYPES

        lines = ["# LaravelGraph Graph Schema\n"]
        lines.append("## Node Types\n")
        for label, props in NODE_TYPES:
            prop_names = ", ".join(p[0] for p in props[:5])
            if len(props) > 5:
                prop_names += f" ... +{len(props) - 5} more"
            lines.append(f"- **{label}** — {prop_names}")

        lines.append("\n## Relationship Types\n")
        for entry in REL_TYPES:
            label = entry[0]
            props = entry[2] if len(entry) >= 3 else (entry[1] if len(entry) >= 2 else [])
            prop_names = ", ".join(p[0] for p in props) if props else "none"
            lines.append(f"- **{label}** — props: {prop_names}")

        return "\n".join(lines)

    # ── Tool: laravelgraph_provider_status ───────────────────────────────────

    @mcp.tool()
    def laravelgraph_provider_status() -> str:
        """Show which LLM providers are configured for semantic summary generation.

        Returns the active provider, which API keys are set (not the keys themselves),
        which are missing, and which environment variables to set for each provider.
        Summaries are generated lazily on first query and cached — no cost until used.
        """
        from laravelgraph.mcp.summarize import PROVIDER_REGISTRY, provider_status

        status = provider_status(cfg.summary)
        lines = ["# LLM Provider Status\n"]

        if not status["enabled"]:
            lines.append("**Semantic summaries are disabled** (`summary.enabled = false` in config)\n")
            return "\n".join(lines)

        active = status["active_provider"]
        if active:
            label = PROVIDER_REGISTRY[active]["label"]
            lines.append(f"**Active provider:** `{active}` — {label} ✓\n")
        else:
            lines.append("**No provider configured** — summaries skipped (tool still works fine)\n")
            lines.append("Run `laravelgraph configure` to set one up.\n")

        # Cloud providers
        lines.append("## Cloud Providers\n")
        for name, info in status["providers"].items():
            if info["local"]:
                continue
            configured = info["configured"]
            icon = "✓" if configured else "—"
            active_marker = " ← **active**" if name == active else ""
            lines.append(f"- **{PROVIDER_REGISTRY[name]['label']}** (`{name}`) {icon}{active_marker}")
            if configured:
                lines.append(f"  model: `{info['model']}`")
            elif info.get("env_var"):
                lines.append(f"  env var: `{info['env_var']}`")

        # Local providers
        lines.append("\n## Local Providers\n")
        for name, info in status["providers"].items():
            if not info["local"]:
                continue
            configured = info["configured"]
            icon = "✓" if configured else "—"
            active_marker = " ← **active**" if name == active else ""
            lines.append(f"- **{PROVIDER_REGISTRY[name]['label']}** (`{name}`) {icon}{active_marker}")
            if configured:
                lines.append(f"  model: `{info['model']}` | url: `{info['base_url']}`")
            else:
                lines.append(f"  default url: `{PROVIDER_REGISTRY[name]['base_url']}`")

        cache_stats = _summary_cache.stats()
        lines.append(f"\n## Summary Cache\n")
        lines.append(f"- **Cached summaries:** {cache_stats['cached_summaries']}")
        if cache_stats.get("models_used"):
            lines.append(f"- **Providers used:** {', '.join(cache_stats['models_used'])}")

        return "\n".join(lines)

    @mcp.resource("laravelgraph://summaries")
    def resource_summaries() -> str:
        """Semantic summary cache stats — how many symbols have cached summaries."""
        stats = _summary_cache.stats()
        lines = ["# Semantic Summary Cache\n"]
        lines.append(f"**Cached summaries:** {stats['cached_summaries']}")
        if stats.get("models_used"):
            lines.append(f"**Models used:** {', '.join(stats['models_used'])}")
        lines.append(
            "\nSummaries are generated lazily on first explain/context call and cached "
            "in `.laravelgraph/summaries.json`. Auto-invalidated when source files change."
        )
        return "\n".join(lines)

    @mcp.resource("laravelgraph://providers")
    def resource_providers() -> str:
        """LLM provider configuration — which API keys are set and which model each uses."""
        try:
            return laravelgraph_provider_status()
        except Exception as e:
            return f"Error: {e}"

    @mcp.resource("laravelgraph://routes")
    def resource_routes() -> str:
        """Route table — all HTTP routes with their controllers and middleware."""
        try:
            return laravelgraph_routes(limit=100)
        except Exception as e:
            return f"Error: {e}"

    @mcp.resource("laravelgraph://models")
    def resource_models() -> str:
        """Eloquent model relationship map."""
        try:
            return laravelgraph_models()
        except Exception as e:
            return f"Error: {e}"

    @mcp.resource("laravelgraph://events")
    def resource_events() -> str:
        """Event → listener → job dispatch map."""
        try:
            return laravelgraph_events()
        except Exception as e:
            return f"Error: {e}"

    @mcp.resource("laravelgraph://dead-code")
    def resource_dead_code() -> str:
        """Full dead code report."""
        try:
            return laravelgraph_dead_code()
        except Exception as e:
            return f"Error: {e}"

    @mcp.resource("laravelgraph://bindings")
    def resource_bindings() -> str:
        """Service container binding map."""
        try:
            return laravelgraph_bindings()
        except Exception as e:
            return f"Error: {e}"

    return mcp


# ── Helper: symbol resolution ─────────────────────────────────────────────────

def _normalize_node(row: dict) -> dict:
    """Strip 'n.' prefix from keys returned by RETURN n.* queries in KuzuDB."""
    return {(k[2:] if k.startswith("n.") else k): v for k, v in row.items()}


def _resolve_symbol(db: GraphDB, symbol: str) -> dict | None:
    """Try multiple strategies to find a node matching the symbol string.

    KuzuDB requires labeled MATCH patterns — MATCH (n) without a label
    returns no results. We search each relevant node table explicitly.
    """
    # Labels that have node_id — derived from schema to stay in sync
    from laravelgraph.core.schema import NODE_TYPES as _NODE_TYPES
    _node_id_labels = [label for label, _ in _NODE_TYPES]
    # Labels that have fqn
    _fqn_labels = ["Class_", "Method", "Function_", "Interface_", "Trait_"]

    # 1. Exact node_id match across all node tables
    for label in _node_id_labels:
        try:
            results = db.execute(
                f"MATCH (n:{label}) WHERE n.node_id = $s RETURN n.*, '{label}' AS _label LIMIT 1",
                {"s": symbol},
            )
            if results:
                return _normalize_node(results[0])
        except Exception:
            continue

    # 2. Exact FQN match
    for label in _fqn_labels:
        try:
            results = db.execute(
                f"MATCH (n:{label}) WHERE n.fqn = $s RETURN n.*, '{label}' AS _label LIMIT 1",
                {"s": symbol},
            )
            if results:
                return _normalize_node(results[0])
        except Exception:
            continue

    # 3. Class name match — with disambiguation when multiple classes share a name.
    # Prefer canonical app paths over sub-namespaces (e.g. App\Http\Controllers\BookingController
    # over App\Http\Controllers\Reseller\BookingController).
    try:
        results = db.execute(
            "MATCH (n:Class_) WHERE n.name = $s RETURN n.*, 'Class_' AS _label LIMIT 10",
            {"s": symbol},
        )
        if results:
            if len(results) == 1:
                return _normalize_node(results[0])
            # Multiple matches — rank by FQN depth (fewer segments = more canonical)
            # and by preferred path prefixes.
            def _class_rank(row: dict) -> tuple:
                fqn = row.get("n.fqn", "") or ""
                fp  = row.get("n.file_path", "") or ""
                # Prefer paths in app/Http/Controllers/ directly (not sub-dirs)
                canonical = (
                    ("Http\\Controllers\\" in fqn and fqn.count("\\") <= fqn.index("Controllers\\") // 1 + 4)
                    or "/Http/Controllers/" in fp
                )
                depth = fqn.count("\\")
                return (0 if canonical else 1, depth)
            results.sort(key=_class_rank)
            return _normalize_node(results[0])
    except Exception:
        pass

    # 4. Method name match
    try:
        results = db.execute(
            "MATCH (n:Method) WHERE n.name = $s RETURN n.*, 'Method' AS _label LIMIT 1",
            {"s": symbol},
        )
        if results:
            return _normalize_node(results[0])
    except Exception:
        pass

    # 5. FQN contains (partial match) — with disambiguation: prefer shorter FQN.
    for label in _fqn_labels:
        try:
            results = db.execute(
                f"MATCH (n:{label}) WHERE n.fqn CONTAINS $s RETURN n.*, '{label}' AS _label LIMIT 10",
                {"s": symbol},
            )
            if results:
                results.sort(key=lambda r: len(r.get("n.fqn", "") or ""))
                return _normalize_node(results[0])
        except Exception:
            continue

    return None


# ── Server startup helpers ────────────────────────────────────────────────────

def run_stdio(project_root: Path, config: Config | None = None) -> None:
    """Run MCP server over stdio transport."""
    mcp = create_server(project_root, config)
    mcp.run(transport="stdio")


def run_http(
    project_root: Path,
    host: str = "127.0.0.1",
    port: int = 3000,
    config: Config | None = None,
    api_key: str = "",
) -> None:
    """Run MCP server over HTTP/SSE transport.

    If api_key is set, all requests must include:
        Authorization: Bearer <api_key>
    The /health endpoint is always publicly accessible (no auth required).
    """
    mcp = create_server(project_root, config)

    # ── /health endpoint — always public, used by EC2/load-balancer health checks ──
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "project": str(project_root)})

    # ── API key middleware ────────────────────────────────────────────────────
    middleware = []
    if api_key:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import Response

        class _BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Health check is always public
                if request.url.path == "/health":
                    return await call_next(request)
                auth_header = request.headers.get("Authorization", "")
                if auth_header == f"Bearer {api_key}":
                    return await call_next(request)
                return Response(
                    content='{"error":"Unauthorized — valid Bearer token required"}',
                    status_code=401,
                    media_type="application/json",
                )

        from starlette.middleware import Middleware
        middleware = [Middleware(_BearerAuthMiddleware)]

    mcp.run(
        transport="sse",
        host=host,
        port=port,
        show_banner=False,
        middleware=middleware,
    )
