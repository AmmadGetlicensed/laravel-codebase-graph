"""LaravelGraph agent instruction installer.

Writes LaravelGraph agent files for AI coding tools.  Supports:

  - Claude Code  → .laravelgraph/agent.md  (rich agent file, single source of truth)
                 → .claude/agents/laravelgraph.md  (proper Claude Code subagent)
                 → CLAUDE.md  (minimal delegation block)
  - OpenCode     → .laravelgraph/agent.md  (rich agent file)
                 → .opencode/instructions.md  (full rich content inlined)
  - Cursor       → .laravelgraph/agent.md  (rich agent file)
                 → .cursorrules  (full rich content inlined)

Each config-file target gets a clearly-delimited ``<!-- laravelgraph-agent-instructions -->``
section that is idempotent — running the installer again replaces the section
in place rather than appending a second copy.

The rich agent file (.laravelgraph/agent.md and .claude/agents/laravelgraph.md) is
fully owned by the installer and rewritten on every run.  It includes both static
protocol content and dynamic project data (plugins, DB connections, features, stats)
collected from the graph at install time.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path

# ── Markers ────────────────────────────────────────────────────────────────────

_SECTION_START = "<!-- laravelgraph-agent-instructions-start -->"
_SECTION_END   = "<!-- laravelgraph-agent-instructions-end -->"

# ── Dynamic data collection ────────────────────────────────────────────────────

@dataclass
class DynamicData:
    """Project data collected from the graph at install time."""
    stats: dict[str, int] = field(default_factory=dict)
    plugins: list[dict] = field(default_factory=list)
    features: list[dict] = field(default_factory=list)
    db_connections: list[dict] = field(default_factory=list)
    graph_available: bool = False


def _collect_graph_stats(project_root: Path) -> dict[str, int]:
    db_path = project_root / ".laravelgraph" / "graph.kuzu"
    if not db_path.exists():
        return {}
    from laravelgraph.core.graph import GraphDB
    with GraphDB(db_path, read_only=True) as db:
        return db.stats()


def _collect_features(project_root: Path) -> list[dict]:
    db_path = project_root / ".laravelgraph" / "graph.kuzu"
    if not db_path.exists():
        return []
    from laravelgraph.core.graph import GraphDB
    with GraphDB(db_path, read_only=True) as db:
        try:
            return db.execute(
                "MATCH (f:Feature) "
                "RETURN f.name AS name, f.slug AS slug, "
                "f.route_prefix AS route_prefix, f.symbol_count AS symbol_count "
                "ORDER BY f.symbol_count DESC LIMIT 50"
            )
        except Exception:
            return []


def _collect_plugins(project_root: Path) -> list[dict]:
    plugins_dir = project_root / ".laravelgraph" / "plugins"
    if not plugins_dir.exists():
        return []
    from laravelgraph.plugins.loader import scan_plugin_manifests
    return scan_plugin_manifests(plugins_dir)


def _collect_db_connections(project_root: Path) -> list[dict]:
    try:
        from laravelgraph.config import Config
        config = Config.load(project_root)
        return [
            {"name": c.name, "driver": c.driver, "database": c.database}
            for c in config.databases
        ]
    except Exception:
        return []


def collect_dynamic_data(project_root: Path) -> DynamicData:
    """Collect all dynamic project data for injection into the agent file.

    Each source is wrapped in its own try/except so partial failures
    still produce a useful (partially populated) result.
    """
    data = DynamicData()

    try:
        data.stats = _collect_graph_stats(project_root)
        data.features = _collect_features(project_root)
        data.graph_available = bool(data.stats)
    except Exception:
        pass

    with contextlib.suppress(Exception):
        data.plugins = _collect_plugins(project_root)

    with contextlib.suppress(Exception):
        data.db_connections = _collect_db_connections(project_root)

    return data


# ── Dynamic section builder ────────────────────────────────────────────────────

_STAT_LABELS = [
    ("Route",            "Routes"),
    ("EloquentModel",    "Models"),
    ("Controller",       "Controllers"),
    ("Event",            "Events"),
    ("Job",              "Jobs"),
    ("Middleware",       "Middleware"),
    ("DatabaseTable",    "DB Tables"),
    ("StoredProcedure",  "Stored Procedures"),
    ("Class_",           "Classes"),
    ("Method",           "Methods"),
]


def _build_dynamic_section(data: DynamicData) -> str:
    lines: list[str] = []

    # ── Project Snapshot ──────────────────────────────────────────────────
    lines.append("### Project Snapshot\n")
    if data.graph_available and data.stats:
        rows = [(label, data.stats[node]) for node, label in _STAT_LABELS if node in data.stats]
        if rows:
            lines.append("| Metric | Count |")
            lines.append("|--------|------:|")
            for label, count in rows:
                lines.append(f"| {label} | {count:,} |")
        lines.append("")
    else:
        lines.append(
            "> Graph not yet indexed. Run `laravelgraph analyze .` to populate "
            "and then re-run `laravelgraph agent install .` to refresh this file.\n"
        )

    # ── Configured Database Connections ───────────────────────────────────
    if data.db_connections:
        lines.append("### Configured Database Connections\n")
        for conn in data.db_connections:
            db_name = conn.get("database") or "(not set)"
            lines.append(f"- **{conn['name']}** ({conn['driver']}): `{db_name}`")
        lines.append("")

    # ── Discovered Features ───────────────────────────────────────────────
    if data.features:
        lines.append("### Discovered Features\n")
        for feat in data.features:
            name = feat.get("name") or feat.get("slug") or "?"
            prefix = feat.get("route_prefix") or ""
            count = feat.get("symbol_count") or 0
            prefix_part = f" — route prefix: `{prefix}`" if prefix else ""
            lines.append(f"- **{name}**{prefix_part} ({count} symbols)")
        lines.append("")

    # ── Loaded Plugins ────────────────────────────────────────────────────
    if data.plugins:
        lines.append("### Loaded Plugins\n")
        for plugin in data.plugins:
            name = plugin.get("name", "?")
            desc = plugin.get("description", "")
            prefix = plugin.get("tool_prefix", "")
            tools = plugin.get("tool_names", [])
            tool_list = ", ".join(f"`{t}`" for t in tools) if tools else "(no tools)"
            prefix_part = f" (`{prefix}`)" if prefix else ""
            lines.append(f"- **{name}**{prefix_part} — {desc}")
            lines.append(f"  Tools: {tool_list}")
        lines.append("")

    return "\n".join(lines)


# ── Static protocol content ────────────────────────────────────────────────────

_STATIC_PROTOCOL = """\
### Decision Tree — Which Tool to Call

