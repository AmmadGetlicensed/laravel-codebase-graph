"""LaravelGraph MCP server built with FastMCP.

Exposes all graph intelligence as MCP tools and resources for AI agents.
Supports both stdio and HTTP/SSE transports.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from laravelgraph.config import Config, index_dir
from laravelgraph.core.graph import GraphDB
from laravelgraph.logging import configure, get_logger, get_mcp_logger
from laravelgraph.mcp.cache import SummaryCache
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
- laravelgraph_feature_context: ONE CALL to get the complete picture of any feature — routes, controllers (with source), models, events, jobs, views, config. Start here for any feature exploration.
- laravelgraph_query: Search for symbols (classes, methods, routes, models)
- laravelgraph_context: Get 360° view of any symbol with source code and semantic summary
- laravelgraph_explain: Natural language explanation of how a feature works end-to-end
- laravelgraph_impact: Find blast radius of changes
- laravelgraph_routes: Explore the route table
- laravelgraph_models: Explore Eloquent model relationships
- laravelgraph_request_flow: Trace a complete HTTP request lifecycle
- laravelgraph_events: Explore the event/listener/job dispatch graph
- laravelgraph_dead_code: Find unreachable code
- laravelgraph_schema: Database schema from migrations
- laravelgraph_bindings: Service container binding map
- laravelgraph_config_usage: Config/env dependency map
- laravelgraph_detect_changes: Map git diff to affected symbols
- laravelgraph_suggest_tests: Find tests to run after a change
- laravelgraph_provider_status: Show which LLM providers are configured for semantic summaries
- laravelgraph_cypher: Raw Cypher graph queries (read-only)

IMPORTANT: For understanding any feature, ALWAYS call laravelgraph_feature_context FIRST.
It returns routes + controller source + models + events + config in a single call,
saving you from making 10+ individual tool calls.
""",
    )

    # Lazy semantic summary cache — stored in .laravelgraph/summaries.json
    _summary_cache = SummaryCache(index_dir(project_root))

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

    # ── Tool: laravelgraph_query ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_query(
        query: str,
        limit: int = 20,
        role_filter: str = "",
        file_filter: str = "",
    ) -> str:
        """Hybrid search (BM25 + semantic + fuzzy) across all indexed Laravel symbols.

        Args:
            query: Search query — symbol name, concept, or natural language phrase
            limit: Maximum results to return (default 20)
            role_filter: Filter by Laravel role (model|controller|middleware|job|event|listener|route|...)
            file_filter: Filter to symbols in files matching this path fragment
        """
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
                "RETURN t.name AS name, t.fqn AS fqn, d.dispatch_type AS dtype, d.is_queued AS queued LIMIT 10",
                {"id": node_id},
            )
            if dispatches:
                lines.append(f"### Dispatches ({len(dispatches)})")
                for d in dispatches:
                    dtype = d.get("dtype") or "event"
                    q = " *(queued)*" if d.get("queued") else ""
                    lines.append(f"- **{dtype}:** `{d.get('name', '?')}`{q}")
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
        filter_method: str = "",
        filter_uri: str = "",
        filter_middleware: str = "",
        limit: int = 50,
    ) -> str:
        """Full route map with middleware stacks, controller bindings, and parameters.

        Args:
            filter_method: Filter by HTTP method (GET|POST|PUT|PATCH|DELETE)
            filter_uri: Filter routes containing this URI fragment
            filter_middleware: Filter routes using this middleware
            limit: Max routes to return (default 50)
        """
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
    def laravelgraph_models(model_name: str = "") -> str:
        """Eloquent model relationship graph with foreign keys and pivot tables.

        Args:
            model_name: Optional — filter to a specific model (name or FQN). Omit for all models.
        """
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
            "Use laravelgraph_schema to see the database schema for model tables",
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

            # Trace calls from the controller method
            try:
                called = db.execute(
                    "MATCH (m:Method)-[r:CALLS]->(target) WHERE m.node_id = $id "
                    "RETURN target.fqn AS fqn, target._label AS label, r.confidence AS conf LIMIT 15",
                    {"id": f"method:{controller_fqn}::{action}"},
                )
                if called:
                    lines.append("")
                    lines.append("### 4. Called Services/Models")
                    for c in called:
                        lines.append(f"   - `{c.get('fqn', '?')}` ({c.get('label', '?')}, conf: {c.get('conf', '?')})")
            except Exception:
                pass

            # Events dispatched
            try:
                events = db.execute(
                    "MATCH (m:Method)-[:DISPATCHES]->(e:Event) WHERE m.fqn STARTS WITH $fqn "
                    "RETURN e.name AS event_name, e.fqn AS event_fqn LIMIT 10",
                    {"fqn": controller_fqn},
                )
                if events:
                    lines.append("")
                    lines.append("### 5. Events Dispatched")
                    for ev in events:
                        lines.append(f"   - `{ev.get('event_name')}` (`{ev.get('event_fqn')}`)")

                        # Listeners for this event
                        listeners = db.execute(
                            "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.fqn = $fqn "
                            "RETURN l.name AS listener LIMIT 5",
                            {"fqn": ev.get("event_fqn", "")},
                        )
                        for li in listeners:
                            lines.append(f"     → listener: `{li.get('listener')}`")
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
    def laravelgraph_schema(table_name: str = "") -> str:
        """Database schema graph with table relationships.

        Args:
            table_name: Optional — filter to a specific table
        """
        db = _db()
        start = time.perf_counter()

        try:
            if table_name:
                tables = db.execute(
                    "MATCH (t:DatabaseTable) WHERE t.name = $name RETURN t.* LIMIT 1",
                    {"name": table_name},
                )
            else:
                tables = db.execute("MATCH (t:DatabaseTable) RETURN t.* LIMIT 50")
        except Exception as e:
            return f"Error: {e}"

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_schema", {"table": table_name}, len(tables), elapsed)

        if not tables:
            return "No database tables found. Ensure migrations are present and the project has been indexed."

        lines = [f"## Database Schema ({len(tables)} tables)\n"]
        for t in tables:
            t_name = t.get("t.name", "?")
            lines.append(f"### `{t_name}`")
            try:
                cols = db.execute(
                    "MATCH (t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE t.name = $name "
                    "RETURN c.name AS col, c.type AS type, c.nullable AS nullable, "
                    "c.unique AS uniq, c.default_value AS default_val",
                    {"name": t_name},
                )
                if cols:
                    lines.append("| Column | Type | Nullable | Unique | Default |")
                    lines.append("|--------|------|----------|--------|---------|")
                    for col in cols:
                        nullable = "Yes" if col.get("nullable") else "No"
                        unique = "Yes" if col.get("uniq") else ""
                        default = col.get("default_val", "") or ""
                        lines.append(
                            f"| `{col.get('col')}` | {col.get('type', '?')} | {nullable} | {unique} | {default} |"
                        )
            except Exception:
                pass
            lines.append("")

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_models to see which Eloquent models correspond to these tables",
            "Use laravelgraph_context(ModelName) to see how a model uses its table",
        )

    # ── Tool: laravelgraph_events ─────────────────────────────────────────────

    @mcp.tool()
    def laravelgraph_events() -> str:
        """Full event → listener → job dispatch map."""
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

        if not events:
            return "No events found. Ensure the project has been indexed with event analysis."

        lines = [f"## Event → Listener → Job Map ({len(events)} events)\n"]
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
    def laravelgraph_config_usage(key: str) -> str:
        """Show all code depending on a config key or environment variable.

        Args:
            key: Config key (e.g. "app.name") or env variable name (e.g. "APP_KEY")
        """
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
        """End-to-end explanation of how a feature works: routes → controllers (with source) → events → models.

        Includes actual PHP source code for controller actions, docblock descriptions,
        event chains with listener source, and model relationship maps.
        Semantic summaries are cached — first call generates them, subsequent calls are instant.

        Args:
            feature: Feature or concept to explain (e.g. "user registration", "payment processing", "checkout")
        """
        db = _db()
        start = time.perf_counter()

        from laravelgraph.mcp.explain import (
            find_routes_for_feature,
            find_commands_for_feature,
            trace_method_flow,
            trace_event_chain,
            trace_model_summary,
        )
        from laravelgraph.search.hybrid import HybridSearch

        terms = [t.lower() for t in feature.split() if len(t) > 2]
        if not terms:
            terms = [feature.lower()]

        lines = [f"## How '{feature}' works\n"]

        # ── Routes matching the feature ──────────────────────────────────────
        matched_routes = find_routes_for_feature(db, terms)
        if matched_routes:
            lines.append(f"### HTTP Entry Points ({len(matched_routes)} routes)\n")
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

                    # Check summary cache for this controller method
                    method_nid = f"method:{ctrl}::{action}"
                    cached = _summary_cache.get(method_nid)
                    if cached:
                        lines.append(f"**Summary:** {cached}\n")
                    else:
                        trace_method_flow(db, ctrl, action, lines, project_root=project_root)
                        # Try to generate + cache the summary
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
                                    from laravelgraph.mcp.explain import read_source_snippet
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
                                                row["nid"], summary,
                                                provider_used,
                                                file_path=row.get("fp", ""),
                                            )
                            except Exception:
                                pass
                lines.append("")

        # ── Artisan commands matching the feature ────────────────────────────
        matched_commands = find_commands_for_feature(db, terms)
        if matched_commands:
            lines.append(f"### Artisan Commands ({len(matched_commands)})\n")
            for cmd in matched_commands[:3]:
                lines.append(f"- `{cmd.get('sig', cmd.get('name', '?'))}` — {cmd.get('desc', '')}")
            lines.append("")

        # ── Events matching the feature ───────────────────────────────────────
        try:
            events = db.execute(
                "MATCH (e:Event) RETURN e.node_id AS nid, e.name AS name, e.fqn AS fqn LIMIT 100"
            )
            matched_events = [
                e for e in events
                if any(t in (e.get("name") or "").lower() for t in terms)
            ]
            if matched_events:
                lines.append(f"### Events ({len(matched_events)})\n")
                for ev in matched_events[:3]:
                    trace_event_chain(
                        db, ev["nid"], ev.get("name", "?"), lines,
                        project_root=project_root,
                    )
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

        # ── Fallback: hybrid search if nothing matched above ──────────────────
        if not matched_routes and not matched_commands:
            try:
                search = HybridSearch(db, cfg.search)
                search.build_index()
                results = search.search(feature, limit=8)
                if results:
                    lines.append("### Related Symbols\n")
                    for r in results:
                        cached = _summary_cache.get(r.node_id or "", file_path=r.file_path or "")
                        summary_str = f" — {cached}" if cached else (f" — {r.snippet}" if r.snippet else "")
                        lines.append(f"- **{r.label}** `{r.fqn}`{summary_str}")
                    lines.append("")
            except Exception:
                pass

        elapsed = (time.perf_counter() - start) * 1000
        _log_tool("laravelgraph_explain", {"feature": feature}, 1, elapsed)

        if len(lines) <= 2:
            return (
                f"No components found for '{feature}'.\n\n"
                f"Try: laravelgraph_query('{feature}') to search for related symbols."
            )

        return "\n".join(lines) + _next_steps(
            "Use laravelgraph_feature_context(feature) for a structured full-picture view",
            "Use laravelgraph_context(ClassName) to inspect any specific class in depth",
            "Use laravelgraph_request_flow(route) to trace a specific route lifecycle",
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

                # Dispatches
                try:
                    disps = db.execute(
                        "MATCH (m:Method)-[d:DISPATCHES]->(t) WHERE m.fqn = $fqn "
                        "RETURN t.node_id AS nid, t.name AS name, t.fqn AS fqn, "
                        "d.dispatch_type AS dtype, d.is_queued AS queued",
                        {"fqn": ctrl_action},
                    )
                    for d in disps:
                        dtype = d.get("dtype") or "event"
                        q = " *(queued)*" if d.get("queued") else ""
                        lines.append(f"**Dispatches {dtype}:** `{d.get('name', '?')}`{q}")
                        if dtype == "event" and d.get("nid"):
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

                # Models touched (via calls to EloquentModel)
                try:
                    model_calls = db.execute(
                        "MATCH (m:Method)-[:CALLS]->(mdl:EloquentModel) WHERE m.fqn = $fqn "
                        "RETURN mdl.node_id AS nid, mdl.name AS name LIMIT 5",
                        {"fqn": ctrl_action},
                    )
                    for mc in model_calls:
                        if mc.get("nid"):
                            model_nids_seen.add(mc["nid"])
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

        # ── 5. Jobs matching feature ──────────────────────────────────────────
        try:
            jobs = db.execute(
                "MATCH (j:Job) RETURN j.name AS name, j.fqn AS fqn, j.queue AS queue, "
                "j.is_queued AS queued, j.tries AS tries, j.timeout AS timeout LIMIT 100"
            )
            matched_jobs = [j for j in jobs if any(t in (j.get("name") or "").lower() for t in terms)]
            if matched_jobs:
                lines.append(f"### Jobs ({len(matched_jobs)})\n")
                for j in matched_jobs[:5]:
                    q = f" (queue: `{j.get('queue')}`)" if j.get("queue") else ""
                    tries = f", tries: {j.get('tries')}" if j.get("tries") else ""
                    lines.append(f"- `{j.get('name', '?')}`{q}{tries}")
                lines.append("")
        except Exception:
            pass

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

        registry = Registry()
        repos = registry.all()

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
                top_stats = list(repo.stats.items())[:5]
                lines.append(f"- **Stats:** " + ", ".join(f"{k}: {v}" for k, v in top_stats))
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

    # 3. Class name match
    try:
        results = db.execute(
            "MATCH (n:Class_) WHERE n.name = $s RETURN n.*, 'Class_' AS _label LIMIT 1",
            {"s": symbol},
        )
        if results:
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

    # 5. FQN contains (partial match)
    for label in _fqn_labels:
        try:
            results = db.execute(
                f"MATCH (n:{label}) WHERE n.fqn CONTAINS $s RETURN n.*, '{label}' AS _label LIMIT 1",
                {"s": symbol},
            )
            if results:
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
