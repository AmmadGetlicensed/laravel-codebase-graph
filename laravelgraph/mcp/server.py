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

    # ── Scan plugins before server creation so we can inject them into instructions ──
    from laravelgraph.plugins.loader import scan_plugin_manifests as _scan_manifests
    _plugins_dir_early = project_root / ".laravelgraph" / "plugins"
    _plugin_manifests = _scan_manifests(_plugins_dir_early)

    # Pre-load discovery counts from plugin graph (best-effort, non-blocking).
    # NOTE: _plugin_db is created further below; we use a temporary connection
    # here only to read counts, then close it immediately to avoid holding two
    # open Database objects to the same path (KuzuDB write-lock conflict).
    _plugin_discovery_counts: dict[str, int] = {}
    try:
        from laravelgraph.plugins.plugin_graph import init_plugin_graph as _init_pg
        _pg_tmp = _init_pg(index_dir(project_root))
        for _m in _plugin_manifests:
            _plugin_discovery_counts[_m["name"]] = _pg_tmp.get_plugin_node_count(_m["name"])
        _pg_tmp.close()
        del _pg_tmp
    except Exception:
        pass

    def _build_loaded_plugins_section(manifests: list, discovery_counts: dict | None = None) -> str:
        if not manifests:
            return ""
        _counts = discovery_counts or {}
        lines = [
            "\n═══════════════════════════════════════════════════════════",
            "LOADED PLUGINS (active this conversation)",
            "═══════════════════════════════════════════════════════════",
            "",
            "These plugins were generated for THIS product's domain and are",
            "available RIGHT NOW — no restart, no discovery call needed.",
            "",
            "TWO ways to use them:",
            "  1. Native tool call  — e.g. usr_summary()  (registered at startup)",
            "  2. Hot dispatch      — laravelgraph_run_plugin_tool(plugin, tool, tool_args=None)",
            "     Use hot dispatch when you generated a plugin THIS conversation",
            "     and want to use it immediately without waiting for a restart.",
            "     Pass tool_args={'findings': '...'} to call store_discoveries hot.",
            "",
        ]
        for m in manifests:
            disc_count = _counts.get(m["name"], 0)
            disc_tag = f"  [{disc_count} discoveries]" if disc_count > 0 else ""
            lines.append(f"▸ {m['name']}  (prefix: {m['tool_prefix']}){disc_tag}")
            if m["description"]:
                lines.append(f"  \"{m['description']}\"")
            if m["tool_names"]:
                lines.append(f"  Tools: {', '.join(m['tool_names'])}")
                lines.append(f"  Hot:   laravelgraph_run_plugin_tool(\"{m['name']}\", \"<tool_name>\")  [tool_args={{...}} for store_discoveries]")
            if disc_count > 0:
                lines.append(f"  Recall: laravelgraph_plugin_knowledge(plugin_name=\"{m['name']}\")")
            lines.append("")
        lines.append("If a plugin was just generated this session, use hot dispatch above.")
        lines.append("Native tools are only visible after the next MCP server restart.")
        return "\n".join(lines)

    _loaded_plugins_section = _build_loaded_plugins_section(_plugin_manifests, _plugin_discovery_counts)

    mcp = FastMCP(
        name="LaravelGraph",
        version="0.2.0",
        instructions="""YOU HAVE ACCESS TO THE COMPLETE KNOWLEDGE GRAPH OF THIS PRODUCT.
This is not a search tool — this is the product's brain. It knows every class, method,
route, model, database table, column, stored procedure, event, job, and their relationships.
It sees live production data distributions. It traces code → DB → event → job chains.
It understands product features, behavioral contracts, API surfaces, and performance risks.

YOUR JOB: Use these tools as the SINGLE SOURCE OF TRUTH for any question about this product.
Do NOT read files manually when you can query the graph. Do NOT guess — query first.
When one tool returns sparse results, ALWAYS escalate to the next step. Never stop at empty.

When a result shows "AMBIGUOUS NAME" warning, it means multiple classes share that name.
Use the full FQN shown in the warning to target the exact class you need.

═══════════════════════════════════════════════════════════
HOW TO UNDERSTAND ANY FEATURE
═══════════════════════════════════════════════════════════

1 → laravelgraph_feature_context(feature="booking")
  ONE CALL: routes + controller source + models + events + jobs + views + config.
  If rich → done. If sparse → step 2.

2 → laravelgraph_explain(feature="booking flow")
  Semantic search finds the best anchor — may be a service class, not a route.
  If found → done. If need HTTP detail → step 3.

3 → laravelgraph_request_flow(route="/api/bookings")
  Controller → service → dependencies (3 hops), events/jobs at every level.

4 → laravelgraph_context(symbol="Booking::insertBooking", include_source=True)
  360° view: callers, callees, dispatches, Eloquent relationships, source code.
  Use include_source=True to see the actual PHP logic — switch maps, thresholds,
  hardcoded IDs, email recipients, cache locks — everything static analysis misses.

5 → laravelgraph_features(feature="booking")
  Auto-clustered Feature nodes: all routes, models, events, jobs grouped by URI segment.
  Use this when you need the PRODUCT BOUNDARY view — what belongs to a feature.
  Call laravelgraph_features() with no args to list all detected product features.

═══════════════════════════════════════════════════════════
HOW TO UNDERSTAND ANY DATABASE TABLE
═══════════════════════════════════════════════════════════

1 → laravelgraph_db_context(table="orders")
  Columns, FKs, inferred relations, code access, enum value distributions, annotation.

2 → laravelgraph_resolve_column(table="orders", column="status")
  Write-path evidence, guard conditions, polymorphic hints, live value sample.

3 → laravelgraph_db_query(sql="SELECT col, COUNT(*) cnt FROM table GROUP BY col ORDER BY cnt DESC")
  Live SQL — ALWAYS use this for real numbers. Code cannot tell you Gold plan = 75%.

4 → laravelgraph_db_impact(table="orders", operation="write")
  Write site → events dispatched → listeners → downstream jobs. Full chain.
  When 0 sites: auto-fallback searches linked Eloquent model methods.

═══════════════════════════════════════════════════════════
HOW TO TRACE ASYNC CHAINS (jobs, commands, schedulers)
═══════════════════════════════════════════════════════════

1 → laravelgraph_job_chain(job="MatchAndUploadCertificatesJob", depth=5)
  Job → Event → Listener → more Jobs (up to 8 hops). For non-HTTP flows.

2 → laravelgraph_context(symbol, include_source=True) when chain is empty.

3 → laravelgraph_events() for the full event → listener → job map.

═══════════════════════════════════════════════════════════
HOW TO UNDERSTAND STORED PROCEDURES & DB-LEVEL LOGIC
═══════════════════════════════════════════════════════════

1 → laravelgraph_connection_map()
  ALL procedure names with last-modified dates. Tells you what's active vs legacy.

2 → laravelgraph_procedure_context(name="sp_course_finder_events_cards_new")
  Parameters, body, tables read/written, annotation.

Stored procedures are called by the DB layer, not PHP. Zero PHP CALL statements is normal.

═══════════════════════════════════════════════════════════
HOW TO ASSESS CHANGE IMPACT
═══════════════════════════════════════════════════════════

1 → laravelgraph_impact(symbol) — BFS blast radius: callers + callees + DB tables + events.

2 → laravelgraph_context(symbol) if impact seems low — check if route handler or magic method.

3 → laravelgraph_suggest_tests(symbol) → which tests to run.

4 → laravelgraph_detect_changes(base="HEAD~5") → git diff → affected symbols.

5 → laravelgraph_test_coverage(symbol) → which TestCase nodes cover this route or class.
  If coverage is 0: the symbol has no known tests — flag as untested before merging.

═══════════════════════════════════════════════════════════
HOW TO AUDIT BEHAVIORAL CONTRACTS
═══════════════════════════════════════════════════════════

laravelgraph_contracts(symbol="OrderController")
  Returns all behavioral contracts that govern a class or route:
  • validation — FormRequest rules() extracted (field constraints, required/optional)
  • authorization — Policy method names and rules applied to the route
  • lifecycle — Observer hooks (creating, updating, deleting, saved, etc.)
  • mass_assignment — $fillable / $guarded arrays on Eloquent models

  Use contract_type="validation" to filter to one type.
  ALWAYS check contracts when reviewing a PR — they reveal implicit rules not in the method.

═══════════════════════════════════════════════════════════
HOW TO UNDERSTAND CODE INTENT (LAZY LLM)
═══════════════════════════════════════════════════════════

laravelgraph_intent(symbol="OrderService::processRefund")
  Returns structured intent extracted by LLM on first call, then cached forever:
  • purpose — one-sentence plain-English description
  • reads — list of data it reads (models, tables, config keys)
  • writes — list of data it mutates
  • side_effects — emails sent, jobs dispatched, external APIs called
  • guards — preconditions checked (auth, status checks, locks)

  Intent is generated LAZILY — zero LLM cost during analyze. Only runs when you call this tool.
  Cached in .laravelgraph/intent.json, auto-invalidated when the source file changes.

═══════════════════════════════════════════════════════════
HOW TO FIND PERFORMANCE RISKS
═══════════════════════════════════════════════════════════

laravelgraph_performance_risks()                    → all risks, sorted by severity
laravelgraph_performance_risks(severity="high")     → high-severity only
laravelgraph_performance_risks(symbol="OrderCtrl")  → risks in a specific class/method

Risk types detected:
  • n_plus_one        — foreach + relationship access without eager loading
  • missing_eager_load — relationship access inside loops
  • repeated_count    — ->count() called in a loop
  • raw_query_bypass  — DB::select() / DB::statement() bypassing Eloquent (no query logging)

═══════════════════════════════════════════════════════════
HOW TO AUDIT THE API SURFACE
═══════════════════════════════════════════════════════════

laravelgraph_api_surface()                    → all public routes with middleware, auth, contracts
laravelgraph_api_surface(route="/api/orders") → single-route deep audit
laravelgraph_api_surface(method="POST")       → filter by HTTP method

Shows for each route: middleware stack, auth guards, FormRequest validation rules, Policy,
bound Observer hooks, and which test files cover it.
Use this for security audits, API documentation, and onboarding.

═══════════════════════════════════════════════════════════
HOW TO EXPLORE & SEARCH
═══════════════════════════════════════════════════════════

laravelgraph_query(query="payment")        → Hybrid search (BM25 + semantic + fuzzy)
laravelgraph_routes(filter="booking")      → Route table with controllers & middleware
laravelgraph_models(model="Order")         → Eloquent relationships + linked DB table
laravelgraph_dead_code()                   → Unreachable code report
laravelgraph_bindings()                    → Service container bindings
laravelgraph_config_usage(key="APP_KEY")   → All code depending on a config/env value
laravelgraph_cypher(query="MATCH ...")     → Raw Cypher (labels use _ suffix: Class_, Function_)
laravelgraph_schema(table_name="orders")   → Full table schema
laravelgraph_provider_status()             → Which LLM provider generates annotations

═══════════════════════════════════════════════════════════
PLUGIN SYSTEM — EXTENDING THE GRAPH
═══════════════════════════════════════════════════════════

LaravelGraph supports project-specific plugins that add domain knowledge without
touching the core codebase. Plugins live in .laravelgraph/plugins/*.py.

laravelgraph_suggest_plugins()
  Runs domain-signal detection across 7 built-in recipes against the live graph.
  Recipes: payment-lifecycle, tenant-isolation, booking-state-machine,
           subscription-lifecycle, rbac-coverage, audit-trail, feature-flags.
  Returns ranked recommendations with evidence and scaffold commands.

PLUGIN MANAGEMENT TOOLS (use these to build, update, and prune plugins at runtime):

laravelgraph_request_plugin(description)
  Auto-generate a new MCP tool plugin from a plain-English description.
  Validates through 4 layers (AST + schema + execution + LLM judge).
  AFTER generation: use laravelgraph_run_plugin_tool() immediately this session.
  Native tool names also registered on the NEXT server start.

laravelgraph_run_plugin_tool(plugin_name, tool_name, tool_args=None)
  *** USE THIS IMMEDIATELY AFTER GENERATING A PLUGIN ***
  Dynamically loads any plugin from disk and calls the named tool — NO restart.
  Works for plugins generated this conversation AND plugins from previous sessions.
  tool_args: dict of kwargs for tools that take parameters (e.g. store_discoveries).
  Example: laravelgraph_run_plugin_tool("user-explorer", "usr_summary")
  Example: laravelgraph_run_plugin_tool("order-lifecycle", "order_flow")
  Example: laravelgraph_run_plugin_tool("webhook", "web_store_discoveries",
           {"findings": "POST /v1/payments/paypal-ipn has no auth or HMAC check"})
  To see available tool names: check LOADED PLUGINS section above, or call
  laravelgraph_suggest_plugins() which lists all installed plugins with their tools.

laravelgraph_update_plugin(name, critique)
  Regenerate an existing plugin with a specific critique of what's wrong.
  Replaces the plugin file immediately if validation passes.
  Use laravelgraph_run_plugin_tool() to test the updated plugin immediately.

laravelgraph_remove_plugin(name, reason)
  Remove a plugin that provides no benefit. Logs the reason to prevent
  auto-regeneration of the same unhelpful plugin in the future.

laravelgraph_plugin_knowledge([plugin_name])
  Return domain discoveries stored by plugins across ALL past sessions.
  Plugins accumulate institutional knowledge via store_discoveries() calls —
  findings, patterns, identified issues — that persist between conversations.
  Pass plugin_name to filter to a specific plugin, or omit to see everything.
  Example: laravelgraph_plugin_knowledge(plugin_name="user-explorer")
  Plugins with discoveries show [N discoveries] in the LOADED PLUGINS section.

PLUGIN WORKFLOW:
  1. laravelgraph_suggest_plugins()             → see what plugins exist + what's recommended
  2. laravelgraph_request_plugin(description)   → generate plugin from description
  3. laravelgraph_run_plugin_tool(name, tool)   → USE IT IMMEDIATELY, same conversation
  4. laravelgraph_update_plugin(name, critique) → improve if output is wrong/shallow
  5. laravelgraph_run_plugin_tool(name, tool)   → verify the improvement immediately
  6. Next conversation: native tools registered automatically, shown in LOADED PLUGINS

  If you already see the plugin in LOADED PLUGINS above:
    • Call its native tool directly (e.g. usr_summary()) — fastest path
    • OR use laravelgraph_run_plugin_tool() — works the same way

PLUGIN SAFETY RULES (enforced, cannot be bypassed):
  • tool_prefix must NOT start with "laravelgraph_" (reserved namespace)
  • Plugins cannot DELETE, DROP, or TRUNCATE graph data — read + write only
  • Network access (requests, httpx, urllib) is blocked
  • All nodes written by a plugin are tagged with plugin_source automatically
  • tool_prefix is validated at registration time — mismatch = plugin rejected

═══════════════════════════════════════════════════════════
MANDATORY RULES
═══════════════════════════════════════════════════════════

1. QUERY BEFORE GUESSING. This graph knows more than you can infer from file names.
2. COMBINE CODE + DATA. Example: feature_context → db_context → db_query → context(source=True).
   Code tells you the logic. Data tells you the reality. Both are required for truth.
3. USE FULL FQN when context() warns about ambiguous names. The tool ALWAYS warns you.
4. NEVER stop at empty results. Escalate: feature_context → explain → context(include_source).
5. Model→table names are UNRELIABLE. ALWAYS verify via laravelgraph_models or laravelgraph_db_context.
6. For business logic details (email recipients, capacity thresholds, cache lock durations,
   hardcoded IDs): use laravelgraph_context(symbol, include_source=True). The graph auto-extracts
   switch/match maps. For everything else, the source is the final authority.
7. Live data distributions reveal what code CANNOT: which plans are actually used (Gold=75%),
   which gateways handle real volume, which columns are functionally dead (value=0 across
   all rows). ALWAYS check laravelgraph_db_query for the real numbers.
8. BEHAVIORAL CONTRACTS are invisible without the graph. Always check laravelgraph_contracts
   before reviewing or modifying a route — FormRequest rules, Policies, and Observer hooks
   may enforce business logic that is not obvious from the controller code.
9. PERFORMANCE RISKS are pre-computed. Run laravelgraph_performance_risks() early in any
   refactor — N+1 patterns are the most common source of production slowdowns in Laravel.
10. INTENT is lazy. laravelgraph_intent() costs one LLM call per symbol but is then cached.
    Use it when you need a concise human-readable explanation of what a method actually does.
"""
        + _loaded_plugins_section,
    )

    # Lazy semantic summary cache — stored in .laravelgraph/summaries.json
    _summary_cache = SummaryCache(index_dir(project_root))

    # Lazy DB context cache — stored in .laravelgraph/db_context.json
    _db_cache = DBContextCache(index_dir(project_root))

    # TTL-based query result cache — stored in .laravelgraph/query_cache.json
    _query_cache = QueryResultCache(index_dir(project_root))

    # Plugin graph and meta store — initialized early so all tools can access them
    from laravelgraph.plugins.plugin_graph import init_plugin_graph
    from laravelgraph.plugins.meta import PluginMetaStore
    _plugin_db = init_plugin_graph(index_dir(project_root))
    _meta_store = PluginMetaStore(index_dir(project_root))

    # Evict expired query cache entries on startup — cheap disk cleanup so
    # stale results from previous sessions don't accumulate indefinitely.
    _expired_on_startup = _query_cache.evict_expired()
    if _expired_on_startup:
        logger.info("Query cache: evicted expired entries on startup", count=_expired_on_startup)

    _db_path = index_dir(project_root) / "graph.kuzu"

    def _db() -> GraphDB:
        """Open a fresh DB connection for this request.

        KuzuDB holds its write lock for the lifetime of the connection object.
        By opening a new connection per tool call and closing it when done,
        we never hold a persistent lock — so `laravelgraph analyze` can always
        acquire the write lock regardless of whether the MCP server is running.
        Plugin tools retain full read/write capability since the connection is
        opened without read_only restriction.
        """
        if not _db_path.exists():
            raise ValueError(
                f"No index found at {project_root}. Run: laravelgraph analyze {project_root}"
            )
        return GraphDB(_db_path)

    def _sql_db():
        """Return a live pymysql connection to the first configured database.

        Plugins that declare ``sql_db=None`` in ``register_tools`` receive this
        factory so they can run raw SQL queries alongside Cypher graph queries.
        Returns None if no databases are configured or pymysql is unavailable.
        """
        db_configs = cfg.databases if hasattr(cfg, "databases") else []
        if not db_configs:
            return None
        try:
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
            return _connect_mysql(db_configs[0])
        except Exception:
            return None

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

    def _error_response(severity: str, message: str) -> str:
        icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(severity, "⚪")
        return f"# {icon} Error ({severity})\n\n{message}"

    def _with_confidence(
        level: str,
        reason: str,
        gaps: list[str] | None = None,
    ) -> str:
        tag = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(level, "⚪")
        result = f"\n\n---\n**Confidence:** {tag} **{level}** — {reason}"
        if gaps:
            result += "\n**Coverage gaps:**\n" + "\n".join(f"- {g}" for g in gaps)
        return result

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
        for c in (cfg.databases if hasattr(cfg, "databases") else []):
            if c.name == conn_name:
                return c
        return None

    def _extract_switch_map(source: str) -> list[tuple[str, str]] | None:
        import re

        switch_match = re.search(r"\bswitch\s*\([^)]+\)\s*\{", source, re.MULTILINE)
        if not switch_match:
            return None

        switch_body = source[switch_match.end():]
        brace_depth = 1
        end_pos = 0
        for i, ch in enumerate(switch_body):
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0:
                    end_pos = i
                    break
        switch_body = switch_body[:end_pos]

        case_blocks = re.split(r"\bcase\b", switch_body)
        result: list[tuple[str, str]] = []
        for block in case_blocks[1:]:
            key_m = re.search(r"^\s*(?:(\d+)|'([^']+)'|\"([^\"]+)\")\s*:", block)
            if not key_m:
                continue
            case_key = key_m.group(1) or key_m.group(2) or key_m.group(3) or ""
            if not case_key:
                continue

            prop_m = re.search(r"return\s+\$\w+->(\w+)", block)
            str_m = re.search(r"return\s+'([^']+)'", block) or re.search(r'return\s+"([^"]+)"', block)
            if prop_m:
                result.append((case_key, prop_m.group(1)))
            elif str_m:
                result.append((case_key, f'"{str_m.group(1)}"'))

        return result if len(result) >= 2 else None

    def _fetch_varchar_sample(
        table: str, column: str, conn_cfg: Any, max_distinct: int = 30
    ) -> tuple[list[dict], bool] | None:
        safe_table = table.replace("`", "")
        safe_col = column.replace("`", "")
        fetch_limit = max_distinct + 1
        sql = (
            f"SELECT `{safe_col}` AS val, COUNT(*) AS cnt "
            f"FROM `{safe_table}` "
            f"WHERE `{safe_col}` IS NOT NULL AND `{safe_col}` != '' "
            f"GROUP BY `{safe_col}` "
            f"ORDER BY cnt DESC "
            f"LIMIT {fetch_limit}"
        )
        ttl = getattr(conn_cfg, "query_cache_ttl", 300)
        key = _query_cache.make_key(conn_cfg.name, sql)

        cached = _query_cache.get(key, ttl=ttl)
        if cached is not None:
            rows = cached.get("rows", [])
            overflow = len(rows) >= fetch_limit
            return rows[:max_distinct], overflow

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
            overflow = len(rows_data) >= fetch_limit
            return rows_data[:max_distinct], overflow
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

        if node.get("_disambiguation_warning"):
            lines.append(f"> **⚠ AMBIGUOUS NAME:** {node['_disambiguation_warning']}")
            all_m = node.get("_all_matches", [])
            if all_m:
                lines.append(">")
                lines.append("> | FQN | Methods | Routes | File |")
                lines.append("> |-----|---------|--------|------|")
                for am in all_m:
                    lines.append(
                        f"> | `{am.get('fqn', '')}` | {am.get('methods_count', '?')} "
                        f"| {am.get('routes_count', '?')} | {am.get('file', '')!s:.60} |"
                    )
            lines.append("")

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

        if fp and ls and label in ("Method", "Function_"):
            try:
                from laravelgraph.mcp.explain import read_source_snippet
                src_for_map = read_source_snippet(fp, ls, le, project_root)
                if src_for_map:
                    switch_map = _extract_switch_map(src_for_map)
                    if switch_map:
                        lines.append("\n### Switch/Match Map (extracted)\n")
                        lines.append("| Key | Maps To |")
                        lines.append("|-----|---------|")
                        for k, v in switch_map:
                            lines.append(f"| `{k}` | `{v}` |")
                        lines.append("")
            except Exception:
                pass

        lines.append("")

        # ── Generate and cache summary if we have source and API key ────────
        if not cached_summary and fp and ls and cfg.llm.enabled:
            from laravelgraph.mcp.explain import read_source_snippet
            source_text = read_source_snippet(fp, ls, le, project_root)
            if source_text:
                node_type = label.lower().replace("_", " ").replace("eloquentmodel", "Eloquent model")
                summary, provider_used = generate_summary(
                    fqn=fqn,
                    node_type=node_type,
                    source=source_text,
                    docblock=raw_doc,
                    summary_cfg=cfg.llm,
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
                "RETURN caller.fqn AS caller_fqn, r.confidence AS conf LIMIT 30",
                {"id": node_id},
            )
            if callers:
                own_class = fqn.rsplit("::", 1)[0] if "::" in fqn else fqn
                external = [c for c in callers if not (c.get("caller_fqn") or "").startswith(own_class + "::")]
                if external:
                    lines.append(f"### Callers ({len(external)})")
                    for c in external:
                        conf = c.get("conf")
                        conf_str = f" (conf: {conf:.2f})" if conf is not None else ""
                        lines.append(f"- `{c.get('caller_fqn', '?')}`{conf_str}")
                else:
                    lines.append("### Callers")
                    lines.append("No external callers detected — may be invoked via route registration or service container.")
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

        has_warning = bool(node.get("_disambiguation_warning"))
        ctx_conf = "MEDIUM" if has_warning else "HIGH"
        ctx_reason = (
            "Ambiguous name — multiple classes matched, showing best guess"
            if has_warning else "Exact symbol match from indexed graph"
        )

        return "\n".join(lines) + _with_confidence(ctx_conf, ctx_reason) + _next_steps(
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
                    "RETURN r.http_method AS method, r.uri AS uri, r.name AS rname LIMIT 5",
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

        conf_level = "HIGH" if impact.total > 0 else "LOW"
        conf_reason = (
            f"BFS traversal found {impact.total} affected symbols across callers, callees, DB tables, and dispatches"
            if impact.total > 0
            else "Zero impact detected — may indicate incomplete call graph indexing or a dynamic entry point"
        )
        conf_gaps = []
        if impact.total == 0:
            conf_gaps.append("Dynamic dispatch ($this->$method()) not traceable by static analysis")
            conf_gaps.append("String-based route registration may not create CALLS edges")

        return "\n".join(lines) + _with_confidence(conf_level, conf_reason, conf_gaps) + _next_steps(
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

        eloquent_live: list[dict] = []
        try:
            eloquent_live = db.execute(
                "MATCH (c:Class_ {laravel_role: 'model'})-[:DEFINES]->(m:Method {is_dead_code: false}) "
                "WHERE m.laravel_role IN ['accessor', 'mutator', 'scope', 'relationship'] "
                "OR m.name STARTS WITH 'get' AND m.name ENDS WITH 'Attribute' "
                "OR m.name STARTS WITH 'set' AND m.name ENDS WITH 'Attribute' "
                "OR m.name STARTS WITH 'scope' "
                "RETURN m.fqn AS fqn, m.name AS name, m.laravel_role AS role, "
                "c.name AS model_name LIMIT 100"
            )
        except Exception:
            pass

        if not all_dead:
            lines = ["✅ No dead code detected.\n"]
        else:
            lines = [f"## Dead Code Report ({len(all_dead)} unreachable symbols)\n"]

            by_file: dict[str, list] = {}
            for d in all_dead:
                f = d.get("file", "unknown")
                by_file.setdefault(f, []).append(d)

            for file_path, symbols in sorted(by_file.items())[:30]:
                lines.append(f"### `{file_path}`")
                for s in symbols:
                    lines.append(f"- **DEAD** Line {s.get('line', '?')}: `{s.get('fqn', '?')}` ({s.get('_type', '?')})")
                lines.append("")

        if eloquent_live:
            lines.append(f"\n## Eloquent-Dynamic Methods ({len(eloquent_live)} — status: LIVE)\n")
            lines.append("These methods have no direct PHP callers but are invoked via Eloquent magic:")
            lines.append("")
            for el in eloquent_live[:50]:
                role_tag = el.get("role") or "relationship"
                lines.append(
                    f"- **LIVE** `{el.get('fqn', '?')}` — {role_tag} on {el.get('model_name', '?')} "
                    f"(evidence: eloquent_dynamic, confidence: HIGH)"
                )
            lines.append("")

        return "\n".join(lines) + _with_confidence(
            "MEDIUM",
            "Static call graph analysis — exempts Eloquent relationships, route handlers, accessors/mutators/scopes",
            ["String-based dynamic dispatch (app()->make(), resolve()) not traced",
             "Event subscriber methods registered in $subscribe property may be missed"],
        ) + _next_steps(
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

        if not annotation and cfg.llm.enabled:
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
                summary_cfg=cfg.llm,
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
                varchar_result = _fetch_varchar_sample(table, column, resolve_conn_cfg)
                if varchar_result is not None:
                    varchar_rows, varchar_overflow = varchar_result
                    if varchar_rows:
                        header = "### Value Sample (live DB — top values by frequency)"
                        if varchar_overflow:
                            header += " *(more values exist — use laravelgraph_db_query for full distribution)*"
                        lines.append(f"\n{header}\n")
                        lines.append("| Value | Count |")
                        lines.append("|-------|-------|")
                        for srow in varchar_rows:
                            lines.append(f"| `{srow.get('val')}` | {srow.get('cnt'):,} |")

        # ── Lazy LLM resolution ───────────────────────────────────────────────
        schema_sig = f"{full_t}:{col_data.get('polymorphic_candidate', False)}:{wpe_raw}"
        col_hash = hashlib.sha1(schema_sig.encode()).hexdigest()[:12]
        cache_key = f"dbctx:column:{conn_name}:{table}.{column}"
        annotation = _db_cache.get(cache_key, current_hash=col_hash)

        if not annotation and cfg.llm.enabled:
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
                summary_cfg=cfg.llm,
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

        if not annotation and cfg.llm.enabled:
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
                summary_cfg=cfg.llm,
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
            proc_rows = db.execute(
                "MATCH (p:StoredProcedure) "
                "RETURN p.name AS name, p.connection AS conn, p.last_altered AS modified "
                "ORDER BY p.connection, p.name"
            )
            if proc_rows:
                lines.append("### Stored Procedures\n")
                by_conn: dict[str, list[dict]] = {}
                for row in proc_rows:
                    conn_label = row.get("conn") or "unknown"
                    by_conn.setdefault(conn_label, []).append({
                        "name": row.get("name") or "?",
                        "modified": row.get("modified") or "",
                    })
                for conn_label, procs in sorted(by_conn.items()):
                    lines.append(f"**`{conn_label}`** ({len(procs)} procedures)")
                    has_dates = any(p["modified"] for p in procs)
                    if has_dates:
                        lines.append("| Procedure | Last Modified |")
                        lines.append("|-----------|---------------|")
                        for p in procs:
                            mod = p["modified"][:10] if p["modified"] else "—"
                            lines.append(f"| `{p['name']}` | {mod} |")
                    else:
                        lines.append("| Procedure |")
                        lines.append("|-----------|")
                        for p in procs:
                            lines.append(f"| `{p['name']}` |")
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

    def _db_impact_model_fallback(
        db: GraphDB, table: str, operation: str
    ) -> list[dict]:
        try:
            model_rows = db.execute(
                "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) "
                "WHERE t.name = $tname RETURN m.fqn AS mfqn LIMIT 3",
                {"tname": table},
            )
        except Exception:
            return []
        if not model_rows:
            return []

        results = []
        for mr in model_rows:
            mfqn = mr.get("mfqn", "")
            if not mfqn:
                continue
            try:
                methods = db.execute(
                    "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
                    "WHERE c.fqn = $fqn "
                    "RETURN m.fqn AS src_fqn, m.name AS src_name, 'write' AS op, "
                    "'eloquent_model' AS via, m.line_start AS line",
                    {"fqn": mfqn},
                )
                for row in methods:
                    results.append(row)
            except Exception:
                pass
        return results[:30]

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
            fallback_sites = _db_impact_model_fallback(db, table, operation)
            if fallback_sites:
                sites = fallback_sites
            else:
                op_label = "access" if operation == "any" else operation
                model_hint = ""
                try:
                    model_rows = db.execute(
                        "MATCH (m:EloquentModel)-[:USES_TABLE]->(t:DatabaseTable) "
                        "WHERE t.name = $tname RETURN m.name AS mname, m.fqn AS mfqn LIMIT 3",
                        {"tname": table},
                    )
                    if model_rows:
                        names = [r.get("mfqn", r.get("mname", "")) for r in model_rows]
                        model_hint = (
                            f"\n\nLinked Eloquent models: {', '.join(f'`{n}`' for n in names)}. "
                            "Try laravelgraph_context(model, include_source=True) to read the "
                            "model's methods that write to this table, or "
                            "laravelgraph_db_query(sql=\"SELECT ...\") for live data."
                        )
                except Exception:
                    pass
                return (
                    f"No indexed `{op_label}` sites for table `{table}`. "
                    "The table may be written via $instance->save(), relationship methods, "
                    "or raw SQL not captured by static analysis."
                    + model_hint
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

        has_dynamic = any(s.get("via") == "eloquent_model" for s in sites)
        conf_gaps = []
        if has_dynamic:
            conf_gaps.append("Some write sites from Eloquent model fallback — may include non-write methods")
        conf_level = "MEDIUM" if has_dynamic else "HIGH"
        conf_reason = "Write-path index covers static calls, query builder, and Eloquent instance writes"
        if has_dynamic:
            conf_reason = "Includes fallback model method scan — verify specific write methods in source"

        return "\n".join(lines) + _with_confidence(conf_level, conf_reason, conf_gaps) + _next_steps(
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
            "Use laravelgraph_job_chain(job) to trace the full execution chain from any Job entry point",
        )

    @mcp.tool()
    def laravelgraph_job_chain(job: str, depth: int = 5) -> str:
        """Trace the full execution chain from a Job or Artisan Command entry point.

        Walks: Job/Command → dispatched Events → Listeners → further dispatched Jobs/Events,
        up to `depth` hops. Reveals multi-level chains that laravelgraph_request_flow misses
        because they originate outside the HTTP layer (queued jobs, scheduled commands, etc.).

        Args:
            job: Job class name, FQN, or Artisan command name (e.g. "MatchAndUploadCertificatesJob")
            depth: Max hops to trace (default 5, max 8)
        """
        start = time.perf_counter()
        db = _db()
        depth = min(depth, 8)

        node = _resolve_symbol(db, job)
        if not node:
            return f"Symbol '{job}' not found. Try laravelgraph_query('{job}')."

        root_nid = node.get("node_id", "")
        root_fqn = node.get("fqn", node.get("name", job))
        root_label = node.get("_label", "?")

        lines = [f"## Job Chain: `{root_fqn}`\n"]
        lines.append(f"Entry point type: **{root_label}**\n")

        visited: set[str] = {root_nid}
        queue: list[tuple[str, str, int, str]] = [(root_nid, root_fqn, 0, "")]
        chain_lines: list[str] = []

        while queue:
            cur_nid, cur_fqn, cur_depth, via = queue.pop(0)
            if cur_depth >= depth:
                continue

            indent = "  " * cur_depth

            if cur_depth == 0:
                chain_lines.append(f"**`{cur_fqn}`** ← entry point")
            else:
                chain_lines.append(f"{indent}↳ `{cur_fqn}` (via {via})")

            try:
                method_rows = db.execute(
                    "MATCH (c)-[:DEFINES]->(m:Method) WHERE c.node_id = $nid "
                    "RETURN m.node_id AS mnid, m.name AS mname, m.fqn AS mfqn",
                    {"nid": cur_nid},
                )
            except Exception:
                method_rows = []

            for mrow in method_rows:
                mnid = mrow.get("mnid", "")
                if not mnid or mnid in visited:
                    continue

                try:
                    dispatched = db.execute(
                        "MATCH (m:Method)-[d:DISPATCHES]->(t) WHERE m.node_id = $mnid "
                        "RETURN t.node_id AS tnid, t.fqn AS tfqn, t.name AS tname, "
                        "labels(t)[0] AS tlabel, d.dispatch_type AS dtype, d.condition AS cond",
                        {"mnid": mnid},
                    )
                except Exception:
                    dispatched = []

                for d in dispatched:
                    tnid = d.get("tnid", "")
                    if not tnid or tnid in visited:
                        continue
                    visited.add(tnid)
                    tfqn = d.get("tfqn") or d.get("tname") or "?"
                    dtype = d.get("dtype") or "event"
                    cond = f" when `{d.get('cond')}`" if d.get("cond") else ""
                    queue.append((tnid, tfqn, cur_depth + 1, f"{dtype}{cond}"))

                try:
                    listeners = db.execute(
                        "MATCH (m:Method)-[:DISPATCHES]->(e:Event)<-[:LISTENS_TO]-(l:Listener) "
                        "WHERE m.node_id = $mnid "
                        "RETURN l.node_id AS lnid, l.fqn AS lfqn, l.name AS lname",
                        {"mnid": mnid},
                    )
                except Exception:
                    listeners = []

                for li in listeners:
                    lnid = li.get("lnid", "")
                    if not lnid or lnid in visited:
                        continue
                    visited.add(lnid)
                    lfqn = li.get("lfqn") or li.get("lname") or "?"
                    queue.append((lnid, lfqn, cur_depth + 1, "listener"))

        lines.extend(chain_lines)
        lines.append("")

        total = len(visited) - 1
        if total == 0:
            lines.append(
                "> No dispatch chain detected. This may mean:\n"
                "> - The job dispatches events/jobs dynamically (not statically traceable)\n"
                "> - The index is stale — run `laravelgraph analyze --full`\n"
                "> - Dispatch uses string-based class names not resolvable statically\n"
                "> \n"
                "> Use `laravelgraph_context(job, include_source=True)` to read the source directly."
            )

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_job_chain", {"job": job, "depth": depth}, total, elapsed)

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_context(symbol, include_source=True) to read source at any node",
            "Use laravelgraph_events for the full event → listener map",
            "Use laravelgraph_suggest_tests(job) to find tests covering this chain",
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

    @mcp.tool()
    def laravelgraph_list_procedures(keyword: str = "", connection: str = "") -> str:
        """List stored procedures with modification dates, parameter counts, table access, PHP references, and internal CALL chains."""
        db = _db()
        start = time.perf_counter()
        try:
            rows = db.execute(
                "MATCH (p:StoredProcedure) RETURN p.node_id AS nid, p.name AS name, p.connection AS conn, "
                "p.parameters AS params, p.full_body AS body, p.body_preview AS preview"
            )
            procs = [r for r in rows if (not connection or (r.get("conn") or "") == connection)]
            if keyword:
                kw = keyword.lower()
                procs = [r for r in procs if kw in (r.get("name") or "").lower()]
            proc_names = {r.get("name") or "" for r in procs}

            mod_dates: dict[str, str] = {}
            try:
                from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql
                db_configs = list(getattr(cfg, "databases", []) or [])
                for conn_cfg in db_configs:
                    if connection and conn_cfg.name != connection:
                        continue
                    mc = _connect_mysql(conn_cfg)
                    try:
                        with mc.cursor() as cur:
                            cur.execute(
                                "SELECT ROUTINE_NAME, LAST_ALTERED FROM information_schema.ROUTINES "
                                "WHERE ROUTINE_SCHEMA = DATABASE() AND ROUTINE_TYPE = 'PROCEDURE'"
                            )
                            for row in cur.fetchall():
                                name_val = str(row[0]) if row[0] else ""
                                mod_val = str(row[1])[:19] if row[1] else "—"
                                mod_dates[name_val] = mod_val
                    finally:
                        try:
                            mc.close()
                        except Exception:
                            pass
            except Exception:
                pass

            lines = [f"## Stored Procedures ({len(procs)})\n"]
            if not procs:
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_list_procedures", {"keyword": keyword, "connection": connection}, 0, elapsed)
                return "No stored procedures matched the current filters." + _with_confidence(
                    "HIGH",
                    "Result derived from indexed live DB procedure metadata.",
                ) + _next_steps(
                    "Use laravelgraph_connection_map to see all configured DB connections",
                    "Use laravelgraph_procedure_context(name) to inspect a specific procedure",
                )
            lines.append("| Procedure | Connection | Modified | Params | Reads | Writes | CALLs | Status |")
            lines.append("|-----------|------------|----------|--------|-------|--------|-------|--------|")
            for p in sorted(procs, key=lambda r: ((r.get("conn") or ""), (r.get("name") or "")))[:100]:
                nid, name = p.get("nid") or "", p.get("name") or "?"
                try:
                    params = json.loads(p.get("params") or "[]")
                    param_count = len(params) if isinstance(params, list) else 0
                except Exception:
                    raw = p.get("params") or ""
                    param_count = raw.count(",") + 1 if raw.strip() else 0
                try:
                    reads = db.execute(
                        "MATCH (p:StoredProcedure)-[:PROCEDURE_READS]->(t:DatabaseTable) WHERE p.node_id = $nid RETURN t.name AS name",
                        {"nid": nid},
                    )
                except Exception:
                    reads = []
                try:
                    writes = db.execute(
                        "MATCH (p:StoredProcedure)-[:PROCEDURE_WRITES]->(t:DatabaseTable) WHERE p.node_id = $nid RETURN t.name AS name",
                        {"nid": nid},
                    )
                except Exception:
                    writes = []
                try:
                    ref_rows = db.execute(
                        "MATCH (m:Method) WHERE m.fqn CONTAINS $proc_name RETURN count(m) AS cnt",
                        {"proc_name": name},
                    )
                except Exception:
                    ref_rows = []
                body = ""
                try:
                    body = (p.get("body") or p.get("preview") or "").lower()
                except Exception:
                    pass
                calls = sorted({pn for pn in proc_names if pn and pn != name and f"call {pn.lower()}" in body})[:3]
                reads_s = ", ".join(f"`{r.get('name')}`" for r in reads[:3]) or "—"
                writes_s = ", ".join(f"`{r.get('name')}`" for r in writes[:3]) or "—"
                status = "PHP_UNREFERENCED" if (ref_rows[0].get("cnt", 0) if ref_rows else 0) == 0 else "PHP_MATCHED"
                call_s = ", ".join(f"`{c}`" for c in calls) or "—"
                mod = mod_dates.get(name, "—")
                lines.append(
                    f"| `{name}` | `{p.get('conn') or 'unknown'}` | {mod} | {param_count} | {reads_s} | {writes_s} | {call_s} | {status} |"
                )
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_list_procedures", {"keyword": keyword, "connection": connection}, len(procs), elapsed)
            return "\n".join(lines) + _with_confidence(
                "HIGH",
                "Derived from indexed stored procedures produced by live DB introspection.",
            ) + _next_steps(
                "Use laravelgraph_procedure_context(name) to inspect SQL body and table access",
                "Use laravelgraph_connection_map to compare procedures across connections",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_list_procedures", {"keyword": keyword, "connection": connection}, 0, elapsed)
            return f"Error listing stored procedures: {e}" + _with_confidence(
                "LOW",
                "The query failed before stored procedure metadata could be assembled.",
                ["Stored procedure nodes or live DB metadata may be unavailable."],
            ) + _next_steps(
                "Use laravelgraph_connection_map to confirm procedure indexing exists",
                "Re-run laravelgraph analyze --full after configuring DB connections",
            )

    @mcp.tool()
    def laravelgraph_cross_cutting_concerns(min_call_sites: int = 3, min_layers: int = 2) -> str:
        """Find methods called from many files across multiple architectural layers to surface cross-cutting concerns."""
        db = _db()
        start = time.perf_counter()
        try:
            rows = db.execute(
                "MATCH (caller)-[:CALLS]->(target:Method) WHERE caller.file_path IS NOT NULL AND caller.file_path <> '' "
                "RETURN target.fqn AS target_fqn, target.node_id AS nid, caller.file_path AS file_path, caller.laravel_role AS role"
            )
            role_layer_map = {
                "model": "Model", "controller": "Controller", "service": "Service", "job": "Job",
                "listener": "Listener", "middleware": "Middleware", "command": "Command",
            }
            path_layer_map = {
                "/Controllers/": "Controller", "\\Controllers\\": "Controller",
                "/Models/": "Model", "\\Models\\": "Model",
                "/Services/": "Service", "\\Services\\": "Service",
                "/Jobs/": "Job", "\\Jobs\\": "Job",
                "/Listeners/": "Listener", "\\Listeners\\": "Listener",
                "/Helpers/": "Helper", "\\Helpers\\": "Helper",
                "/Commands/": "Command", "\\Commands\\": "Command",
                "/Middleware/": "Middleware", "\\Middleware\\": "Middleware",
            }
            grouped: dict[str, dict[str, Any]] = {}
            for row in rows:
                key = row.get("nid") or row.get("target_fqn") or "?"
                file_path = row.get("file_path") or ""
                role = (row.get("role") or "").lower()
                layer = role_layer_map.get(role, "")
                if not layer:
                    for path_fragment, path_layer in path_layer_map.items():
                        if path_fragment in file_path:
                            layer = path_layer
                            break
                bucket = grouped.setdefault(key, {"fqn": row.get("target_fqn") or "?", "files": set(), "sites": 0, "layers": set()})
                bucket["sites"] += 1
                bucket["files"].add(file_path)
                if layer:
                    bucket["layers"].add(layer)
            hits = []
            for bucket in grouped.values():
                if len(bucket["files"]) >= min_call_sites and len(bucket["layers"]) >= min_layers:
                    risk = "HIGH" if len(bucket["files"]) >= 8 or len(bucket["layers"]) >= 4 else "MEDIUM"
                    hits.append((bucket["sites"], len(bucket["files"]), bucket["fqn"], sorted(bucket["layers"]), risk))
            hits.sort(reverse=True)
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool(
                "laravelgraph_cross_cutting_concerns",
                {"min_call_sites": min_call_sites, "min_layers": min_layers},
                len(hits),
                elapsed,
            )
            if not hits:
                return "No cross-cutting concern candidates met the current thresholds." + _with_confidence(
                    "MEDIUM",
                    "This depends on how complete the indexed static call graph is.",
                    ["Dynamic dispatch and framework magic may hide some call sites."],
                ) + _next_steps(
                    "Lower min_call_sites or min_layers to widen the search",
                    "Use laravelgraph_context(symbol) on a likely utility method to inspect callers directly",
                )
            lines = [f"## Cross-Cutting Concern Candidates ({len(hits)})\n"]
            lines.append("| Symbol | Call Sites | Files | Layers | Risk |")
            lines.append("|--------|------------|-------|--------|------|")
            for sites, files, fqn, layers, risk in hits[:50]:
                lines.append(f"| `{fqn}` | {sites} | {files} | {', '.join(layers)} | {risk} |")
            return "\n".join(lines) + _with_confidence(
                "MEDIUM",
                "Cross-layer fan-in is inferred from static CALLS edges and file-role metadata.",
                ["Dynamic method calls and unresolved container dispatches reduce completeness."],
            ) + _next_steps(
                "Use laravelgraph_context(symbol, include_source=True) to inspect a high-risk concern",
                "Use laravelgraph_impact(symbol) to measure the blast radius before refactoring",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_cross_cutting_concerns", {"min_call_sites": min_call_sites, "min_layers": min_layers}, 0, elapsed)
            return f"Error finding cross-cutting concerns: {e}" + _with_confidence(
                "LOW",
                "The analysis failed before call graph aggregation completed.",
                ["CALLS edges or laravel_role metadata may be missing."],
            ) + _next_steps(
                "Use laravelgraph_query(query='service helper util') to find shared utilities manually",
                "Re-run laravelgraph analyze --full if call graph data seems stale",
            )

    @mcp.tool()
    def laravelgraph_boundary_map(table: str = "") -> str:
        """Show which PHP and stored procedure layers access a table and flag mixed-boundary conflicts."""
        db = _db()
        start = time.perf_counter()
        try:
            meta_rows = db.execute(
                "MATCH (t:DatabaseTable) RETURN t.name AS tname, t.connection AS conn, t.source AS source"
            )
            meta = {
                (r.get("tname") or "", r.get("conn") or ""): {"source": r.get("source") or "migration"}
                for r in meta_rows if not table or (r.get("tname") or "") == table
            }
            import re as _re
            all_models = db.execute(
                "MATCH (m:EloquentModel) RETURN m.name AS model_name, m.fqn AS model_fqn, "
                "m.db_table AS graph_table, m.file_path AS fp"
            )
            model_table_map: dict[tuple[str, str], list[str]] = {}
            for r in all_models:
                fp = r.get("fp") or ""
                graph_tbl = r.get("graph_table") or ""
                actual_tbl = graph_tbl
                if fp:
                    try:
                        _fp = Path(fp)
                        if not _fp.is_absolute():
                            _fp = project_root / _fp
                        head = _fp.read_text(errors="replace")[:3000]
                        tm = _re.search(r"\$table\s*=\s*['\"]([^'\"]+)['\"]", head)
                        if tm:
                            actual_tbl = tm.group(1)
                    except OSError:
                        pass
                if actual_tbl:
                    for (tname, conn) in meta:
                        if tname == actual_tbl:
                            model_table_map.setdefault((tname, conn), []).append(
                                r.get("model_fqn") or r.get("model_name") or ""
                            )
                            break
            code_rows = db.execute(
                "MATCH (src)-[q:QUERIES_TABLE]->(t:DatabaseTable) RETURN t.name AS tname, t.connection AS conn, "
                "q.operation AS op, q.via AS via"
            )
            proc_r = db.execute(
                "MATCH (p:StoredProcedure)-[:PROCEDURE_READS]->(t:DatabaseTable) RETURN t.name AS tname, t.connection AS conn"
            )
            proc_w = db.execute(
                "MATCH (p:StoredProcedure)-[:PROCEDURE_WRITES]->(t:DatabaseTable) RETURN t.name AS tname, t.connection AS conn"
            )
            buckets: dict[tuple[str, str], dict[str, Any]] = {
                k: {
                    "markers": set(),
                    "php_read": False,
                    "php_write": False,
                    "proc_read": False,
                    "proc_write": False,
                    "source": v["source"],
                    "notes": set(),
                }
                for k, v in meta.items()
            }
            for row in code_rows:
                key = (row.get("tname") or "", row.get("conn") or "")
                if key not in buckets:
                    continue
                via = (row.get("via") or "").lower()
                op = (row.get("op") or "").lower()
                if via == "eloquent":
                    if op in ("read", "readwrite"):
                        buckets[key]["markers"].add("PHP_Eloquent_Read")
                        buckets[key]["php_read"] = True
                    if op != "read":
                        buckets[key]["markers"].add("PHP_Eloquent_Write")
                        buckets[key]["php_write"] = True
                if via in ("query_builder", "raw_sql"):
                    buckets[key]["markers"].add("PHP_QueryBuilder")
                    buckets[key]["php_read"] = buckets[key]["php_read"] or op in ("read", "readwrite")
                    buckets[key]["php_write"] = buckets[key]["php_write"] or op != "read"
            for row in proc_r:
                key = (row.get("tname") or "", row.get("conn") or "")
                if key in buckets:
                    buckets[key]["markers"].add("StoredProcedure_Read")
                    buckets[key]["proc_read"] = True
            for row in proc_w:
                key = (row.get("tname") or "", row.get("conn") or "")
                if key in buckets:
                    buckets[key]["markers"].add("StoredProcedure_Write")
                    buckets[key]["proc_write"] = True
            for (tname, conn), models in model_table_map.items():
                if table and tname != table:
                    continue
                key = (tname, conn)
                if key not in buckets:
                    key = next((k for k in buckets if k[0] == tname), None)
                if not key or key not in buckets:
                    continue
                for model_fqn in models:
                    if not model_fqn:
                        continue
                    short_name = model_fqn.rsplit(chr(92), 1)[-1]
                    buckets[key]["notes"].add(
                        f"PHP access resolved via $table = '{tname}' override on {short_name}"
                    )

                    try:
                        qt_rows = db.execute(
                            "MATCH (c:Class_)-[:DEFINES]->(m:Method)-[q:QUERIES_TABLE]->(t:DatabaseTable) "
                            "WHERE c.fqn = $fqn AND t.name = $tname "
                            "RETURN m.name AS mname, q.operation AS op",
                            {"fqn": model_fqn, "tname": tname},
                        )
                    except Exception:
                        qt_rows = []

                    try:
                        all_methods = db.execute(
                            "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
                            "WHERE c.fqn = $fqn "
                            "RETURN m.name AS mname, m.file_path AS fp, m.line_start AS ls, m.line_end AS le",
                            {"fqn": model_fqn},
                        )
                    except Exception:
                        all_methods = []

                    if qt_rows:
                        for mrow in qt_rows:
                            op = (mrow.get("op") or "").lower()
                            if op in ("read", "readwrite"):
                                buckets[key]["markers"].add("PHP_Eloquent_Read")
                                buckets[key]["php_read"] = True
                            if op != "read":
                                buckets[key]["markers"].add("PHP_Eloquent_Write")
                                buckets[key]["php_write"] = True
                    elif all_methods:
                        buckets[key]["markers"].add("PHP_Eloquent_Read")
                        buckets[key]["php_read"] = True
                        from laravelgraph.mcp.explain import read_source_snippet as _read_src
                        _write_keywords = ("->save(", "->update(", "->delete(", "::create(", "->insert(")
                        for m in all_methods[:20]:
                            try:
                                src = _read_src(m.get("fp", ""), m.get("ls", 0), m.get("le", 0), project_root) or ""
                                if any(kw in src for kw in _write_keywords):
                                    buckets[key]["markers"].add("PHP_Eloquent_Write")
                                    buckets[key]["php_write"] = True
                                    break
                            except Exception:
                                pass
            rows_out = []
            for (tname, conn), info in buckets.items():
                conflicts = []
                if info["proc_read"] and info["php_write"]:
                    conflicts.append("PROC_READS_vs_PHP_WRITES")
                if info["proc_write"] and info["php_read"]:
                    conflicts.append("PROC_WRITES_vs_PHP_READS")
                if not table and len(info["markers"]) < 2 and not conflicts:
                    continue
                rows_out.append((
                    tname,
                    conn or "migration",
                    sorted(info["markers"]),
                    conflicts,
                    info["source"],
                    sorted(info["notes"]),
                ))
            rows_out.sort(key=lambda x: (0 if x[3] else 1, x[0], x[1]))
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_boundary_map", {"table": table}, len(rows_out), elapsed)
            if not rows_out:
                return "No multi-boundary table access patterns matched the current filter." + _with_confidence(
                    "MEDIUM",
                    "No mixed PHP/procedure access was found in the indexed graph for this scope.",
                ) + _next_steps(
                    "Use laravelgraph_db_context(table) to inspect one table in detail",
                    "Run laravelgraph_connection_map to confirm live DB and procedure indexing coverage",
                )
            lines = [f"## Boundary Map{' for `' + table + '`' if table else ''} ({len(rows_out)})\n"]
            lines.append("| Table | Connection | Access Layers | Conflicts | Notes |")
            lines.append("|-------|------------|---------------|-----------|-------|")
            for tname, conn, markers, conflicts, _source, notes in rows_out[:50]:
                lines.append(
                    f"| `{tname}` | `{conn}` | {', '.join(markers) or '—'} | {', '.join(conflicts) or '—'} | {', '.join(notes) or '—'} |"
                )
            has_model_override = any("$table override" in n for item in rows_out for n in item[5])
            has_live = any(item[4] == "live_db" for item in rows_out)
            if has_live and not has_model_override:
                conf_level = "HIGH"
                conf_reason = "Live DB table metadata backs these boundary relationships."
            elif has_live and has_model_override:
                conf_level = "HIGH"
                conf_reason = "Live DB + Eloquent $table override resolved PHP access layers."
            else:
                conf_level = "MEDIUM"
                conf_reason = "Derived from indexed code and migration-level metadata."
            return "\n".join(lines) + _with_confidence(
                conf_level,
                conf_reason,
                None if conf_level == "HIGH" else ["Migration-only tables may miss production-only procedure access."],
            ) + _next_steps(
                "Use laravelgraph_db_impact(table, operation='write') to trace downstream effects",
                "Use laravelgraph_procedure_context(name) on any procedure touching a conflicted table",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_boundary_map", {"table": table}, 0, elapsed)
            return f"Error building boundary map: {e}" + _with_confidence(
                "LOW",
                "The map failed before table access patterns could be consolidated.",
                ["QUERIES_TABLE or PROCEDURE_* edges may be missing."],
            ) + _next_steps(
                "Use laravelgraph_db_context(table) for single-table analysis",
                "Re-run laravelgraph analyze --full after DB introspection if procedure data is missing",
            )

    @mcp.tool()
    def laravelgraph_data_quality_report(
        connection: str = "",
        table_filter: str = "",
        min_rows: int = 100,
    ) -> str:
        """Scan live DB for data quality issues: boolean columns storing non-boolean values,
        near-duplicate enum strings, and broken status/state fields with empty/None mixed in.

        Optimized: batches queries, skips tables under min_rows, caches results.
        Use table_filter to scan a single table (fast) or omit for full scan.

        Args:
            connection: DB connection name (optional — scans all if omitted)
            table_filter: Scan only this table (fast mode — recommended for first use)
            min_rows: Skip tables with fewer rows than this (default 100)
        """
        start = time.perf_counter()
        timeout_seconds = 30.0
        db = _db()
        db_configs = [
            c for c in (cfg.databases if hasattr(cfg, "databases") else [])
            if not connection or c.name == connection
        ]
        if not db_configs:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_data_quality_report", {}, 0, elapsed)
            return "No configured database connections." + _with_confidence(
                "LOW", "Requires a configured live MySQL connection.",
            )

        try:
            from laravelgraph.pipeline.phase_24_db_introspect import _connect_mysql

            table_where = ""
            if table_filter:
                safe_tf = table_filter.replace("`", "")
                table_where = f" AND t.name = '{safe_tf}'"

            cols = db.execute(
                "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) "
                f"WHERE t.source = 'live_db'{table_where} "
                "RETURN t.name AS tname, t.connection AS conn, c.name AS cname, c.full_type AS ftype"
            )
            if connection:
                cols = [c for c in cols if (c.get("conn") or "") == connection]

            bool_cols: dict[str, list[str]] = {}
            varchar_cols: list[dict] = []
            for c in cols:
                tname = c.get("tname") or ""
                cname = c.get("cname") or ""
                ftype = (c.get("ftype") or "").lower()
                if not tname or not cname:
                    continue
                if "tinyint(1" in ftype:
                    bool_cols.setdefault(tname, []).append(cname)
                if "varchar" in ftype or any(
                    k in cname.lower() for k in ("status", "state", "type")
                ):
                    varchar_cols.append(c)

            issues: list[dict[str, Any]] = []
            writers_cache: dict[str, str] = {}

            def writers(tname: str) -> str:
                if tname not in writers_cache:
                    try:
                        rows = db.execute(
                            "MATCH (src:Method)-[q:QUERIES_TABLE]->(t:DatabaseTable) "
                            "WHERE t.name = $t AND q.operation <> 'read' "
                            "RETURN src.fqn AS fqn LIMIT 3",
                            {"t": tname},
                        )
                        writers_cache[tname] = ", ".join(f"`{r.get('fqn')}`" for r in rows) or "—"
                    except Exception:
                        writers_cache[tname] = "—"
                return writers_cache[tname]

            for conn_cfg in db_configs:
                mc = _connect_mysql(conn_cfg)
                try:
                    with mc.cursor() as cur:
                        if min_rows > 0 and not table_filter:
                            cur.execute(
                                "SELECT TABLE_NAME FROM information_schema.TABLES "
                                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_ROWS >= %s",
                                (min_rows,),
                            )
                            big_tables = {row[0] for row in cur.fetchall()}
                        else:
                            big_tables = None

                        batch_size = 10
                        bool_tables = list(bool_cols.keys())
                        if big_tables is not None:
                            bool_tables = [t for t in bool_tables if t in big_tables]

                        for i in range(0, len(bool_tables), batch_size):
                            if time.perf_counter() - start > timeout_seconds:
                                issues.append({
                                    "table": "—", "column": "—",
                                    "type": "SCAN_TIMEOUT",
                                    "expected": "—", "actual": "—", "count": 0,
                                    "risk": f"Scan stopped at {timeout_seconds}s. Use table_filter for targeted checks.",
                                    "writers": "—",
                                })
                                break

                            batch_tables = bool_tables[i:i + batch_size]
                            unions = []
                            for tname in batch_tables:
                                safe_t = tname.replace("`", "")
                                for cname in bool_cols[tname]:
                                    safe_c = cname.replace("`", "")
                                    unions.append(
                                        f"SELECT '{safe_t}' AS tbl, '{safe_c}' AS col, "
                                        f"CAST(`{safe_c}` AS CHAR) AS val, COUNT(*) AS cnt "
                                        f"FROM `{safe_t}` "
                                        f"WHERE `{safe_c}` IS NOT NULL AND `{safe_c}` NOT IN (0,1) "
                                        f"GROUP BY `{safe_c}` ORDER BY cnt DESC LIMIT 5"
                                    )
                            if not unions:
                                continue

                            for sql in unions:
                                try:
                                    cur.execute(sql)
                                    bad = cur.fetchall()
                                    if bad:
                                        tbl = bad[0][0]
                                        col = bad[0][1]
                                        detail = ", ".join(f"{r[2]} ({r[3]})" for r in bad)
                                        total = sum(int(r[3]) for r in bad)
                                        issues.append({
                                            "table": tbl, "column": col,
                                            "type": "BOOLEAN_DRIFT",
                                            "expected": "0, 1, NULL",
                                            "actual": detail,
                                            "count": total,
                                            "risk": "Column used as counter or flag with non-boolean values.",
                                            "writers": writers(tbl),
                                        })
                                except Exception:
                                    continue

                        scanned_varchar = 0
                        for vc in varchar_cols:
                            if time.perf_counter() - start > timeout_seconds:
                                break
                            tname = vc.get("tname") or ""
                            cname = vc.get("cname") or ""
                            if big_tables is not None and tname not in big_tables:
                                continue

                            sample = _fetch_varchar_sample(tname, cname, conn_cfg, max_distinct=30)
                            if not sample:
                                continue
                            rows_data, overflow = sample
                            if overflow or not rows_data:
                                continue

                            norm: dict[str, list[dict]] = {}
                            for r in rows_data:
                                norm.setdefault(
                                    str(r.get("val") or "").strip().lower(), []
                                ).append(r)

                            dup_groups = [
                                grp for key, grp in norm.items()
                                if key and len({str(x.get("val") or "") for x in grp}) > 1
                            ]
                            if dup_groups:
                                actual = "; ".join(
                                    " / ".join(f"{x.get('val')} ({x.get('cnt')})" for x in grp)
                                    for grp in dup_groups[:3]
                                )
                                issues.append({
                                    "table": tname, "column": cname,
                                    "type": "ENUM_NEAR_DUPLICATE",
                                    "expected": "Canonicalized enum values",
                                    "actual": actual,
                                    "count": sum(int(x.get("cnt") or 0) for grp in dup_groups for x in grp),
                                    "risk": "Near-duplicate text values split state reporting.",
                                    "writers": writers(tname),
                                })

                            bad_keys = [
                                k for k in norm
                                if k in ("", "none")
                                and any(other not in ("", "none") for other in norm)
                            ]
                            if bad_keys and any(k in cname.lower() for k in ("status", "state")):
                                actual = ", ".join(
                                    f"{x.get('val')!r} ({x.get('cnt')})"
                                    for k in bad_keys for x in norm[k]
                                )
                                issues.append({
                                    "table": tname, "column": cname,
                                    "type": "STATUS_MIXED_EMPTY",
                                    "expected": "Non-empty status values",
                                    "actual": actual,
                                    "count": sum(int(x.get("cnt") or 0) for k in bad_keys for x in norm[k]),
                                    "risk": "Empty/None mixed with real workflow states.",
                                    "writers": writers(tname),
                                })
                            scanned_varchar += 1
                finally:
                    try:
                        mc.close()
                    except Exception:
                        pass

            issues.sort(key=lambda x: int(x.get("count") or 0), reverse=True)
            issues = issues[:50]
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_data_quality_report", {"table_filter": table_filter}, len(issues), elapsed)

            if not issues:
                scope = f"table `{table_filter}`" if table_filter else f"all tables with ≥{min_rows} rows"
                return f"No data quality issues detected in {scope}." + _with_confidence(
                    "HIGH", f"Scanned {scope} via live DB queries in {elapsed:.0f}ms.",
                ) + _next_steps(
                    "Use table_filter to scan a specific table: data_quality_report(table_filter='orders')",
                    "Use laravelgraph_db_query for ad-hoc distribution checks",
                )

            timed_out = any(i["type"] == "SCAN_TIMEOUT" for i in issues)
            scope = f"table `{table_filter}`" if table_filter else f"tables with ≥{min_rows} rows"
            lines = [f"## Data Quality Report ({len(issues)} issues from {scope})\n"]
            lines.append("| Table | Column | Issue | Actual Values | Affected Rows | Write Paths |")
            lines.append("|-------|--------|-------|---------------|---------------|-------------|")
            for issue in issues:
                if issue["type"] == "SCAN_TIMEOUT":
                    lines.append(f"| ⏱ | — | **TIMEOUT** | — | — | {issue['risk']} |")
                    continue
                lines.append(
                    f"| `{issue['table']}` | `{issue['column']}` | {issue['type']} "
                    f"| {issue['actual'][:80]} | {issue['count']:,} | {issue['writers']} |"
                )

            conf = "MEDIUM" if timed_out else "HIGH"
            conf_reason = f"Live DB scan completed in {elapsed:.0f}ms"
            if timed_out:
                conf_reason = f"Scan hit {timeout_seconds}s timeout — partial results"

            return "\n".join(lines) + _with_confidence(conf, conf_reason) + _next_steps(
                "Use data_quality_report(table_filter='table_name') to scan a specific table",
                "Use laravelgraph_resolve_column(table, column) for deep-dive on any flagged column",
                "Use laravelgraph_db_query for ad-hoc verification of any finding",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_data_quality_report", {}, 0, elapsed)
            return f"Error: {e}" + _with_confidence(
                "LOW", "Live DB scan failed.",
                [str(e)],
            )

    @mcp.tool()
    def laravelgraph_race_conditions() -> str:
        """Search for likely check-then-act race conditions that mutate shared counters without transaction or lock protection."""
        db = _db()
        start = time.perf_counter()
        try:
            import re
            from laravelgraph.mcp.explain import read_source_snippet
            rows = db.execute(
                "MATCH (c:Class_)-[:DEFINES]->(m:Method) WHERE c.laravel_role IN ['model','controller','service','job'] "
                "RETURN c.laravel_role AS role, m.fqn AS fqn, m.name AS mname, m.file_path AS fp, m.line_start AS ls, m.line_end AS le ORDER BY m.fqn LIMIT 800"
            )
            _ANALYTICS_NAMES = re.compile(
                r"(avg|trend|insight|report|stats|analytics|export|download|index$|list$|show$|get[A-Z])",
                re.IGNORECASE,
            )

            def _strip_comments(code: str) -> str:
                code = re.sub(r"//[^\n]*", "", code)
                code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
                return code

            findings = []
            for row in rows:
                fp = row.get("fp") or ""
                mname = row.get("mname") or ""
                if not fp or "/tests/" in fp or fp.endswith(".blade.php"):
                    continue
                if _ANALYTICS_NAMES.search(mname):
                    continue
                raw_src = read_source_snippet(fp, row.get("ls", 0), row.get("le", 0), project_root)
                if not raw_src:
                    continue
                src = _strip_comments(raw_src)
                low = src.lower()
                if any(tok in low for tok in ("db::transaction", "lockforupdate", "sharedlock", "cache::lock", "->lock(")):
                    continue
                if "$request->" in low and "->save()" not in low:
                    continue
                pattern_hits: list[dict[str, str]] = []

                # Multi-line window: $var->prop = ... $var->prop ... followed by $var->save()
                # Scans line-by-line to avoid Python regex backreference issues with dollar signs.
                _SKIP_VARS = frozenset({"request", "response", "this", "data", "input", "params"})
                _SKIP_COLS = frozenset({"id", "created_at", "updated_at", "deleted_at"})
                src_lines = src.splitlines()
                for _li, _line in enumerate(src_lines):
                    _assign_m = re.search(r'\$(\w+)->(\w+)\s*=', _line)
                    if not _assign_m:
                        continue
                    _var, _col = _assign_m.group(1), _assign_m.group(2)
                    if _var in _SKIP_VARS or _col in _SKIP_COLS:
                        continue
                    # RHS must reference the same $var->col (read-modify-write)
                    _rhs = _line[_assign_m.end():]
                    if not re.search(r'\$' + re.escape(_var) + r'->' + re.escape(_col) + r'(?:\s|[-+*/;]|$)', _rhs):
                        continue
                    # Skip atomic ops
                    if re.search(r'(?:decrement|increment)\(\s*[\'"]' + re.escape(_col) + r'[\'"]', src):
                        continue
                    # $var->save() must appear within 10 lines
                    _window = '\n'.join(src_lines[_li:_li + 10])
                    if not re.search(r'\$' + re.escape(_var) + r'->\s*save\s*\(', _window):
                        continue
                    pattern_hits.append({
                        "pattern_type": "ELOQUENT_PROPERTY",
                        "cols": _col,
                        "evidence": _line.strip()[:120],
                    })

                count_m = re.search(
                    r"(->count\(\)|->sum\([^)]*\))\s*;\s*\n[^;]*if\s*\(\s*\$\w+\s*(?:<|<=|>=|>)\s*\$?\w+",
                    src, re.DOTALL,
                )
                if count_m and re.search(r"::create\(|->insert\(", src):
                    evidence = count_m.group(0)[:100].strip()
                    pattern_hits.append({
                        "pattern_type": "CAPACITY_CHECK",
                        "cols": "count/capacity",
                        "evidence": evidence,
                    })

                for match in re.finditer(
                    r"if\s*\(\s*\$(\w+)->\s*(\w+)\s*(?:==|===)\s*['\"](\w+)['\"]", src
                ):
                    var, col, _val = match.group(1), match.group(2), match.group(3)
                    if var == "request":
                        continue
                    if not col.endswith(("status", "state", "stage")):
                        continue
                    write_pat = re.compile(rf"\${re.escape(var)}->\s*{re.escape(col)}\s*=\s*['\"]")
                    if not write_pat.search(src):
                        continue
                    if not re.search(rf"\${re.escape(var)}->\s*save\s*\(", src):
                        continue
                    evidence = next(
                        (line.strip() for line in src.splitlines()
                         if col in line and ("if" in line.lower() or "=" in line)),
                        match.group(0),
                    )
                    pattern_hits.append({
                        "pattern_type": "STATUS_GATE",
                        "cols": col,
                        "evidence": evidence[:120],
                    })

                seen_patterns: set[tuple[str, ...]] = set()
                for hit in pattern_hits:
                    dedupe_key = (row.get("fqn") or "?", hit["pattern_type"], hit["cols"])
                    if dedupe_key in seen_patterns:
                        continue
                    seen_patterns.add(dedupe_key)
                    findings.append({
                        "fqn": row.get("fqn") or "?",
                        "role": row.get("role") or "?",
                        "file": Path(fp).name,
                        "evidence": hit["evidence"],
                        "cols": hit["cols"],
                        "pattern_type": hit["pattern_type"],
                    })
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_race_conditions", {}, len(findings), elapsed)
            if not findings:
                return "No obvious check-then-act race condition candidates were detected." + _with_confidence(
                    "LOW",
                    "This tool uses source-pattern heuristics and may miss framework-specific locking strategies.",
                ) + _next_steps(
                    "Use laravelgraph_query(query='decrement stock seats') to inspect high-risk counters manually",
                    "Use laravelgraph_context(symbol, include_source=True) on inventory-like methods to confirm locking",
                )
            lines = [f"## Possible Race Conditions ({len(findings)})\n"]
            lines.append("| Method | Role | File | Pattern Type | Columns | Evidence |")
            lines.append("|--------|------|------|--------------|---------|----------|")
            for item in findings[:30]:
                lines.append(
                    f"| `{item['fqn']}` | {item['role']} | `{item['file']}` | {item['pattern_type']} | {item['cols']} | `{item['evidence']}` |"
                )
            return "\n".join(lines) + _with_confidence(
                "LOW",
                "Findings are produced by static regex matching over method source snippets.",
                ["False positives are possible when locking happens in a caller or helper.", "False negatives are possible for indirect condition checks."],
            ) + _next_steps(
                "Use laravelgraph_context(symbol, include_source=True) to validate each candidate in full source",
                "Search for DB::transaction or Cache::lock in callers with laravelgraph_impact(symbol)",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_race_conditions", {}, 0, elapsed)
            return f"Error searching for race conditions: {e}" + _with_confidence(
                "LOW",
                "The heuristic source scan did not complete successfully.",
                ["Method source snippets or class-role metadata may be incomplete."],
            ) + _next_steps(
                "Use laravelgraph_query(query='decrement') to search high-risk code paths manually",
                "Re-run laravelgraph analyze if file paths or method ranges look stale",
            )

    @mcp.tool()
    def laravelgraph_security_surface() -> str:
        """Prioritize webhook verification gaps, sensitive payment data retention, and missing auth middleware across the indexed security surface."""
        db = _db()
        start = time.perf_counter()
        try:
            import re
            from laravelgraph.mcp.explain import read_source_snippet
            issues: list[dict[str, Any]] = []
            route_rows = db.execute(
                "MATCH (r:Route) RETURN r.http_method AS method, r.uri AS uri, r.controller_fqn AS ctrl, r.action_method AS action, r.middleware_stack AS mw"
            )
            src_cache: dict[str, str] = {}
            for row in route_rows:
                uri = (row.get("uri") or "").lower()
                if (row.get("method") or "").upper() != "POST" or not any(k in uri for k in ("webhook", "callback", "notify", "ipn")):
                    continue
                mids = json.loads(row.get("mw") or "[]") if row.get("mw") else []
                mid_text = " ".join(mids).lower()
                if "auth" in mid_text or "csrf" in mid_text or "verifycsrftoken" in mid_text:
                    continue
                key = f"{row.get('ctrl') or ''}::{row.get('action') or ''}"
                if key not in src_cache and row.get("ctrl") and row.get("action"):
                    method_rows = db.execute(
                        "MATCH (c:Class_)-[:DEFINES]->(m:Method) WHERE c.fqn = $cfqn AND m.name = $mname RETURN m.file_path AS fp, m.line_start AS ls, m.line_end AS le",
                        {"cfqn": row.get("ctrl"), "mname": row.get("action")},
                    )
                    src_cache[key] = read_source_snippet(method_rows[0].get("fp", ""), method_rows[0].get("ls", 0), method_rows[0].get("le", 0), project_root).lower() if method_rows else ""
                src = src_cache.get(key, "")
                if not any(tok in src for tok in ("hash_hmac", "constructevent", "signature", "verifysignature", "validate")):
                    issues.append({"severity": "CRITICAL", "kind": "UNVERIFIED_WEBHOOK", "location": f"{row.get('method')} {row.get('uri')}", "evidence": key or "route handler unresolved", "fix": "Verify provider signatures before processing webhook payloads."})
            sens_rows = db.execute(
                "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE t.connection IS NOT NULL AND t.connection <> '' RETURN t.name AS tname, c.name AS cname"
            )
            sensitive_patterns = [
                "card_number", "card_last_four", "card_last4", "card_brand", "name_on_card",
                "payment_method_id", "client_secret", "stripe_token", "stripetoken",
                "stripeToken", "access_token", "secret_key", "private_key", "api_key",
                "card_name", "cvv", "pan_number",
            ]
            exclude_patterns = [
                "card_title", "enable_card", "card_id", "company_name",
                "company_logo", "company_address", "rate_", "expected_", "occupancy",
                "enable_", "token_2fa",
            ]
            exclude_tables: list[str] = []
            sensitive = []
            for row in sens_rows:
                tname = (row.get("tname") or "").lower()
                cname = (row.get("cname") or "").lower()
                if not tname or not cname:
                    continue
                if not any(p.lower() in cname for p in sensitive_patterns):
                    continue
                if any(p.lower() in cname for p in exclude_patterns):
                    continue
                if any(t.lower() in tname for t in exclude_tables):
                    continue
                sensitive.append(row)
            for row in sensitive[:30]:
                accessors = db.execute(
                    "MATCH (src)-[q:QUERIES_TABLE]->(t:DatabaseTable) WHERE t.name = $t AND q.operation <> 'read' RETURN src.fqn AS fqn, src.file_path AS fp, src.line_start AS ls, src.line_end AS le LIMIT 20",
                    {"t": row.get("tname")},
                )
                clear_found = False
                for src in accessors:
                    code = read_source_snippet(src.get("fp", ""), src.get("ls", 0), src.get("le", 0), project_root).lower()
                    col = (row.get("cname") or "").lower()
                    if re.search(rf"{re.escape(col)}\s*['\"]?\s*=>\s*null|{re.escape(col)}\s*=\s*null", code):
                        clear_found = True
                        break
                if not clear_found:
                    issues.append({"severity": "HIGH", "kind": "SENSITIVE_DATA_RETENTION", "location": f"{row.get('tname')}.{row.get('cname')}", "evidence": "No indexed nullify/clear path detected in write methods", "fix": "Store tokens in a vault or clear sensitive columns after use."})
            groups: dict[str, list[dict[str, Any]]] = {}
            for row in route_rows:
                uri = "/" + ((row.get("uri") or "").strip("/").split("/")[0] if row.get("uri") else "")
                groups.setdefault(uri, []).append(row)
            for prefix, routes in groups.items():
                if len(routes) < 3:
                    continue
                auth_count = 0
                for route in routes:
                    mids = json.loads(route.get("mw") or "[]") if route.get("mw") else []
                    auth_count += 1 if any("auth" in str(m).lower() for m in mids) else 0
                if auth_count <= len(routes) / 2:
                    continue
                for route in routes:
                    mids = json.loads(route.get("mw") or "[]") if route.get("mw") else []
                    if not any("auth" in str(m).lower() for m in mids):
                        issues.append({"severity": "MEDIUM", "kind": "MISSING_AUTH", "location": f"{route.get('method')} {route.get('uri')}", "evidence": f"Peer routes under `{prefix}` mostly use auth middleware", "fix": "Add the appropriate auth/guard middleware or document why this route is public."})
            order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
            issues.sort(key=lambda x: (order.get(x["severity"], 9), x["kind"], x["location"]))
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_security_surface", {}, len(issues), elapsed)
            if not issues:
                return "No prioritized security surface issues were detected with the current heuristics." + _with_confidence(
                    "MEDIUM",
                    "This report is heuristic and focuses on high-signal patterns only.",
                ) + _next_steps(
                    "Use laravelgraph_routes(filter='webhook') to inspect public callback routes manually",
                    "Use laravelgraph_db_context(table) on payment tables to review sensitive columns in context",
                )
            lines = [f"## Security Surface ({len(issues)})\n"]
            lines.append("| Severity | Finding | Location | Evidence | Recommended Fix |")
            lines.append("|----------|---------|----------|----------|------------------|")
            for item in issues[:50]:
                lines.append(
                    f"| {item['severity']} | {item['kind']} | `{item['location']}` | {item['evidence'][:90]} | {item['fix']} |"
                )
            return "\n".join(lines) + _with_confidence(
                "MEDIUM",
                "Findings are inferred from route middleware, source-pattern checks, and sensitive column usage.",
                ["Indirect verification helpers and background sanitization outside indexed write paths may be missed."],
            ) + _next_steps(
                "Use laravelgraph_request_flow(route='/...') to inspect a flagged route end to end",
                "Use laravelgraph_context(symbol, include_source=True) on flagged handlers to validate the evidence",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_security_surface", {}, 0, elapsed)
            return f"Error analyzing security surface: {e}" + _with_confidence(
                "LOW",
                "The heuristic security scan did not complete successfully.",
                ["Some route, source, or database metadata could not be read."],
            ) + _next_steps(
                "Use laravelgraph_routes(filter='webhook') and laravelgraph_db_context(table) to inspect the highest-risk areas manually",
                "Re-run laravelgraph analyze --full if route or DB metadata appears incomplete",
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
                        if cfg.llm.enabled:
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
                                            summary_cfg=cfg.llm,
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
                            if cfg.llm.enabled and fp and ls:
                                src = read_source_snippet(fp, ls, le, project_root)
                                if src:
                                    summary, provider_used = generate_summary(
                                        fqn=ctrl_action,
                                        node_type="controller action",
                                        source=src,
                                        docblock=row.get("doc", ""),
                                        summary_cfg=cfg.llm,
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
    def laravelgraph_cypher(query: str, graph: str = "core") -> str:
        """Execute a Cypher query against the knowledge graph.

        By default queries the core graph (read-only).
        Use graph="plugin" to query the plugin knowledge graph (writable runtime data).

        Only MATCH, RETURN, WITH, WHERE, ORDER BY, LIMIT are allowed for graph="core".
        The plugin graph (graph="plugin") supports write operations.

        Args:
            query: Cypher query string
            graph: Which graph to query — "core" (default) or "plugin"
        """
        if graph == "plugin":
            start = time.perf_counter()
            try:
                results = _plugin_db.execute(query)
            except Exception as e:
                return f"Plugin graph query error: {e}"
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_cypher", {"query": query[:100], "graph": "plugin"}, len(results), elapsed)
            if not results:
                return "Plugin graph query returned no results."
            lines = [f"## Plugin Graph Query Results ({len(results)} rows)\n"]
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

        forbidden = ["CREATE", "MERGE", "DELETE", "SET", "REMOVE", "DROP", "DETACH"]
        q_upper = query.upper()
        for kw in forbidden:
            if kw in q_upper:
                return f"Mutation keyword '{kw}' is not allowed. Only read-only queries permitted."

        _LABEL_FIXES = {
            "Class": "Class_",
            "Function": "Function_",
            "Trait": "Trait_",
            "Interface": "Interface_",
            "Enum": "Enum_",
        }
        import re as _re
        for wrong, right in _LABEL_FIXES.items():
            query = _re.sub(rf"\b{wrong}\b(?!_)", right, query)

        db = _db()
        start = time.perf_counter()

        try:
            results = db.execute(query)
        except Exception as e:
            hint = ""
            err_str = str(e)
            if "does not exist" in err_str:
                hint = (
                    "\n\nNode labels use trailing underscores for Python keywords: "
                    "Class_, Function_, Trait_, Interface_, Enum_. "
                    "Use laravelgraph://schema resource to see all available types."
                )
            return f"Query error: {e}{hint}"

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

    # ── Tool: laravelgraph_features ──────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_features(feature: str = "") -> str:
        """List all auto-detected product features and their constituent symbols. Pass feature name to drill into one.

        Args:
            feature: Feature name or slug to drill into (leave empty to list all)
        """
        db = _db()
        start = time.perf_counter()
        try:
            if not feature:
                rows = db.execute(
                    "MATCH (f:Feature) RETURN f.node_id AS node_id, f.name AS name, f.slug AS slug, "
                    "f.entry_routes AS entry_routes, f.symbol_count AS symbol_count, f.has_changes AS has_changes "
                    "ORDER BY f.name"
                )
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_features", {"feature": feature}, len(rows), elapsed)
                if not rows:
                    return (
                        "No Feature nodes found. Run `laravelgraph analyze` to index the project."
                        + _with_confidence("LOW", "Feature detection requires a fully analyzed project.")
                        + _next_steps(
                            "Run laravelgraph analyze <project> to index features",
                            "Use laravelgraph_routes() to browse routes directly",
                        )
                    )
                lines = ["## Product Features\n"]
                lines.append("| Name | Slug | Routes | Symbols | Has Changes |")
                lines.append("|------|------|--------|---------|-------------|")
                for row in rows:
                    name = row.get("name") or ""
                    slug = row.get("slug") or ""
                    symbol_count = row.get("symbol_count") or 0
                    has_changes = "Yes" if row.get("has_changes") else "No"
                    entry_routes_raw = row.get("entry_routes") or "[]"
                    try:
                        entry_routes = json.loads(entry_routes_raw) if isinstance(entry_routes_raw, str) else (entry_routes_raw or [])
                        route_count = len(entry_routes)
                    except Exception:
                        route_count = 0
                    lines.append(f"| {name} | `{slug}` | {route_count} | {symbol_count} | {has_changes} |")
                return "\n".join(lines) + _with_confidence(
                    "HIGH",
                    "Feature nodes are derived from route grouping and static analysis during indexing.",
                ) + _next_steps(
                    "Pass feature='<name>' to drill into a specific feature",
                    "Use laravelgraph_routes() for raw route listing",
                )
            else:
                # Drill into a specific feature
                slug_lower = feature.lower().replace(" ", "-")
                # Find matching feature
                feat_rows = db.execute(
                    "MATCH (f:Feature) WHERE f.slug = $slug OR toLower(f.name) CONTAINS toLower($feature) "
                    "RETURN f.node_id AS node_id, f.name AS name, f.slug AS slug, "
                    "f.entry_routes AS entry_routes, f.symbol_count AS symbol_count, f.has_changes AS has_changes "
                    "LIMIT 5",
                    {"slug": slug_lower, "feature": feature},
                )
                if not feat_rows:
                    elapsed = (time.perf_counter() - start) * 1000
                    _log_tool("laravelgraph_features", {"feature": feature}, 0, elapsed)
                    return (
                        f"No feature matching '{feature}' found."
                        + _next_steps(
                            "Call laravelgraph_features() with no arguments to list all features",
                        )
                    )
                feat = feat_rows[0]
                feat_name = feat.get("name") or feature
                feat_slug = feat.get("slug") or slug_lower
                entry_routes_raw = feat.get("entry_routes") or "[]"
                try:
                    entry_routes = json.loads(entry_routes_raw) if isinstance(entry_routes_raw, str) else (entry_routes_raw or [])
                except Exception:
                    entry_routes = []
                has_changes = "Yes" if feat.get("has_changes") else "No"
                symbol_count = feat.get("symbol_count") or 0

                lines = [f"## Feature: {feat_name}\n"]
                lines.append(f"- **Slug:** `{feat_slug}`")
                lines.append(f"- **Symbol count:** {symbol_count}")
                lines.append(f"- **Has changes:** {has_changes}")
                if entry_routes:
                    lines.append(f"- **Entry routes:** {', '.join(str(r) for r in entry_routes[:10])}")
                lines.append("")

                # Query all symbols BELONGS_TO_FEATURE this feature
                symbol_rows: list[dict] = []
                for label in ("Route", "EloquentModel", "Class_", "Event", "Job"):
                    try:
                        label_rows = db.execute(
                            f"MATCH (x:{label})-[:BELONGS_TO_FEATURE]->(f:Feature) "
                            "WHERE f.slug = $slug OR toLower(f.name) CONTAINS toLower($feature) "
                            "RETURN $label AS type, x.name AS name, x.fqn AS fqn "
                            "LIMIT 30",
                            {"slug": feat_slug, "feature": feature, "label": label},
                        )
                        symbol_rows.extend(label_rows)
                    except Exception:
                        pass

                if symbol_rows:
                    lines.append("### Constituent Symbols\n")
                    lines.append("| Type | Name | FQN |")
                    lines.append("|------|------|-----|")
                    for s in symbol_rows:
                        stype = s.get("type") or ""
                        sname = s.get("name") or ""
                        sfqn = s.get("fqn") or ""
                        lines.append(f"| {stype} | {sname} | `{sfqn}` |")
                else:
                    lines.append("_No BELONGS_TO_FEATURE edges found for this feature._")

                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_features", {"feature": feature}, len(symbol_rows), elapsed)
                return "\n".join(lines) + _with_confidence(
                    "MEDIUM",
                    "Feature membership is inferred by route grouping and call-graph traversal during indexing.",
                ) + _next_steps(
                    f"Use laravelgraph_request_flow(route='...') to trace a specific entry route end to end",
                    f"Use laravelgraph_impact(symbol='...') to see which symbols in this feature would be affected by a change",
                )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_features", {"feature": feature}, 0, elapsed)
            return f"Error listing features: {e}" + _with_confidence(
                "LOW",
                "Feature query did not complete. Feature nodes may not be present in this index.",
            ) + _next_steps(
                "Run laravelgraph analyze <project> to ensure features are indexed",
            )

    # ── Tool: laravelgraph_contracts ─────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_contracts(symbol: str = "", contract_type: str = "") -> str:
        """Browse behavioral contracts (validation rules, authorization policies, lifecycle hooks, mass-assignment guards) for any model, controller, or route.

        Args:
            symbol: Class name or partial FQN to filter contracts by source class
            contract_type: Contract type filter (e.g. 'validation', 'authorization', 'lifecycle', 'fillable')
        """
        db = _db()
        start = time.perf_counter()
        try:
            rows = db.execute(
                "MATCH (c:Contract) "
                "WHERE ($symbol = '' OR toLower(c.source_class) CONTAINS toLower($symbol)) "
                "  AND ($type = '' OR c.contract_type = $type) "
                "RETURN c.name AS name, c.contract_type AS contract_type, c.source_class AS source_class, "
                "       c.rules AS rules, c.file_path AS file_path, c.line_start AS line_start "
                "ORDER BY c.contract_type, c.source_class LIMIT 50",
                {"symbol": symbol, "type": contract_type},
            )
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_contracts", {"symbol": symbol, "contract_type": contract_type}, len(rows), elapsed)
            if not rows:
                hint = f" matching '{symbol}'" if symbol else ""
                hint += f" of type '{contract_type}'" if contract_type else ""
                return (
                    f"No Contract nodes found{hint}."
                    + _with_confidence(
                        "LOW",
                        "Contract nodes are extracted from FormRequests, policies, and model definitions during indexing.",
                    )
                    + _next_steps(
                        "Use laravelgraph_models() to see model fillable/guard definitions",
                        "Use laravelgraph_routes(filter='...') to find FormRequest-backed routes",
                    )
                )
            lines = ["## Behavioral Contracts\n"]
            lines.append("| Contract Name | Type | Source Class | Rules (summary) |")
            lines.append("|---------------|------|--------------|-----------------|")
            for row in rows:
                name = row.get("name") or ""
                ctype = row.get("contract_type") or ""
                src = row.get("source_class") or ""
                rules_raw = row.get("rules") or ""
                try:
                    rules_obj = json.loads(rules_raw) if isinstance(rules_raw, str) and rules_raw.startswith(("{", "[")) else rules_raw
                    rules_str = str(rules_obj)[:80]
                except Exception:
                    rules_str = str(rules_raw)[:80]
                lines.append(f"| {name} | `{ctype}` | `{src}` | {rules_str} |")

            # If a specific symbol was given, also show GOVERNS targets for first match
            if symbol and rows:
                first_name = rows[0].get("name") or ""
                governs: list[dict] = []
                for label in ("Class_", "Method", "Route", "EloquentModel"):
                    try:
                        g_rows = db.execute(
                            f"MATCH (c:Contract)-[:GOVERNS]->(x:{label}) WHERE c.name = $name "
                            "RETURN $label AS type, x.name AS target_name, x.fqn AS fqn LIMIT 10",
                            {"name": first_name, "label": label},
                        )
                        governs.extend(g_rows)
                    except Exception:
                        pass
                if governs:
                    lines.append(f"\n### Governed Targets for `{first_name}`\n")
                    lines.append("| Type | Target | FQN |")
                    lines.append("|------|--------|-----|")
                    for g in governs:
                        lines.append(f"| {g.get('type', '')} | {g.get('target_name', '')} | `{g.get('fqn', '')}` |")

            return "\n".join(lines) + _with_confidence(
                "HIGH",
                "Contract nodes are extracted from FormRequests, Policy classes, and Eloquent $fillable/$guarded during indexing.",
            ) + _next_steps(
                "Use laravelgraph_context(symbol='<FormRequest class>', include_source=True) to see full validation rules",
                "Use laravelgraph_request_flow(route='...') to trace the full HTTP request including validation",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_contracts", {"symbol": symbol, "contract_type": contract_type}, 0, elapsed)
            return f"Error querying contracts: {e}" + _with_confidence(
                "LOW",
                "Contract query failed. Contract nodes may not be present in this index version.",
            ) + _next_steps(
                "Use laravelgraph_models() to explore model definitions manually",
            )

    # ── Tool: laravelgraph_intent ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_intent(symbol: str) -> str:
        """Get structured intent analysis for any PHP method or class: what it does, what it reads/writes, side effects, and business rules enforced. Generated lazily by LLM and cached.

        Args:
            symbol: Class name, FQN, or method FQN (e.g. 'UserController', 'App\\Http\\Controllers\\UserController::store')
        """
        from laravelgraph.mcp.explain import read_source_snippet
        from laravelgraph.mcp.intent import generate_intent
        from laravelgraph.mcp.intent_cache import IntentCache

        db = _db()
        start = time.perf_counter()
        try:
            node = _resolve_symbol(db, symbol)
            if node is None:
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_intent", {"symbol": symbol}, 0, elapsed)
                return (
                    f"Symbol '{symbol}' not found in the graph."
                    + _with_confidence("LOW", "Symbol resolution failed — the FQN may be incomplete or the project not yet indexed.")
                    + _next_steps(
                        "Use laravelgraph_explain(feature='...') to find the correct symbol name",
                        "Use laravelgraph_query(query='...') to search by partial name",
                    )
                )

            node_id = node.get("node_id") or symbol
            file_path = node.get("file_path") or ""
            line_start = node.get("line_start") or 0
            line_end = node.get("line_end") or 0
            fqn = node.get("fqn") or node.get("name") or symbol

            # Read source snippet
            source = ""
            if file_path:
                source = read_source_snippet(file_path, line_start, line_end, project_root) or ""

            # Check intent cache
            intent_cache = IntentCache(index_dir(project_root))
            cached = intent_cache.get(node_id, file_path)
            if cached:
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_intent", {"symbol": symbol}, 1, elapsed)
                lines = [f"## Intent Analysis: `{fqn}`\n", "_(from cache)_\n"]
                lines.append(f"**Purpose:** {cached.get('purpose', '_unknown_')}\n")
                if cached.get("reads"):
                    lines.append("**Reads:**\n" + "\n".join(f"- {r}" for r in cached["reads"]))
                if cached.get("writes"):
                    lines.append("\n**Writes:**\n" + "\n".join(f"- {w}" for w in cached["writes"]))
                if cached.get("side_effects"):
                    lines.append("\n**Side Effects:**\n" + "\n".join(f"- {s}" for s in cached["side_effects"]))
                if cached.get("guards"):
                    lines.append("\n**Business Rules:**\n" + "\n".join(f"- {g}" for g in cached["guards"]))
                return "\n".join(lines) + _with_confidence(
                    "MEDIUM",
                    "Intent is LLM-generated from static source analysis. Runtime behavior may differ.",
                ) + _next_steps(
                    f"Use laravelgraph_context(symbol='{symbol}', include_source=True) to see the raw source",
                    f"Use laravelgraph_impact(symbol='{symbol}') to see downstream effects of this method",
                )

            # Cache miss — generate via LLM
            if not source:
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_intent", {"symbol": symbol}, 0, elapsed)
                return (
                    f"Cannot generate intent for `{symbol}`: source code not found at `{file_path}`."
                    + _with_confidence("LOW", "Source file could not be read.")
                    + _next_steps(
                        "Verify the file path is correct and the project root is accessible",
                    )
                )

            intent, model_used = generate_intent(fqn, source, cfg.llm)
            elapsed = (time.perf_counter() - start) * 1000

            if intent is None:
                _log_tool("laravelgraph_intent", {"symbol": symbol}, 0, elapsed)
                return (
                    f"Could not generate intent for `{symbol}`: {model_used}"
                    + _with_confidence(
                        "LOW",
                        "LLM intent generation failed. Check provider configuration.",
                    )
                    + _next_steps(
                        "Run `laravelgraph provider-status` to check LLM provider configuration",
                        f"Use laravelgraph_context(symbol='{symbol}', include_source=True) to read source manually",
                    )
                )

            # Store in cache
            intent_cache.set(node_id, intent, model_used, file_path)
            _log_tool("laravelgraph_intent", {"symbol": symbol}, 1, elapsed)

            lines = [f"## Intent Analysis: `{fqn}`\n", f"_(generated by {model_used})_\n"]
            lines.append(f"**Purpose:** {intent.get('purpose', '_unknown_')}\n")
            if intent.get("reads"):
                lines.append("**Reads:**\n" + "\n".join(f"- {r}" for r in intent["reads"]))
            if intent.get("writes"):
                lines.append("\n**Writes:**\n" + "\n".join(f"- {w}" for w in intent["writes"]))
            if intent.get("side_effects"):
                lines.append("\n**Side Effects:**\n" + "\n".join(f"- {s}" for s in intent["side_effects"]))
            if intent.get("guards"):
                lines.append("\n**Business Rules:**\n" + "\n".join(f"- {g}" for g in intent["guards"]))
            return "\n".join(lines) + _with_confidence(
                "MEDIUM",
                "Intent is LLM-generated from static source analysis. Runtime behavior may differ.",
            ) + _next_steps(
                f"Use laravelgraph_context(symbol='{symbol}', include_source=True) to see the raw source",
                f"Use laravelgraph_impact(symbol='{symbol}') to see downstream effects of this method",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_intent", {"symbol": symbol}, 0, elapsed)
            return f"Error generating intent for '{symbol}': {e}" + _with_confidence(
                "LOW",
                "Intent generation encountered an unexpected error.",
            ) + _next_steps(
                f"Use laravelgraph_context(symbol='{symbol}', include_source=True) to inspect the source manually",
            )

    # ── Tool: laravelgraph_test_coverage ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_test_coverage(symbol: str = "") -> str:
        """Show test coverage for a route, class, or feature — which test cases exercise it and what's untested.

        Args:
            symbol: Route URI, class name, or feature name to check coverage for (leave empty for summary)
        """
        db = _db()
        start = time.perf_counter()
        try:
            if not symbol:
                # Summary view
                total_tests = 0
                try:
                    t_rows = db.execute("MATCH (t:TestCase) RETURN count(t) AS total")
                    total_tests = t_rows[0].get("total", 0) if t_rows else 0
                except Exception:
                    pass

                uncovered_routes = 0
                total_routes = 0
                try:
                    total_rows = db.execute("MATCH (r:Route) RETURN count(r) AS total")
                    total_routes = total_rows[0].get("total", 0) if total_rows else 0
                except Exception:
                    pass
                try:
                    uncov_rows = db.execute(
                        "MATCH (r:Route) WHERE NOT EXISTS { MATCH (tc:TestCase)-[:TESTS]->(r) } RETURN count(r) AS uncovered"
                    )
                    uncovered_routes = uncov_rows[0].get("uncovered", 0) if uncov_rows else 0
                except Exception:
                    pass

                covered_routes = total_routes - uncovered_routes
                pct = round((covered_routes / total_routes) * 100, 1) if total_routes > 0 else 0.0

                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_test_coverage", {"symbol": symbol}, total_tests, elapsed)

                lines = ["## Test Coverage Summary\n"]
                lines.append(f"- **Total TestCases:** {total_tests}")
                lines.append(f"- **Total Routes:** {total_routes}")
                lines.append(f"- **Routes covered:** {covered_routes} ({pct}%)")
                lines.append(f"- **Routes uncovered:** {uncovered_routes}")
                return "\n".join(lines) + _with_confidence(
                    "MEDIUM",
                    "Coverage is inferred from TESTS edges extracted during static analysis. Dynamic coverage (Xdebug) is not included.",
                    ["Tests that exercise routes indirectly (e.g. via integration harness) may not be detected."],
                ) + _next_steps(
                    "Pass symbol='<route URI or class name>' to see tests for a specific target",
                    "Use laravelgraph_routes() to list all routes",
                )
            else:
                # Specific symbol coverage
                test_rows: list[dict] = []
                for label in ("Route", "Class_", "Feature", "Method"):
                    try:
                        rows = db.execute(
                            f"MATCH (tc:TestCase)-[:TESTS]->(x:{label}) "
                            "WHERE toLower(x.name) CONTAINS toLower($symbol) "
                            "   OR (x.uri IS NOT NULL AND toLower(x.uri) CONTAINS toLower($symbol)) "
                            "RETURN tc.name AS test_name, tc.file_path AS file_path, tc.test_type AS test_type, "
                            "       x.name AS target_name, $label AS target_type "
                            "LIMIT 30",
                            {"symbol": symbol, "label": label},
                        )
                        test_rows.extend(rows)
                    except Exception:
                        pass

                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_test_coverage", {"symbol": symbol}, len(test_rows), elapsed)

                if not test_rows:
                    return (
                        f"No test coverage found for '{symbol}'."
                        + _with_confidence(
                            "LOW",
                            "Either no TESTS edges exist for this target, or the symbol was not matched.",
                        )
                        + _next_steps(
                            "Use laravelgraph_suggest_tests(symbol='...') for test suggestions",
                            "Verify the symbol name with laravelgraph_query(query='...')",
                        )
                    )

                lines = [f"## Test Coverage: `{symbol}`\n"]
                lines.append(f"Found **{len(test_rows)}** covering test case(s).\n")
                lines.append("| Test Name | Type | File | Target |")
                lines.append("|-----------|------|------|--------|")
                for row in test_rows:
                    tname = row.get("test_name") or ""
                    ttype = row.get("test_type") or ""
                    tfile = row.get("file_path") or ""
                    ttarget = row.get("target_name") or ""
                    lines.append(f"| {tname} | {ttype} | `{tfile}` | {ttarget} |")

                # Also check for uncovered routes in a matching feature
                try:
                    uncov_rows = db.execute(
                        "MATCH (r:Route)-[:BELONGS_TO_FEATURE]->(f:Feature) "
                        "WHERE toLower(f.name) CONTAINS toLower($symbol) "
                        "  AND NOT EXISTS { MATCH (tc:TestCase)-[:TESTS]->(r) } "
                        "RETURN r.http_method AS method, r.uri AS uri LIMIT 20",
                        {"symbol": symbol},
                    )
                    if uncov_rows:
                        lines.append(f"\n### Uncovered Routes in Feature '{symbol}'\n")
                        for r in uncov_rows:
                            lines.append(f"- `{r.get('method', 'GET')} {r.get('uri', '')}`")
                except Exception:
                    pass

                return "\n".join(lines) + _with_confidence(
                    "MEDIUM",
                    "Coverage is inferred from TESTS edges extracted during static analysis.",
                    ["Integration or browser tests that exercise routes without explicit annotations may not appear."],
                ) + _next_steps(
                    "Use laravelgraph_suggest_tests(symbol='...') for gap analysis and suggestions",
                    "Use laravelgraph_request_flow(route='...') to understand what a route does before writing tests",
                )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_test_coverage", {"symbol": symbol}, 0, elapsed)
            return f"Error querying test coverage: {e}" + _with_confidence(
                "LOW",
                "Test coverage query failed. TestCase nodes may not be present in this index.",
            ) + _next_steps(
                "Verify the project is indexed with laravelgraph analyze",
            )

    # ── Tool: laravelgraph_performance_risks ─────────────────────────────────

    @mcp.tool()
    def laravelgraph_performance_risks(severity: str = "", symbol: str = "") -> str:
        """List detected N+1 query risks, missing eager loads, and raw query bypasses across the codebase.

        Args:
            severity: Filter by severity level ('HIGH', 'MEDIUM', 'LOW')
            symbol: Filter by method FQN containing this string
        """
        db = _db()
        start = time.perf_counter()
        try:
            rows = db.execute(
                "MATCH (m:Method)-[:HAS_PERFORMANCE_RISK]->(r:PerformanceRisk) "
                "WHERE ($severity = '' OR r.severity = $severity) "
                "  AND ($symbol = '' OR toLower(m.fqn) CONTAINS toLower($symbol)) "
                "RETURN m.fqn AS method_fqn, r.risk_type AS risk_type, r.severity AS severity, "
                "       r.description AS description, r.evidence AS evidence, "
                "       r.file_path AS file_path, r.line_number AS line_number "
                "ORDER BY r.severity DESC, r.risk_type LIMIT 50",
                {"severity": severity.upper() if severity else "", "symbol": symbol},
            )
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_performance_risks", {"severity": severity, "symbol": symbol}, len(rows), elapsed)

            if not rows:
                hint = ""
                if severity:
                    hint += f" with severity '{severity}'"
                if symbol:
                    hint += f" in '{symbol}'"
                return (
                    f"No performance risks detected{hint}."
                    + _with_confidence(
                        "MEDIUM",
                        "PerformanceRisk nodes are populated only when the performance analysis phase runs.",
                    )
                    + _next_steps(
                        "Use laravelgraph_race_conditions() to find concurrency risks",
                        "Use laravelgraph_context(symbol='...', include_source=True) to manually review hot methods",
                    )
                )

            # Group by risk_type
            by_type: dict[str, list[dict]] = {}
            for row in rows:
                rt = row.get("risk_type") or "UNKNOWN"
                by_type.setdefault(rt, []).append(row)

            lines = [f"## Performance Risks ({len(rows)} total)\n"]
            for risk_type, items in sorted(by_type.items()):
                lines.append(f"### {risk_type} ({len(items)})\n")
                lines.append("| Method | File | Severity | Evidence |")
                lines.append("|--------|------|----------|----------|")
                for item in items:
                    method = item.get("method_fqn") or ""
                    fp = item.get("file_path") or ""
                    sev = item.get("severity") or ""
                    evidence = (item.get("evidence") or item.get("description") or "")[:90]
                    lineno = item.get("line_number") or ""
                    loc = f"{fp}:{lineno}" if lineno else fp
                    lines.append(f"| `{method}` | `{loc}` | {sev} | {evidence} |")
                lines.append("")

            return "\n".join(lines) + _with_confidence(
                "MEDIUM",
                "Risks are detected via static analysis patterns. Runtime profiling may reveal additional hotspots.",
                ["Lazy-loading risks inside loops may not be detected if the loop is dynamically constructed."],
            ) + _next_steps(
                "Use laravelgraph_context(symbol='<method>', include_source=True) to review flagged methods",
                "Use laravelgraph_models() to check eager-load configuration on affected Eloquent models",
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_performance_risks", {"severity": severity, "symbol": symbol}, 0, elapsed)
            return f"Error querying performance risks: {e}" + _with_confidence(
                "LOW",
                "PerformanceRisk query failed. This node type may not be present in the current index.",
            ) + _next_steps(
                "Use laravelgraph_race_conditions() as an alternative concurrency/risk check",
            )

    # ── Tool: laravelgraph_api_surface ────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_api_surface(route: str = "", method: str = "") -> str:
        """Full API contract for a route: HTTP method+URI, input validation (FormRequest rules), output shape (Resource fields), auth policy, and side effects (events/jobs dispatched).

        Args:
            route: Route URI or partial URI to look up (e.g. '/api/users', 'orders')
            method: HTTP method filter (e.g. 'GET', 'POST', 'PUT', 'DELETE')
        """
        db = _db()
        start = time.perf_counter()
        try:
            if not route and not method:
                # List all routes that have at least one API contract annotation
                try:
                    annotated_rows = db.execute(
                        "MATCH (r:Route)-[:ROUTES_TO]->(m:Method) "
                        "WHERE EXISTS { MATCH (m)-[:VALIDATES_WITH]->(:FormRequest) } "
                        "   OR EXISTS { MATCH (m)-[:TRANSFORMS_WITH]->(:Resource) } "
                        "   OR EXISTS { MATCH (m)-[:AUTHORIZES_WITH]->(:Policy) } "
                        "RETURN r.http_method AS http_method, r.uri AS uri, r.name AS route_name "
                        "ORDER BY r.uri LIMIT 50"
                    )
                except Exception:
                    annotated_rows = []

                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_api_surface", {"route": route, "method": method}, len(annotated_rows), elapsed)

                if not annotated_rows:
                    return (
                        "No routes with FormRequest validation, Resource transformation, or Policy authorization found.\n"
                        "Pass route='<uri>' to inspect any specific route's surface."
                        + _with_confidence(
                            "LOW",
                            "API surface annotation requires VALIDATES_WITH, TRANSFORMS_WITH, or AUTHORIZES_WITH edges.",
                        )
                        + _next_steps(
                            "Use laravelgraph_routes() to list all routes",
                            "Pass route='<uri>' to laravelgraph_api_surface for a specific route contract",
                        )
                    )

                lines = ["## Routes with API Contract Annotations\n"]
                lines.append("| Method | URI | Route Name |")
                lines.append("|--------|-----|------------|")
                for row in annotated_rows:
                    lines.append(
                        f"| `{row.get('http_method', '')}` | `{row.get('uri', '')}` | {row.get('route_name', '')} |"
                    )
                return "\n".join(lines) + _with_confidence(
                    "HIGH",
                    "Only routes with explicit FormRequest/Resource/Policy bindings are shown here.",
                ) + _next_steps(
                    "Pass route='<uri>' to get the full API contract card for a specific route",
                )
            else:
                # Full API contract card for a specific route
                contract_rows = db.execute(
                    "MATCH (r:Route)-[:ROUTES_TO]->(m:Method) "
                    "WHERE ($route = '' OR toLower(r.uri) CONTAINS toLower($route)) "
                    "  AND ($method = '' OR r.http_method = toUpper($method)) "
                    "OPTIONAL MATCH (m)-[:VALIDATES_WITH]->(fr:FormRequest) "
                    "OPTIONAL MATCH (m)-[:TRANSFORMS_WITH]->(res:Resource) "
                    "OPTIONAL MATCH (m)-[:DISPATCHES]->(ev:Event) "
                    "OPTIONAL MATCH (m)-[:DISPATCHES]->(j:Job) "
                    "RETURN r.http_method AS http_method, r.uri AS uri, r.middleware_stack AS middleware_stack, "
                    "       m.fqn AS handler_fqn, fr.name AS form_request, fr.rules_summary AS rules_summary, "
                    "       res.name AS resource_name, ev.name AS event_name, j.name AS job_name "
                    "LIMIT 20",
                    {"route": route, "method": method},
                )

                if not contract_rows:
                    elapsed = (time.perf_counter() - start) * 1000
                    _log_tool("laravelgraph_api_surface", {"route": route, "method": method}, 0, elapsed)
                    return (
                        f"No route found matching uri='{route}'" + (f" method='{method}'" if method else "") + "."
                        + _with_confidence("LOW", "Route not found or not yet indexed.")
                        + _next_steps(
                            "Use laravelgraph_routes() to list all available routes",
                        )
                    )

                # Group results by route+handler (multiple OPTIONAL matches can produce duplicate rows)
                seen_routes: dict[str, dict] = {}
                for row in contract_rows:
                    key = f"{row.get('http_method', '')} {row.get('uri', '')}"
                    if key not in seen_routes:
                        seen_routes[key] = {
                            "http_method": row.get("http_method") or "",
                            "uri": row.get("uri") or "",
                            "middleware_stack": row.get("middleware_stack") or "",
                            "handler_fqn": row.get("handler_fqn") or "",
                            "form_request": row.get("form_request") or "",
                            "rules_summary": row.get("rules_summary") or "",
                            "resource_name": row.get("resource_name") or "",
                            "events": [],
                            "jobs": [],
                        }
                    entry = seen_routes[key]
                    if row.get("event_name") and row["event_name"] not in entry["events"]:
                        entry["events"].append(row["event_name"])
                    if row.get("job_name") and row["job_name"] not in entry["jobs"]:
                        entry["jobs"].append(row["job_name"])

                # Also query middleware and policy separately for each route
                lines = []
                for key, entry in seen_routes.items():
                    uri = entry["uri"]
                    http_method = entry["http_method"]
                    handler_fqn = entry["handler_fqn"]

                    # Fetch middleware
                    middleware: list[str] = []
                    try:
                        mw_raw = entry.get("middleware_stack") or ""
                        if mw_raw:
                            mw_list = json.loads(mw_raw) if isinstance(mw_raw, str) and mw_raw.startswith("[") else [mw_raw]
                            middleware = [str(m) for m in mw_list if m]
                    except Exception:
                        pass

                    # Fetch policy authorization
                    policies: list[str] = []
                    if handler_fqn:
                        try:
                            pol_rows = db.execute(
                                "MATCH (m:Method)-[:AUTHORIZES_WITH]->(p:Policy) WHERE m.fqn = $fqn "
                                "RETURN p.name AS policy_name, p.ability AS ability LIMIT 5",
                                {"fqn": handler_fqn},
                            )
                            for p in pol_rows:
                                ability = p.get("ability") or ""
                                pname = p.get("policy_name") or ""
                                policies.append(f"{pname}::{ability}" if ability else pname)
                        except Exception:
                            pass

                    lines.append(f"## API Contract: `{http_method} {uri}`\n")
                    lines.append(f"**Handler:** `{handler_fqn}`\n")

                    if middleware:
                        lines.append(f"**Middleware:** {', '.join(f'`{m}`' for m in middleware)}\n")

                    if entry["form_request"]:
                        rules = entry["rules_summary"] or "_see source_"
                        lines.append(f"**Validation (FormRequest):** `{entry['form_request']}`")
                        lines.append(f"  - Rules: {str(rules)[:200]}\n")

                    if entry["resource_name"]:
                        lines.append(f"**Output (Resource):** `{entry['resource_name']}`\n")

                    if policies:
                        lines.append(f"**Authorization (Policy):** {', '.join(f'`{p}`' for p in policies)}\n")

                    if entry["events"]:
                        lines.append(f"**Dispatched Events:** {', '.join(f'`{e}`' for e in entry['events'])}\n")

                    if entry["jobs"]:
                        lines.append(f"**Queued Jobs:** {', '.join(f'`{j}`' for j in entry['jobs'])}\n")

                    if not entry["form_request"] and not entry["resource_name"] and not policies and not entry["events"] and not entry["jobs"]:
                        lines.append("_No FormRequest, Resource, Policy, or dispatched side effects detected for this route._\n")

                    lines.append("---")

                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_api_surface", {"route": route, "method": method}, len(seen_routes), elapsed)
                return "\n".join(lines) + _with_confidence(
                    "HIGH",
                    "API contract is derived from static graph edges: VALIDATES_WITH, TRANSFORMS_WITH, AUTHORIZES_WITH, DISPATCHES.",
                    ["Dynamic route bindings and runtime-resolved middleware may not appear."],
                ) + _next_steps(
                    f"Use laravelgraph_request_flow(route='{uri}') for the full controller → service → DB chain",
                    "Use laravelgraph_context(symbol='<FormRequest class>', include_source=True) to see full validation rules",
                )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_api_surface", {"route": route, "method": method}, 0, elapsed)
            return f"Error querying API surface for route='{route}': {e}" + _with_confidence(
                "LOW",
                "API surface query failed. Route or handler nodes may be missing or the graph may be incomplete.",
            ) + _next_steps(
                "Use laravelgraph_routes() to browse routes manually",
                "Run laravelgraph analyze to re-index the project",
            )

    # ── Tool: laravelgraph_outbound_apis ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_outbound_apis(caller: str = "", url_contains: str = "") -> str:
        """Show all outbound HTTP calls made by this application — external API dependencies.

        Queries HttpClientCall nodes created by phase 32, which detects Laravel
        Http:: facade calls, Guzzle requests, and curl usage.  Use this to:
        - Understand what third-party services this app depends on
        - Find all callers before a planned API migration
        - Audit PCI/GDPR: which code sends data to external services?

        Args:
            caller:       Filter by calling class/method FQN (partial match)
            url_contains: Filter by URL pattern substring (e.g. 'stripe', 'sendgrid')
        """
        start = time.perf_counter()
        db = _db()
        try:
            rows = db.execute(
                "MATCH (h:HttpClientCall) RETURN "
                "h.caller_fqn AS caller, h.http_verb AS verb, "
                "h.url_pattern AS url, h.client_type AS client, "
                "h.file_path AS path, h.line_number AS line "
                "ORDER BY h.url_pattern LIMIT 200"
            )
            if not rows:
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_outbound_apis", {}, 0, elapsed)
                return (
                    "No outbound HTTP calls detected in the index.\n\n"
                    "This means either:\n"
                    "- The codebase has no Http::, Guzzle, or curl calls\n"
                    "- The index was built before phase 32 was added — re-run: `laravelgraph analyze`"
                )

            # Apply filters
            if caller:
                rows = [r for r in rows if caller.lower() in (r.get("caller") or "").lower()]
            if url_contains:
                rows = [r for r in rows if url_contains.lower() in (r.get("url") or "").lower()]

            if not rows:
                elapsed = (time.perf_counter() - start) * 1000
                _log_tool("laravelgraph_outbound_apis", {"caller": caller, "url_contains": url_contains}, 0, elapsed)
                return f"No outbound HTTP calls matching the filters (caller='{caller}', url_contains='{url_contains}')."

            # Group by URL domain/pattern for readability
            lines = [f"## Outbound API Calls ({len(rows)} found)\n"]
            for r in rows:
                verb    = r.get("verb") or "?"
                url     = r.get("url") or "?"
                c       = r.get("caller") or "?"
                client  = r.get("client") or "?"
                lineno  = r.get("line") or 0
                fpath   = r.get("path") or "?"
                lines.append(
                    f"- **[{verb}]** `{url}`\n"
                    f"  Caller: `{c}` ({fpath}:{lineno})  via: {client}"
                )

            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_outbound_apis", {"caller": caller, "url_contains": url_contains}, len(rows), elapsed)
            return "\n".join(lines)
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            _log_tool("laravelgraph_outbound_apis", {}, 0, elapsed)
            return (
                f"Error querying outbound APIs: {e}\n\n"
                "If the error mentions 'HttpClientCall', the index may predate phase 32 — "
                "run `laravelgraph analyze` to rebuild."
            )

    # ── Tool: laravelgraph_request_plugin ────────────────────────────────────

    @mcp.tool()
    def laravelgraph_request_plugin(description: str) -> str:
        """Request auto-generation of a new MCP tool plugin.

        Call this when you need specialized querying capability that the current
        tool set cannot provide. Describe what you need in plain English.

        The system will:
        1. Query the graph for relevant context
        2. Generate a plugin using the configured LLM
        3. Validate through 4 layers (AST + schema + execution + LLM judge)
        4. Retry up to 3 times with auto-critique if validation fails
        5. Deploy the plugin to .laravelgraph/plugins/ if it passes

        The plugin is available in your NEXT conversation (server restart required).
        If generation fails, you receive a detailed failure report.

        Example: laravelgraph_request_plugin("I need a tool that traces all refund
        flows from the route through to the database update, showing which models
        are involved and what events are dispatched")
        """
        import time as _time
        _t0 = _time.time()
        try:
            from laravelgraph.plugins.generator import generate_plugin
            from laravelgraph.plugins.meta import PluginMeta
            from datetime import datetime, timezone
            import re

            db = _db()
            code, status = generate_plugin(description, project_root, db, cfg)
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_request_plugin", {"description": description[:80]}, 1 if code else 0, elapsed)

            if not code:
                return (
                    f"# Plugin Generation Failed\n\n{status}\n\n"
                    f"**Next steps:**\n"
                    f"- Run `laravelgraph configure` to set up an LLM provider\n"
                    f"- Try a more specific description\n"
                    f"- Check `laravelgraph doctor` for issues"
                )

            # Extract plugin name from PLUGIN_MANIFEST
            import ast as _ast
            plugin_name = "generated-plugin"
            try:
                tree = _ast.parse(code)
                for node in _ast.walk(tree):
                    if isinstance(node, _ast.Assign):
                        for target in node.targets:
                            if isinstance(target, _ast.Name) and target.id == "PLUGIN_MANIFEST":
                                manifest = _ast.literal_eval(node.value)
                                plugin_name = manifest.get("name", plugin_name)
            except Exception:
                pass

            # Save plugin file
            plugins_dir = project_root / ".laravelgraph" / "plugins"
            plugins_dir.mkdir(exist_ok=True)
            plugin_path = plugins_dir / f"{plugin_name}.py"
            plugin_path.write_text(code, encoding="utf-8")

            # Register in meta store
            _meta_store.set(PluginMeta(
                name=plugin_name,
                status="active",
                created_at=datetime.now(timezone.utc).isoformat(),
            ))

            # Collect tool names for the success message
            _tool_names: list[str] = []
            try:
                import ast as _ast2
                _tree2 = _ast2.parse(code)
                for _n in _ast2.walk(_tree2):
                    if isinstance(_n, _ast2.Assign):
                        for _t in _n.targets:
                            if isinstance(_t, _ast2.Name) and _t.id == "PLUGIN_MANIFEST":
                                _m2 = _ast2.literal_eval(_n.value)
                                _pfx = _m2.get("tool_prefix", "").rstrip("_")
                                break
                if _pfx:
                    import re as _re2
                    for _match in _re2.finditer(r"^\s{4}def\s+(" + _re2.escape(_pfx) + r"\w+)\s*\(", code, _re2.MULTILINE):
                        _tool_names.append(_match.group(1))
            except Exception:
                pass

            _tool_list = "\n".join(f"  - `laravelgraph_run_plugin_tool(\"{plugin_name}\", \"{t}\")`" for t in _tool_names) or f"  - `laravelgraph_run_plugin_tool(\"{plugin_name}\", \"<tool_name>\")`"

            return (
                f"# Plugin Generated Successfully\n\n"
                f"**Name:** `{plugin_name}`\n"
                f"**File:** `.laravelgraph/plugins/{plugin_name}.py`\n"
                f"**Status:** {status}\n\n"
                f"## Use it RIGHT NOW (no restart needed)\n\n"
                f"Call any tool via hot dispatch:\n"
                f"{_tool_list}\n\n"
                f"## Next conversation\n\n"
                f"After MCP server restarts, native tools are registered automatically\n"
                f"and shown in the LOADED PLUGINS section of the server instructions.\n\n"
                f"**Manage it:**\n"
                f"- `laravelgraph plugin list` — see all plugins\n"
                f"- `laravelgraph plugin validate .laravelgraph/plugins/{plugin_name}.py` — re-validate\n"
                f"- `laravelgraph plugin disable {plugin_name}` — disable if not useful"
            )
        except Exception as exc:
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_request_plugin", {"description": description[:80]}, 0, elapsed)
            return _error_response("HIGH", f"Plugin request failed: {exc}")

    # ── Tool: laravelgraph_run_plugin_tool ───────────────────────────────────

    @mcp.tool()
    def laravelgraph_run_plugin_tool(
        plugin_name: str,
        tool_name: str,
        tool_args: "dict | None" = None,
    ) -> str:
        """Run any plugin tool on-demand — no server restart needed.

        This is the hot-dispatch mechanism for plugin tools. Use it to:

        1. Run a plugin tool IMMEDIATELY after generating it this conversation
           (without waiting for a server restart).
        2. Run any previously installed plugin tool at any time.

        How to find valid plugin names and tool names:
        - Check the "LOADED PLUGINS" section at the top of the server instructions.
        - Call laravelgraph_suggest_plugins() to list all installed plugins.
        - After laravelgraph_request_plugin(), the success message lists tool names.

        Args:
            plugin_name: The plugin slug, e.g. "user-explorer", "order-lifecycle".
                         Must match the name in PLUGIN_MANIFEST (also the filename
                         without .py in .laravelgraph/plugins/).
            tool_name:   The exact function name, e.g. "usr_summary", "order_flow".
                         Must start with the plugin's tool_prefix.
            tool_args:   Optional dict of keyword arguments to pass to the tool.
                         Required for tools that take parameters, e.g. store_discoveries.
                         Example: {"findings": "Users table has soft-deletes."}

        Returns:
            The tool's output as a string, or a detailed error message.

        Examples:
            laravelgraph_run_plugin_tool("user-explorer", "usr_summary")
            laravelgraph_run_plugin_tool("user-explorer", "usr_routes")
            laravelgraph_run_plugin_tool("order-lifecycle", "order_flow")
            laravelgraph_run_plugin_tool("webhook", "web_store_discoveries",
                                         {"findings": "POST /v1/payments/paypal-ipn has no auth"})
        """
        import time as _time
        _t0 = _time.time()
        try:
            from laravelgraph.plugins.loader import (
                _import_plugin_module,
                _ToolCollector,
                PluginSafeDB,
            )
            from laravelgraph.plugins.plugin_graph import DualDB

            plugin_path = project_root / ".laravelgraph" / "plugins" / f"{plugin_name}.py"
            if not plugin_path.exists():
                return (
                    f"# Plugin Not Found\n\n"
                    f"No plugin file at `.laravelgraph/plugins/{plugin_name}.py`.\n\n"
                    f"**To see available plugins:** call `laravelgraph_suggest_plugins()`\n"
                    f"**To generate a new plugin:** call `laravelgraph_request_plugin(description)`"
                )

            # Load the module fresh from disk
            module = _import_plugin_module(
                plugin_path,
                f"laravelgraph_hotrun_{plugin_name.replace('-', '_')}",
            )

            if not hasattr(module, "register_tools"):
                return f"Plugin `{plugin_name}` has no register_tools() function — it may be a pipeline plugin, not an MCP plugin."

            # Build db argument — same as what load_mcp_plugins provides
            safe_db = PluginSafeDB(_db(), plugin_name)
            if _plugin_db is not None:
                db_arg: Any = DualDB(lambda: safe_db, _plugin_db)
            else:
                db_arg = lambda: safe_db  # noqa: E731

            # Register tools into the collector
            collector = _ToolCollector()
            import inspect as _inspect
            sig = _inspect.signature(module.register_tools)
            reg_kwargs: dict = {}
            if "db" in sig.parameters:
                reg_kwargs["db"] = db_arg
            if "sql_db" in sig.parameters:
                reg_kwargs["sql_db"] = _sql_db

            module.register_tools(collector, **reg_kwargs)

            if tool_name not in collector.tools:
                available = sorted(collector.tools.keys())
                return (
                    f"# Tool Not Found in Plugin\n\n"
                    f"Plugin `{plugin_name}` has no tool named `{tool_name}`.\n\n"
                    f"**Available tools in this plugin:**\n"
                    + "\n".join(f"  - `laravelgraph_run_plugin_tool(\"{plugin_name}\", \"{t}\")`" for t in available)
                )

            # Call the tool (with optional kwargs for tools like store_discoveries)
            result = collector.tools[tool_name](**(tool_args or {}))
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_run_plugin_tool", {"plugin": plugin_name, "tool": tool_name}, 1, elapsed)
            return result if isinstance(result, str) else str(result)

        except Exception as exc:
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_run_plugin_tool", {"plugin": plugin_name, "tool": tool_name}, 0, elapsed)
            return _error_response("MEDIUM", f"Plugin tool execution failed: {exc}")

    # ── Tool: laravelgraph_update_plugin ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_update_plugin(name: str, critique: str) -> str:
        """Request regeneration of an existing plugin with a specific critique.

        Call this when an existing plugin tool isn't meeting your needs.
        Provide the exact plugin name and a specific description of what's wrong.

        The system regenerates and validates the plugin. If it passes, the plugin
        file is replaced immediately.

        Args:
            name: Plugin name (e.g. "payment-risk") — use laravelgraph_suggest_plugins() to list
            critique: What's wrong and what should be different
        """
        import time as _time
        _t0 = _time.time()
        try:
            plugins_dir = project_root / ".laravelgraph" / "plugins"
            plugin_path = plugins_dir / f"{name}.py"

            if not plugin_path.exists():
                return (
                    f"Plugin `{name}` not found. "
                    f"Use `laravelgraph plugin list` to see installed plugins."
                )

            # Get original description from manifest
            import ast as _ast
            source = plugin_path.read_text(encoding="utf-8")
            original_desc = name
            try:
                tree = _ast.parse(source)
                for node in _ast.walk(tree):
                    if isinstance(node, _ast.Assign):
                        for target in node.targets:
                            if isinstance(target, _ast.Name) and target.id == "PLUGIN_MANIFEST":
                                manifest = _ast.literal_eval(node.value)
                                original_desc = manifest.get("description", name)
            except Exception:
                pass

            from laravelgraph.plugins.generator import generate_plugin
            description = f"{original_desc}. CRITIQUE: {critique}"
            db = _db()
            code, status = generate_plugin(description, project_root, db, cfg)

            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_update_plugin", {"name": name}, 1 if code else 0, elapsed)

            if not code:
                return f"# Plugin Update Failed\n\n{status}"

            plugin_path.write_text(code, encoding="utf-8")

            # Update meta
            meta = _meta_store.get(name)
            if meta:
                meta.self_improvement_count += 1
                from datetime import datetime, timezone
                meta.last_improved_at = datetime.now(timezone.utc).isoformat()
                _meta_store.set(meta)

            return (
                f"# Plugin Updated\n\n"
                f"**Plugin:** `{name}`\n"
                f"**Status:** {status}\n\n"
                f"Changes take effect in your **next conversation**."
            )
        except Exception as exc:
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_update_plugin", {"name": name}, 0, elapsed)
            return _error_response("HIGH", f"Plugin update failed: {exc}")

    # ── Tool: laravelgraph_remove_plugin ─────────────────────────────────────

    @mcp.tool()
    def laravelgraph_remove_plugin(name: str, reason: str) -> str:
        """Remove a plugin that provides no real benefit.

        Call this when a plugin consistently fails to answer questions or returns
        irrelevant results. The reason is logged to prevent regenerating the same
        useless plugin in the future.

        Args:
            name: Plugin name to remove
            reason: Why this plugin is being removed (used to prevent future regeneration)
        """
        import time as _time
        _t0 = _time.time()
        try:
            plugins_dir = project_root / ".laravelgraph" / "plugins"
            plugin_path = plugins_dir / f"{name}.py"

            removed_file = False
            if plugin_path.exists():
                plugin_path.unlink()
                removed_file = True

            # Clean plugin graph data
            try:
                _plugin_db.delete_plugin_data(name)
            except Exception:
                pass

            # Log removal reason in meta (keep meta for history)
            meta = _meta_store.get(name)
            if meta:
                meta.removal_reasons.append(reason)
                meta.status = "disabled"
                _meta_store.set(meta)

            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_remove_plugin", {"name": name}, 1, elapsed)

            if not removed_file:
                return f"Plugin `{name}` not found. May already be removed."

            return (
                f"# Plugin Removed\n\n"
                f"**Plugin:** `{name}`\n"
                f"**Reason logged:** {reason}\n\n"
                f"Plugin file, graph data, and future auto-generation of this plugin are prevented.\n"
                f"Changes take effect in your **next conversation**."
            )
        except Exception as exc:
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_remove_plugin", {"name": name}, 0, elapsed)
            return _error_response("HIGH", f"Plugin removal failed: {exc}")

    # ── Tool: laravelgraph_suggest_plugins ───────────────────────────────────

    @mcp.tool()
    def laravelgraph_suggest_plugins() -> str:
        """Analyse the knowledge graph and suggest which plugins would add value to this project.

        Runs domain-signal detection across 7 built-in recipes (payment lifecycle,
        tenant isolation, booking state machine, subscription lifecycle, RBAC coverage,
        audit trail, feature-flags). Each recipe fires Cypher queries against the live
        graph and is recommended only when enough domain signals are found.

        Returns a ranked list of applicable plugins with evidence and scaffold commands.
        Use `laravelgraph plugin suggest` (CLI) or `laravelgraph plugin scaffold <name>`
        to generate the plugin skeleton.
        """
        import time as _time
        _t0 = _time.time()
        try:
            from laravelgraph.plugins.suggest import detect_applicable_recipes, format_suggestions
            db = _db()
            results = detect_applicable_recipes(db)
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_suggest_plugins", {}, len(results), elapsed)
            if not results:
                return (
                    "# Plugin Suggestions\n\n"
                    "No domain-specific plugin recipes matched this project's graph.\n\n"
                    "This is normal for generic CRUD applications. The 7 built-in recipes\n"
                    "cover: payment lifecycle, tenant isolation, booking state machines,\n"
                    "subscription lifecycle, RBAC coverage, audit trails, and feature-flags.\n\n"
                    "You can still create a custom plugin from scratch:\n"
                    "```\nlaravelgraph plugin scaffold my-plugin\n```"
                )
            return format_suggestions(results)
        except Exception as exc:
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_suggest_plugins", {}, 0, elapsed)
            return _error_response(
                "HIGH",
                f"Plugin suggestion failed: {exc}",
            ) + _next_steps(
                "Run `laravelgraph analyze` to ensure the graph is up to date",
                "Check `laravelgraph doctor` for any index issues",
            )

    # ── Tool: laravelgraph_plugin_knowledge ──────────────────────────────────

    @mcp.tool()
    def laravelgraph_plugin_knowledge(plugin_name: str = "") -> str:
        """Return domain discoveries accumulated by plugins across sessions.

        Each plugin can store findings via its ``store_discoveries`` tool. This
        tool surfaces everything that has been stored — domain patterns, team
        insights, identified issues — making institutional knowledge available
        to every future agent without needing to re-discover it.

        Args:
            plugin_name: Optional. Filter to a specific plugin's discoveries.
                         Leave empty to see all plugins' discoveries.

        Examples:
            laravelgraph_plugin_knowledge()
            laravelgraph_plugin_knowledge(plugin_name="user-explorer")
        """
        import time as _time
        _t0 = _time.time()
        try:
            # Use the already-open shared plugin graph connection (_plugin_db).
            # Creating a new Database object to the same .kuzu path would cause
            # KuzuDB write-lock conflicts and miss writes from the shared instance.
            _pg = _plugin_db

            if plugin_name:
                rows = _pg.execute(
                    "MATCH (n:PluginNode {plugin_source: $src}) "
                    "RETURN n.node_id AS id, n.label AS label, n.data AS data, "
                    "n.created_at AS created_at, n.updated_at AS updated_at "
                    "ORDER BY n.updated_at DESC",
                    params={"src": plugin_name},
                )
            else:
                rows = _pg.execute(
                    "MATCH (n:PluginNode) "
                    "RETURN n.node_id AS id, n.plugin_source AS plugin_source, "
                    "n.label AS label, n.data AS data, "
                    "n.created_at AS created_at, n.updated_at AS updated_at "
                    "ORDER BY n.updated_at DESC LIMIT 200"
                )

            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_plugin_knowledge", {"plugin_name": plugin_name or "all"}, len(rows), elapsed)

            if not rows:
                scope = f" for `{plugin_name}`" if plugin_name else ""
                return (
                    f"# Plugin Knowledge Base\n\n"
                    f"No discoveries stored{scope} yet.\n\n"
                    "Discoveries are accumulated when agents call the plugin's "
                    "`store_discoveries` tool during investigations. Run an investigation "
                    "using the plugin tools and call `store_discoveries` to begin building "
                    "the knowledge base."
                )

            import json as _json
            lines = [f"# Plugin Knowledge Base\n"]
            if plugin_name:
                lines.append(f"**Plugin:** `{plugin_name}`  |  **Discoveries:** {len(rows)}\n")
            else:
                # Group by plugin_source
                by_plugin: dict[str, list[dict]] = {}
                for row in rows:
                    src = row.get("plugin_source") or "unknown"
                    by_plugin.setdefault(src, []).append(row)
                lines.append(f"**Total discoveries:** {len(rows)} across {len(by_plugin)} plugin(s)\n")
                lines.append("")
                for src, src_rows in by_plugin.items():
                    lines.append(f"## {src}  ({len(src_rows)} discoveries)")
                    for row in src_rows[:20]:  # cap per plugin
                        label = row.get("label") or "Discovery"
                        updated = str(row.get("updated_at", ""))[:19]
                        try:
                            data_obj = _json.loads(row.get("data") or "{}")
                            data_str = _json.dumps(data_obj, indent=2)[:500]
                        except Exception:
                            data_str = str(row.get("data", ""))[:500]
                        lines.append(f"\n**[{label}]** _{updated}_")
                        lines.append(f"```json\n{data_str}\n```")
                    if len(src_rows) > 20:
                        lines.append(f"\n_...and {len(src_rows) - 20} more_")
                    lines.append("")
                return "\n".join(lines)

            for row in rows:
                label = row.get("label") or "Discovery"
                updated = str(row.get("updated_at", ""))[:19]
                try:
                    data_obj = _json.loads(row.get("data") or "{}")
                    data_str = _json.dumps(data_obj, indent=2)[:500]
                except Exception:
                    data_str = str(row.get("data", ""))[:500]
                lines.append(f"\n**[{label}]** _{updated}_")
                lines.append(f"```json\n{data_str}\n```")

            return "\n".join(lines)

        except Exception as exc:
            elapsed = (_time.time() - _t0) * 1000
            _log_tool("laravelgraph_plugin_knowledge", {"plugin_name": plugin_name or "all"}, 0, elapsed)
            return _error_response("MEDIUM", f"Plugin knowledge retrieval failed: {exc}")

    # ── Tool: laravelgraph_provider_status ───────────────────────────────────

    @mcp.tool()
    def laravelgraph_provider_status() -> str:
        """Show which LLM providers are configured for semantic summary generation.

        Returns the active provider, which API keys are set (not the keys themselves),
        which are missing, and which environment variables to set for each provider.
        Summaries are generated lazily on first query and cached — no cost until used.
        """
        from laravelgraph.mcp.summarize import PROVIDER_REGISTRY, provider_status

        status = provider_status(cfg.llm)
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

    # ── Project-specific plugin tools ────────────────────────────────────────
    from laravelgraph.plugins.loader import load_mcp_plugins

    _plugins_dir = project_root / ".laravelgraph" / "plugins"

    # Append active plugin system prompts to server instructions — log them for agent awareness
    _active_prompts = _meta_store.get_all_system_prompts()
    if _active_prompts:
        logger.info("Plugin system prompts active", count=len(_active_prompts))

    if _plugins_dir.exists():
        _loaded = load_mcp_plugins(
            _plugins_dir, mcp, logger,
            db_factory=_db,
            plugin_db=_plugin_db,
            meta_store=_meta_store,
            sql_db_factory=_sql_db,
        )
        if _loaded:
            logger.info("MCP plugin tools loaded", plugins=_loaded)

        # Run self-improvement check on startup (stats-based only — no LLM calls here)
        # Auto-generation via LLM is intentionally NOT run at startup: it is slow
        # (30+ seconds with local Ollama), blocks server readiness, and runs on every
        # restart without user intent. Use `laravelgraph plugin evolve` in CI/cron instead.
        try:
            from laravelgraph.plugins.self_improve import run_improvement_check_all
            _improved = run_improvement_check_all(_plugins_dir, _meta_store, project_root, cfg)
            for _pname, _ok, _msg in _improved:
                if _ok:
                    logger.info("Plugin self-improved on startup", plugin=_pname, message=_msg)
                else:
                    logger.warning("Plugin self-improvement failed on startup", plugin=_pname, message=_msg)
        except Exception as _e:
            logger.debug("Self-improvement check skipped", error=str(_e))

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

    try:
        results = db.execute(
            "MATCH (n:Class_) WHERE n.name = $s RETURN n.*, 'Class_' AS _label LIMIT 10",
            {"s": symbol},
        )
        if results:
            if len(results) == 1:
                return _normalize_node(results[0])

            def _class_rank(row: dict) -> tuple:
                fqn = row.get("n.fqn", "") or ""
                depth = fqn.count("\\")
                in_controllers = 1 if "Http\\Controllers\\" in fqn else 0
                in_models = 1 if "Models\\" in fqn else 0
                in_services = 1 if "Services\\" in fqn else 0
                return (-in_controllers, -in_models, -in_services, depth)
            results.sort(key=_class_rank)

            picked = results[0]
            node = _normalize_node(picked)

            match_details = []
            for r in results:
                r_fqn = r.get("n.fqn", "")
                r_nid = r.get("n.node_id", "")
                r_file = r.get("n.file_path", "")
                methods_cnt = 0
                routes_cnt = 0
                try:
                    mc = db.execute(
                        "MATCH (c:Class_)-[:DEFINES]->(m:Method) WHERE c.node_id = $nid "
                        "RETURN count(m) AS cnt",
                        {"nid": r_nid},
                    )
                    methods_cnt = mc[0].get("cnt", 0) if mc else 0
                except Exception:
                    pass
                try:
                    rc = db.execute(
                        "MATCH (r:Route)-[:ROUTES_TO]->(m:Method)<-[:DEFINES]-(c:Class_) "
                        "WHERE c.node_id = $nid RETURN count(r) AS cnt",
                        {"nid": r_nid},
                    )
                    routes_cnt = rc[0].get("cnt", 0) if rc else 0
                except Exception:
                    pass
                match_details.append({
                    "fqn": r_fqn,
                    "file": r_file,
                    "methods_count": methods_cnt,
                    "routes_count": routes_cnt,
                })

            alternatives = [m["fqn"] for m in match_details[1:] if m["fqn"]]
            alt_list = ", ".join(f"`{a}`" for a in alternatives)
            node["_disambiguation_warning"] = (
                f"Multiple classes named `{symbol}` exist: "
                f"`{picked.get('n.fqn', '')}` (shown), {alt_list}. "
                f"Use the full FQN to target a specific one."
            )
            node["_all_matches"] = match_details
            return node
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