Use this to pick the right tool immediately. No exploration needed.

**Understanding a feature or flow:**
- "how does X work?" → `laravelgraph_feature_context(feature="X")` — single call, returns everything
- "what is X?" (vague) → `laravelgraph_explain(feature="X")` — semantic search finds the anchor
- "trace POST /api/X" → `laravelgraph_request_flow(route="/api/X")` — controller → service → DB
- "360° view of Class::method" → `laravelgraph_context(symbol="Class::method", include_source=True)`

**Finding callers / impact / relationships:**
- "what calls X?" → `laravelgraph_impact(symbol="X")` — shows callers (depth 1+)
- "what breaks if I change X?" → `laravelgraph_impact(symbol="X")` — full blast radius
- "what code touches column X?" → `laravelgraph_db_impact(table="T", operation="write")`
- "what tests cover X?" → `laravelgraph_suggest_tests(symbol="X")` or `laravelgraph_test_coverage(symbol="X")`

**Browsing the codebase:**
- "show me all routes" → `laravelgraph_routes()` or `laravelgraph_routes(filter="keyword")`
- "show me all models" → `laravelgraph_models()` or `laravelgraph_models(model_name="X")`
- "show me all events" → `laravelgraph_events()`
- "what features exist?" → `laravelgraph_features()` — auto-clustered product areas
- "what are the service bindings?" → `laravelgraph_bindings()`
- "what does a class/method do?" → `laravelgraph_intent(symbol="X")` — LLM summary, cached

**Database intelligence:**
- "what does table X look like?" → `laravelgraph_db_context(table="X")` — columns, FKs, code access
- "what values does column X hold?" → `laravelgraph_resolve_column(table="T", column="X")`
- "run SQL query" → `laravelgraph_db_query(sql="SELECT ...")`
- "show schema" → `laravelgraph_schema(table_name="X")`
- "what stored procedures exist?" → `laravelgraph_list_procedures()` or `laravelgraph_connection_map()`
- "explain stored procedure X" → `laravelgraph_procedure_context(name="X")`

**Security & performance:**
- "find auth gaps / unprotected routes" → `laravelgraph_security_surface()`
- "find N+1 queries" → `laravelgraph_performance_risks()` or `laravelgraph_performance_risks(severity="high")`
- "find race conditions" → `laravelgraph_race_conditions()`
- "audit route X" → `laravelgraph_api_surface(route="/api/X")` — middleware + validation + policy
- "find all auth rules" → `laravelgraph_contracts(symbol="X", contract_type="authorization")`

**Async / event-driven chains:**
- "trace job X" → `laravelgraph_job_chain(job="X", depth=5)` — job → events → listeners → jobs
- "what events does X dispatch?" → `laravelgraph_events()` then filter, or `laravelgraph_context(symbol="X")`

**Outbound / integrations:**
- "what external APIs does this call?" → `laravelgraph_outbound_apis()`
- "what calls Stripe?" → `laravelgraph_outbound_apis(url_contains="stripe")`
- "what config/env vars are used?" → `laravelgraph_config_usage(key="APP_KEY")`

**Plugins (domain-specific tools):**
- "suggest plugins" → `laravelgraph_suggest_plugins()`
- "generate plugin for X" → `laravelgraph_request_plugin("description of X domain")`
- "call plugin tool immediately" → `laravelgraph_run_plugin_tool("plugin-name", "tool_name")`
- "read past discoveries" → `laravelgraph_plugin_knowledge()`

---

### Complete Tool Reference

Every tool, its exact signature, what it returns, and when to use it.

#### Feature & Code Intelligence

**`laravelgraph_feature_context(feature: str) → str`**
PRIMARY TOOL. Returns routes + controller source + models + events + jobs + config for the named
feature in ONE call. Use for any "how does X work?" question. Call this FIRST before any other
tool. If it returns sparse results, escalate to `laravelgraph_explain`.

**`laravelgraph_explain(feature: str) → str`**
Semantic search. Phrases like "payment refund flow" work better than class names. Returns the
best anchor class/method with full context. Use when feature_context is sparse or you're unsure
where a feature lives. Multi-anchor: tries routes, service classes, events.

**`laravelgraph_context(symbol: str, include_source: bool = False) → str`**
360° view of any symbol (class, method, function). Returns: semantic summary, callers, callees,
Eloquent relationships, dispatched events/jobs, and file path/line numbers. Always pass
`include_source=True` when you need business logic (thresholds, hardcoded IDs, email
recipients, cache lock durations, switch/match maps).

**`laravelgraph_request_flow(route: str) → str`**
Traces a complete HTTP request from route URI to controller to services to DB. Shows middleware
stack, FormRequest, 3-hop deep service chain, events/jobs dispatched at every level. Use for
"trace this route" or "what happens when POST /api/orders is called".

**`laravelgraph_features(feature: str = "") → str`**
Auto-clustered product features. Shows Feature nodes (URI-segment clusters) with all their
routes, models, events, jobs. Call with no args to list all features. Pass a name to drill into
one. Use for "what are the main product features?" or onboarding.

**`laravelgraph_intent(symbol: str) → str`**
LLM-generated intent for a PHP method/class: purpose, what it reads, what it writes, side
effects, and business guards enforced. Generated lazily and cached. Use when you need a
human-readable explanation before editing. Costs one LLM call per symbol (cached after).

**`laravelgraph_query(query: str, limit: int = 20) → str`**
Hybrid BM25 + vector + fuzzy search across all indexed symbols. Use for keyword search when
you don't know what you're looking for. Less powerful than feature_context for feature exploration.

#### Impact & Relationships

**`laravelgraph_impact(symbol: str, depth: int = 3) → str`**
BFS blast radius: all callers and callees, grouped by depth. Shows what models, routes, events,
and DB tables are affected. ALWAYS call before renaming/modifying any method or class.

**`laravelgraph_db_impact(table: str, operation: str = "write", connection: str = "") → str`**
What code reads/writes a DB table, what events those writes dispatch, what jobs those events
trigger. Full chain. Call before changing any table column or schema. If 0 write sites, auto-
fallback searches linked Eloquent model methods.

**`laravelgraph_detect_changes(base: str = "HEAD~1", head: str = "HEAD") → str`**
Maps git diff to affected symbols, flows, and suggested tests. Pass diff text or base/head refs.
Use for PR impact analysis.

**`laravelgraph_suggest_tests(symbol: str) → str`**
Returns which test files and methods exercise a given symbol. Use before committing changes.

**`laravelgraph_test_coverage(symbol: str = "") → str`**
Shows TestCase nodes covering a route, class, or feature. Returns 0 for untested symbols.
Use before merging to flag untested code paths.

#### Browsing

**`laravelgraph_routes(filter: str = "", filter_method: str = "", filter_uri: str = "", filter_middleware: str = "") → str`**
All HTTP routes with HTTP method, URI, controller FQN, middleware stack, and action method.
Filter by keyword, HTTP verb, URI fragment, or middleware name.

**`laravelgraph_models(model_name: str = "") → str`**
All Eloquent models with their relationships (hasMany, belongsTo, etc.), fillable fields, and
linked DB table. Pass a name to filter. Note: model→table mapping is unreliable — verify with
`laravelgraph_db_context` when needed.

**`laravelgraph_events() → str`**
Full event → listener → job dispatch map plus scheduled task summary. Use to understand the
event-driven architecture of the app.

**`laravelgraph_bindings(abstract_filter: str = "") → str`**
Service container binding map: interfaces → implementations, where bound, how (bind/singleton/instance).

**`laravelgraph_contracts(symbol: str = "", contract_type: str = "") → str`**
Behavioral contracts for a class or route. Types: `validation` (FormRequest rules),
`authorization` (Policy methods), `lifecycle` (Observer hooks), `mass_assignment` ($fillable).
ALWAYS check before modifying a route or model — reveals implicit rules not in the controller.

**`laravelgraph_dead_code(role_filter: str = "", file_filter: str = "") → str`**
Unreachable/unused classes and methods. EXPENSIVE — don't run on every session. Use only when
cleaning up dead code.

**`laravelgraph_config_usage(key: str = "", symbol: str = "") → str`**
All code using a config key or env variable. Use to find everything that depends on a config
value before changing it.

**`laravelgraph_cross_cutting_concerns(min_call_sites: int = 3, min_layers: int = 2) → str`**
Methods called from many files across multiple architectural layers (traits, shared utilities).

**`laravelgraph_outbound_apis(caller: str = "", url_contains: str = "") → str`**
All outbound HTTP calls (Http:: facade, Guzzle, curl). Filter by caller class or URL pattern.
Use for "what external services does this app call?" or PCI/GDPR audits.

#### Database Intelligence

**`laravelgraph_db_context(table: str, connection: str = "") → str`**
Full semantic picture of a DB table: columns with types/nullability, FK relationships, inferred
relations, which code accesses this table, enum value distributions, and LLM-generated annotation.
Use before any DB schema change.

**`laravelgraph_resolve_column(table: str, column: str, connection: str = "") → str`**
Deep-dive into a single column: write-path evidence, guard conditions, polymorphic hints, live
value sample. Especially useful for `*_type`, `*_status`, `entity_id`, `owner_id` columns that
lack FK constraints.

**`laravelgraph_db_query(sql: str, connection: str = "", limit: int = 50) → str`**
Execute read-only SQL against the live database. Use for real production numbers: value
distributions, row counts, actual enum values in use. SELECT only — INSERT/UPDATE/DELETE blocked.

**`laravelgraph_schema(table_name: str = "", connection: str = "") → str`**
Full DB schema: tables, columns with types, indexes, foreign keys. Live DB introspection if
connected, migration fallback otherwise.

**`laravelgraph_connection_map() → str`**
All configured DB connections with table counts, procedure lists, views, and cross-database
query patterns. Use to understand what databases this app talks to.

**`laravelgraph_list_procedures(keyword: str = "", connection: str = "") → str`**
All stored procedures with modification dates, parameter counts, table access, and PHP references.

**`laravelgraph_procedure_context(name: str, connection: str = "") → str`**
Stored procedure body, which tables it reads/writes, and LLM-generated annotation.

**`laravelgraph_boundary_map(table: str = "") → str`**
Shows which PHP and stored procedure layers access a table and flags mixed-boundary conflicts.

**`laravelgraph_data_quality_report(connection: str = "", table_filter: str = "", min_rows: int = 100) → str`**
Data quality issues: NULL rates, orphaned FKs, suspicious columns. Needs live DB connection.

#### Security & Performance

**`laravelgraph_security_surface() → str`**
All auth middleware, gates, policies, unprotected routes, webhook verification gaps, and sensitive
data retention issues. Use for security audits.

**`laravelgraph_api_surface(route: str = "", method: str = "") → str`**
Full API contract for a route: middleware stack, FormRequest validation rules, output Resource
fields, Policy authorization, dispatched events/jobs. Use for API docs, security review, onboarding.

**`laravelgraph_performance_risks(severity: str = "", symbol: str = "") → str`**
N+1 queries, missing eager loads, repeated count() calls in loops, raw query bypasses. Run early
in any refactor. Filter by severity: `HIGH`, `MEDIUM`, `LOW`.

**`laravelgraph_race_conditions() → str`**
Check-then-act patterns that mutate shared counters without transaction or lock protection.

#### Async & Jobs

**`laravelgraph_job_chain(job: str, depth: int = 5) → str`**
Full execution chain from a Job or Artisan Command: Job → Events → Listeners → further Jobs/Events,
up to `depth` hops. Use for non-HTTP async flows.

#### Plugins

**`laravelgraph_suggest_plugins() → str`**
Domain-signal detection across 7 built-in recipes (payment, tenant, booking, subscription, RBAC,
audit, feature-flags). Returns ranked recommendations with evidence.

**`laravelgraph_request_plugin(description: str, allow_skeleton: bool = False) → str`**
Auto-generate a domain plugin from plain English. Validates through 4 layers. Use after
`suggest_plugins`. Call `run_plugin_tool` immediately after to use it this session.

**`laravelgraph_run_plugin_tool(plugin_name: str, tool_name: str, tool_args: dict = None) → str`**
Hot-dispatch: runs any plugin tool without server restart. Use immediately after generating a
plugin. Pass `tool_args={"findings": "..."}` for store_discoveries.

**`laravelgraph_update_plugin(name: str, critique: str) → str`**
Regenerate a plugin with a specific critique. Use when plugin output is wrong or shallow.

**`laravelgraph_remove_plugin(name: str, reason: str) → str`**
Remove a useless plugin and prevent auto-regeneration.

**`laravelgraph_plugin_knowledge(plugin_name: str = "") → str`**
Return all domain discoveries stored by plugins across past sessions. Read at session start for
domains that have plugins — avoids re-discovering known facts.

#### Utility

**`laravelgraph_cypher(query: str, graph: str = "core") → str`**
Raw Cypher for anything not covered by built-in tools. Always add LIMIT. Node labels use `_`
suffix: `Class_`, `Function_`, `Interface_`, `Trait_`. Route nodes use `http_method` (not
`method`) and `action_method` (not `action`). Use `graph="plugin"` to query plugin discoveries.

**`laravelgraph_provider_status() → str`**
Which LLM provider is configured for semantic summaries, which API keys are set.

---

### Investigation Recipes

**Recipe: Understand a feature end-to-end**
```
1. laravelgraph_feature_context(feature="booking")          # everything at once
2. laravelgraph_db_context(table="bookings")                # DB layer
3. laravelgraph_db_query("SELECT status, COUNT(*) FROM bookings GROUP BY status")  # real data
4. laravelgraph_context(symbol="BookingService::create", include_source=True)  # business logic
```

**Recipe: Before editing a method**
```
1. laravelgraph_impact(symbol="OrderService::processPayment")  # blast radius
2. laravelgraph_contracts(symbol="OrderService")               # implicit rules
3. laravelgraph_suggest_tests(symbol="OrderService::processPayment")  # tests to run
```

**Recipe: Debug "what touches this table?"**
```
1. laravelgraph_db_context(table="orders")      # who accesses it
2. laravelgraph_db_impact(table="orders")       # write chain: code → events → jobs
3. laravelgraph_resolve_column(table="orders", column="status")  # specific column
```

**Recipe: Security audit of a route**
```
1. laravelgraph_api_surface(route="/api/admin/users")   # full contract
2. laravelgraph_security_surface()                       # unprotected routes
3. laravelgraph_contracts(symbol="AdminController", contract_type="authorization")
```

**Recipe: PR review — what's the blast radius?**
```
1. laravelgraph_detect_changes(base="main")       # affected symbols
2. laravelgraph_impact(symbol="ChangedClass")     # blast radius per symbol
3. laravelgraph_suggest_tests(symbol="ChangedClass")  # tests to run
```

**Recipe: Onboarding a new developer**
```
1. laravelgraph_features()                    # all product areas
2. laravelgraph_routes()                      # all HTTP endpoints
3. laravelgraph_models()                      # data model
4. laravelgraph_events()                      # async architecture
5. laravelgraph_plugin_knowledge()            # accumulated team knowledge
```

**Recipe: Performance review**
```
1. laravelgraph_performance_risks(severity="high")   # worst N+1s
2. laravelgraph_race_conditions()                     # concurrency risks
3. laravelgraph_feature_context(feature="X")          # drill into hot path
4. laravelgraph_context(symbol="HotMethod", include_source=True)
```

---

### Tool Hierarchy (use in this order)

1. **Plugin tools** (e.g. `usr_summary`, `ord_routes`) — start here if a plugin
   covers the domain.  Plugin tools give pre-built, domain-specific answers.
   Check the `## LOADED PLUGINS` section at session start for what's available.
   Call `laravelgraph_plugin_knowledge()` to read accumulated discoveries first.

2. `laravelgraph_feature_context(feature="...")` — single call that returns
   routes + controller source + models + events + jobs for a whole feature area.
   Use this as your first call for any feature investigation.

3. `laravelgraph_explain(feature="...")` — semantic search; finds the best
   anchor class/method when you're not sure where a feature lives.  Phrase it
   like a human question: "how does payment refund work?" not "PaymentRefund".

4. `laravelgraph_context(symbol="Foo::bar", include_source=True)` — 360° view
   of a single symbol: callers, callees, Eloquent relationships, dispatched
   events/jobs, and the actual PHP source.  Always pass `include_source=True`
   when you need to see business logic (thresholds, hardcoded IDs, conditions).

5. `laravelgraph_request_flow(route="/api/path")` — traces a route from
   controller through services, events, and jobs (3 hops deep).  Use when you
   need the full request call chain.

### What Tools Cannot Do

- **Cannot modify the Laravel codebase** — all tools are read-only graph queries.
- **Cannot run tests or execute PHP** — analysis is static + live DB introspection only.
- **`laravelgraph_dead_code` is expensive** — do not run on every session.
- **`laravelgraph_cypher` does not auto-handle label quirks** — built-in tools do.
- **`laravelgraph_explain` is semantic** — "explain payment refund flow" works better than "PaymentRefund".
- **Graph reflects the last `laravelgraph analyze` run** — re-index after significant code changes.

### Investigation Protocol

- **Never read PHP files manually** when the graph can answer. File reads cost more context.
- **Never stop at empty results** — escalate: feature_context → explain → context(include_source).
- Use `laravelgraph_impact(symbol="...")` before changing any method or class.
- Use `laravelgraph_db_impact(table="...")` before changing a DB column.
- For security reviews: `laravelgraph_security_surface()` then `laravelgraph_api_surface()` on suspicious routes.
- For performance reviews: `laravelgraph_performance_risks()` then `laravelgraph_race_conditions()`.
- Combine code + data: feature_context → db_context → db_query → context(include_source). Code tells logic, data tells reality.

### Common Pitfalls

- **Route property names**: KuzuDB Route nodes use `http_method` (not `method`)
  and `action_method` (not `action`) in Cypher queries.
- **`laravelgraph_feature_context` is ONE call** — don't chain
  `laravelgraph_routes` + `laravelgraph_models` + `laravelgraph_events`
  separately when feature_context returns all of them together.
- **Ambiguous class names**: when a tool shows "AMBIGUOUS NAME", use the full
  FQN shown in the warning to target the exact class.
- **`laravelgraph_explain` is semantic** — phrase queries as human questions, not class names.
- **Model→table names are unreliable** — always verify via `laravelgraph_models` or `laravelgraph_db_context`.
- **`laravelgraph_intent` costs one LLM call** per symbol but is cached forever after.

### Plugin Workflow

When a domain has no plugin yet:

```
1. laravelgraph_suggest_plugins()          # see what's recommended
2. laravelgraph_request_plugin("domain")   # generate a plugin (needs LLM)
3. laravelgraph_run_plugin_tool("slug", "prefix_summary")  # call it immediately
```

After the next server restart, the plugin's tools are native MCP tools listed
in `## LOADED PLUGINS` and callable directly without `laravelgraph_run_plugin_tool`.

### store_discoveries Protocol (IMPORTANT)

After any substantive investigation, call the domain plugin's `store_discoveries`
tool with a plain-text summary of what you found:

```
usr_store_discoveries(findings="Users table has soft-deletes enabled. Admin flag
  is set via role_id FK to roles table, not a boolean column. Password reset uses
  custom token table, not Laravel's built-in password_resets.")
```

These findings persist across sessions. Future agents read them via
`laravelgraph_plugin_knowledge()` without re-running the analysis.

**Call `store_discoveries` after every investigation** — even a single insight saves
the next agent from re-discovering it.

### Plugin Knowledge Recall

At the start of any session involving a feature that has a plugin:

```
laravelgraph_plugin_knowledge()                    # all accumulated discoveries
laravelgraph_plugin_knowledge(plugin_name="slug")  # discoveries for one plugin
```

Read these before doing fresh analysis — the answer may already be stored.

### Keeping Plugin Knowledge Current (CI/cron)

```bash
laravelgraph plugin evolve . --max-generate 2   # weekly auto-generation
laravelgraph plugin evolve . --dry-run           # preview what would be generated
```
"""


# ── Content builders ───────────────────────────────────────────────────────────

def build_rich_agent_body(data: DynamicData) -> str:
    """Return the full agent body: dynamic project snapshot + static protocol."""
    dynamic = _build_dynamic_section(data)
    return (
        "## LaravelGraph — Agent Protocol\n\n"
        "This project is indexed by LaravelGraph. You have access to a complete knowledge\n"
        "graph of every PHP class, method, route, model, event, job, database table, and\n"
        "their relationships. **Query the graph before reading files.**\n\n"
        + dynamic
        + _STATIC_PROTOCOL
    )


def build_rich_agent_file(data: DynamicData) -> str:
    """Return the full `.laravelgraph/agent.md` content (no YAML frontmatter)."""
    return (
        "# LaravelGraph Agent\n\n"
        "> Generated by `laravelgraph agent install`. Re-run after upgrading LaravelGraph "
        "or re-indexing the project to refresh dynamic data.\n\n"
        + build_rich_agent_body(data)
        + "\n"
    )


def build_claude_agent_file(data: DynamicData) -> str:
    """Return the full `.claude/agents/laravelgraph.md` content with YAML frontmatter."""
    frontmatter = """\
---
name: laravelgraph
description: >
  Background research agent for Laravel/PHP codebase intelligence via the LaravelGraph
  knowledge graph. This agent is READ-ONLY — it only calls MCP graph tools, never reads
  or edits files, and never runs shell commands. Safe to spawn in parallel with any
  coding task without conflict.

  SPAWN THIS AGENT for any of the following:
  - Understanding how a feature, flow, or domain works end-to-end
  - Finding what classes/methods call a given symbol
  - Assessing blast radius before changing code (what will break?)
  - Tracing an HTTP request from route through controller to database
  - Investigating database table structure, column semantics, or live data distributions
  - Reviewing N+1 query risks, performance problems, or race conditions
  - Auditing authentication / authorization coverage on routes
  - Mapping events, queued jobs, and async dispatch chains
  - Understanding what a class or method does before editing it
  - PR review: what's the impact of these changed files?
  - Onboarding: what are the main features and their routes?
  - Security audit: which routes lack auth? where is sensitive data retained?
  - Generating or managing domain plugins for a product area
  - Reading accumulated team discoveries stored by past agents

  <example>user: "how does the payment flow work?"</example>
  <example>user: "what calls UserService::createUser?"</example>
  <example>user: "what will break if I rename this method?"</example>
  <example>user: "trace the POST /api/orders request end to end"</example>
  <example>user: "what does the orders table look like?"</example>
  <example>user: "show me all routes that don't have auth middleware"</example>
  <example>user: "find N+1 queries in this codebase"</example>
  <example>user: "what events are dispatched when an order is placed?"</example>
  <example>user: "what's the blast radius of changing Order::calculateTotal?"</example>
  <example>user: "generate a plugin for the subscription domain"</example>
  <example>user: "what are the main features of this app?"</example>
  <example>user: "review this PR for performance and security issues"</example>
  <example>user: "what does this class actually do?"</example>
  <example>user: "which tests cover this method?"</example>
  <example>user: "what external APIs does this app call?"</example>
  <example>user: "what stored procedures exist and what do they do?"</example>
  <example>user: "show me the service container bindings"</example>
  <example>user: "what config/env vars does this feature depend on?"</example>
allowedTools:
  - mcp__laravelgraph__laravelgraph_feature_context
  - mcp__laravelgraph__laravelgraph_explain
  - mcp__laravelgraph__laravelgraph_context
  - mcp__laravelgraph__laravelgraph_request_flow
  - mcp__laravelgraph__laravelgraph_impact
  - mcp__laravelgraph__laravelgraph_db_impact
  - mcp__laravelgraph__laravelgraph_detect_changes
  - mcp__laravelgraph__laravelgraph_suggest_tests
  - mcp__laravelgraph__laravelgraph_test_coverage
  - mcp__laravelgraph__laravelgraph_routes
  - mcp__laravelgraph__laravelgraph_models
  - mcp__laravelgraph__laravelgraph_events
  - mcp__laravelgraph__laravelgraph_features
  - mcp__laravelgraph__laravelgraph_bindings
  - mcp__laravelgraph__laravelgraph_contracts
  - mcp__laravelgraph__laravelgraph_intent
  - mcp__laravelgraph__laravelgraph_dead_code
  - mcp__laravelgraph__laravelgraph_config_usage
  - mcp__laravelgraph__laravelgraph_cross_cutting_concerns
  - mcp__laravelgraph__laravelgraph_outbound_apis
  - mcp__laravelgraph__laravelgraph_db_context
  - mcp__laravelgraph__laravelgraph_resolve_column
  - mcp__laravelgraph__laravelgraph_db_query
  - mcp__laravelgraph__laravelgraph_schema
  - mcp__laravelgraph__laravelgraph_connection_map
  - mcp__laravelgraph__laravelgraph_list_procedures
  - mcp__laravelgraph__laravelgraph_procedure_context
  - mcp__laravelgraph__laravelgraph_boundary_map
  - mcp__laravelgraph__laravelgraph_data_quality_report
  - mcp__laravelgraph__laravelgraph_security_surface
  - mcp__laravelgraph__laravelgraph_api_surface
  - mcp__laravelgraph__laravelgraph_performance_risks
  - mcp__laravelgraph__laravelgraph_race_conditions
  - mcp__laravelgraph__laravelgraph_job_chain
  - mcp__laravelgraph__laravelgraph_query
  - mcp__laravelgraph__laravelgraph_cypher
  - mcp__laravelgraph__laravelgraph_provider_status
  - mcp__laravelgraph__laravelgraph_suggest_plugins
  - mcp__laravelgraph__laravelgraph_request_plugin
  - mcp__laravelgraph__laravelgraph_run_plugin_tool
  - mcp__laravelgraph__laravelgraph_update_plugin
  - mcp__laravelgraph__laravelgraph_remove_plugin
  - mcp__laravelgraph__laravelgraph_plugin_knowledge
model: inherit
---

"""
    return frontmatter + build_rich_agent_body(data) + "\n"


def build_minimal_block() -> str:
    """Return the assertive CLAUDE.md delegation block."""
    return (
        f"{_SECTION_START}\n\n"
        "## LaravelGraph — Query the Graph BEFORE Reading PHP Files\n\n"
        "This project is indexed by LaravelGraph: a complete knowledge graph of every "
        "PHP class, method, route, model, event, job, and database table. "
        "Graph queries are faster than file reads and see cross-file relationships that grep misses.\n\n"
        "**RULE: Before opening any PHP file or running grep/find on this codebase, "
        "check if a LaravelGraph MCP tool answers the question.**\n\n"
        "### When the user asks → call this tool first\n\n"
        "| Question / Task | Tool to call |\n"
        "|---|---|\n"
        "| \"how does [feature] work?\" | `laravelgraph_feature_context(feature=\"...\")` |\n"
        "| \"what calls this method/class?\" | `laravelgraph_impact(symbol=\"...\")` |\n"
        "| \"what breaks if I change X?\" | `laravelgraph_impact(symbol=\"...\")` |\n"
        "| \"trace this HTTP request\" | `laravelgraph_request_flow(route=\"/api/...\")` |\n"
        "| \"360° view of a class\" | `laravelgraph_context(symbol=\"...\", include_source=True)` |\n"
        "| \"what does this class/method do?\" | `laravelgraph_intent(symbol=\"...\")` |\n"
        "| \"show me all routes\" | `laravelgraph_routes()` |\n"
        "| \"show me all models\" | `laravelgraph_models()` |\n"
        "| \"what events are dispatched?\" | `laravelgraph_events()` |\n"
        "| \"what DB tables exist / what does table X look like?\" | `laravelgraph_db_context(table=\"...\")` |\n"
        "| \"find N+1 queries / performance risks\" | `laravelgraph_performance_risks()` |\n"
        "| \"which routes have no auth?\" | `laravelgraph_security_surface()` |\n"
        "| \"what code touches this DB column?\" | `laravelgraph_db_impact(table=\"...\")` |\n"
        "| \"what external APIs does this call?\" | `laravelgraph_outbound_apis()` |\n"
        "| \"PR review: what's the blast radius?\" | `laravelgraph_detect_changes(base=\"main\")` |\n"
        "| \"what are the main product features?\" | `laravelgraph_features()` |\n\n"
        "### Triggers — always use LaravelGraph MCP tools when:\n\n"
        "- Asked to explain, understand, or map how anything works in this codebase\n"
        "- About to edit a method or class (check impact first)\n"
        "- Debugging something that touches models, routes, events, or jobs\n"
        "- Reviewing a PR for impact, security, or performance\n"
        "- Onboarding to an unfamiliar part of the app\n"
        "- Any question about the database schema or live data\n\n"
        "### For complex multi-step investigations\n\n"
        "Delegate to the **laravelgraph** subagent — it has complete protocol knowledge, "
        "a full tool reference, and investigation recipes pre-loaded. It is read-only and "
        "safe to spawn in parallel with any coding task.\n\n"
        f"{_SECTION_END}"
    )


# ── Upsert helper ──────────────────────────────────────────────────────────────

def _upsert_section(target: Path, block: str) -> None:
    """Replace the LaravelGraph section in *target*, or append it if absent.

    Idempotent — running multiple times leaves exactly one section.
    """
    existing = target.read_text(encoding="utf-8") if target.exists() else ""

    if _SECTION_START in existing and _SECTION_END in existing:
        before = existing[: existing.index(_SECTION_START)]
        after  = existing[existing.index(_SECTION_END) + len(_SECTION_END):]
        new_content = before + block + after
    else:
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ("\n" if existing else "")
        new_content = existing + sep + block + "\n"

    target.write_text(new_content, encoding="utf-8")


# ── File writers ───────────────────────────────────────────────────────────────

def _write_rich_agent_file(project_root: Path, data: DynamicData) -> Path:
    """Write `.laravelgraph/agent.md` — the single source of truth for agent content."""
    target = project_root / ".laravelgraph" / "agent.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_rich_agent_file(data), encoding="utf-8")
    return target


def _write_claude_agent_file(project_root: Path, data: DynamicData) -> Path:
    """Write `.claude/agents/laravelgraph.md` — proper Claude Code subagent."""
    target = project_root / ".claude" / "agents" / "laravelgraph.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(build_claude_agent_file(data), encoding="utf-8")
    return target


# ── Install targets ────────────────────────────────────────────────────────────

def install_for_claude_code(project_root: Path, data: DynamicData | None = None) -> list[Path]:
    """Install for Claude Code.

    Writes three files:
      - .laravelgraph/agent.md         (rich agent reference file)
      - .claude/agents/laravelgraph.md  (proper Claude Code subagent)
      - CLAUDE.md                       (minimal delegation block)
    """
    if data is None:
        data = collect_dynamic_data(project_root)

    written = []
    written.append(_write_rich_agent_file(project_root, data))
    written.append(_write_claude_agent_file(project_root, data))
    _upsert_section(project_root / "CLAUDE.md", build_minimal_block())
    written.append(project_root / "CLAUDE.md")
    return written


def install_for_opencode(project_root: Path, data: DynamicData | None = None) -> list[Path]:
    """Install for OpenCode.

    Writes two files:
      - .laravelgraph/agent.md               (rich agent reference file)
      - .opencode/instructions.md             (full rich content inlined)
    """
    if data is None:
        data = collect_dynamic_data(project_root)

    written = []
    written.append(_write_rich_agent_file(project_root, data))
    full_block = (
        f"{_SECTION_START}\n\n"
        + build_rich_agent_body(data)
        + f"\n{_SECTION_END}"
    )
    target = project_root / ".opencode" / "instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    _upsert_section(target, full_block)
    written.append(target)
    return written


def install_for_cursor(project_root: Path, data: DynamicData | None = None) -> list[Path]:
    """Install for Cursor.

    Writes two files:
      - .laravelgraph/agent.md   (rich agent reference file)
      - .cursorrules              (full rich content inlined)
    """
    if data is None:
        data = collect_dynamic_data(project_root)

    written = []
    written.append(_write_rich_agent_file(project_root, data))
    full_block = (
        f"{_SECTION_START}\n\n"
        + build_rich_agent_body(data)
        + f"\n{_SECTION_END}"
    )
    target = project_root / ".cursorrules"
    _upsert_section(target, full_block)
    written.append(target)
    return written


INSTALL_TARGETS: dict[str, list[str]] = {
    "claude-code": ["CLAUDE.md", ".claude/agents/laravelgraph.md", ".laravelgraph/agent.md"],
    "opencode":    [".opencode/instructions.md", ".laravelgraph/agent.md"],
    "cursor":      [".cursorrules", ".laravelgraph/agent.md"],
}
