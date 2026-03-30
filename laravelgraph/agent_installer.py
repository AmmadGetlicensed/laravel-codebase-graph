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
### Tool Hierarchy (use in this order)

1. **Plugin tools** (e.g. `usr_summary`, `ord_routes`) — start here if a plugin
   covers the domain.  Plugin tools give pre-built, domain-specific answers.
   Check the `## LOADED PLUGINS` section at session start for what's available.

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

### What Tools Can Do

| Tool | Purpose |
|------|---------|
| `laravelgraph_feature_context` | All routes + source + models + events + jobs for a feature in one call |
| `laravelgraph_explain` | Semantic search — find the right class/method from a plain-English description |
| `laravelgraph_context` | 360° symbol view: callers, callees, relationships, PHP source |
| `laravelgraph_request_flow` | Full request trace from route → controller → services → events/jobs |
| `laravelgraph_impact` | What breaks if this method/class changes |
| `laravelgraph_db_impact` | What code touches a given DB column |
| `laravelgraph_security_surface` | All auth middleware, gates, policies, and unprotected routes |
| `laravelgraph_performance_risks` | N+1 queries, missing indexes, eager-load gaps |
| `laravelgraph_race_conditions` | Concurrent job/queue risks |
| `laravelgraph_dead_code` | Unreachable/unused classes and methods |
| `laravelgraph_routes` | All routes with HTTP method, URI, controller, middleware |
| `laravelgraph_models` | All Eloquent models with relationships and fillable fields |
| `laravelgraph_events` | All events with their listeners and dispatchers |
| `laravelgraph_schema` | Database schema: tables, columns, indexes, FKs |
| `laravelgraph_cypher` | Custom Cypher query for anything not covered by built-in tools |
| `laravelgraph_plugin_knowledge` | Read accumulated discoveries stored by plugins |
| `laravelgraph_suggest_plugins` | Recommend which domain plugins to generate next |
| `laravelgraph_request_plugin` | Generate a new domain plugin (needs LLM configured) |
| `laravelgraph_run_plugin_tool` | Call a plugin tool immediately without server restart |
| `laravelgraph_outbound_apis` | All external HTTP calls made by this codebase |
| `laravelgraph_job_chain` | Full job dispatch chain from a given job class |
| `laravelgraph_cross_cutting_concerns` | Traits, service container bindings, shared utilities |
| `laravelgraph_bindings` | Service container bindings and interface→implementation map |
| `laravelgraph_contracts` | All interfaces and their implementations |
| `laravelgraph_config_usage` | Where config() and env() calls are used across the codebase |
| `laravelgraph_db_context` | LLM-annotated table and column descriptions |
| `laravelgraph_db_query` | Query the live database directly (read-only) |

### What Tools Cannot Do

- **Cannot modify the Laravel codebase** — all tools are read-only graph queries.
- **Cannot run tests or execute PHP** — analysis is static + live DB introspection only.
- **`laravelgraph_dead_code` is expensive** — do not run on every session. Use only when investigating dead code or cleaning up.
- **`laravelgraph_cypher` does not auto-handle label quirks** — built-in tools do. Use Cypher only when no built-in tool fits.
- **`laravelgraph_explain` is semantic, not keyword-based** — "explain payment refund flow" works better than "PaymentRefund".
- **Graph reflects the last `laravelgraph analyze` run** — not real-time. Re-index after significant code changes.

### Investigation Protocol

- **Never read PHP files manually** when the graph can answer.  File reads cost
  more context than a graph query.
- Use `laravelgraph_cypher(query)` for custom Cypher when built-in tools don't
  cover your exact question.  Always add a `LIMIT`.
- Use `laravelgraph_impact(symbol="...")` before changing any method or class
  to understand what else will break.
- Use `laravelgraph_db_impact(table="...")` before changing a DB column.
- For security reviews: `laravelgraph_security_surface()` then
  `laravelgraph_request_flow()` on suspicious routes.
- For performance reviews: `laravelgraph_performance_risks()` then
  `laravelgraph_race_conditions()`.

### Common Pitfalls

- **Route property names**: KuzuDB Route nodes use `http_method` (not `method`)
  and `action_method` (not `action`) in Cypher queries.
- **`laravelgraph_feature_context` is ONE call** — don't chain
  `laravelgraph_routes` + `laravelgraph_models` + `laravelgraph_events`
  separately when feature_context returns all of them together.
- **Ambiguous class names**: when a tool shows "AMBIGUOUS NAME", use the full
  FQN shown in the warning to target the exact class.
- **`laravelgraph_explain` is semantic** — phrase queries as human questions,
  not class names.

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

These findings persist across sessions.  Future agents read them via
`laravelgraph_plugin_knowledge()` without re-running the analysis.

**Call `store_discoveries` after every investigation** — not just the big ones.
Even a single insight ("this feature is behind feature flag X") saves the next
agent from re-discovering it.

### Plugin Knowledge Recall

At the start of any session involving a feature that has a plugin:

```
laravelgraph_plugin_knowledge()                    # all accumulated discoveries
laravelgraph_plugin_knowledge(plugin_name="slug")  # discoveries for one plugin
```

Read these before doing fresh analysis — the answer may already be stored.

### Keeping Plugin Knowledge Current (CI/cron)

```bash
# Run weekly to auto-generate plugins for uncovered high-value features
laravelgraph plugin evolve . --max-generate 2

# Dry run to preview what would be generated
laravelgraph plugin evolve . --dry-run
```

```yaml
# .github/workflows/laravelgraph.yml
- name: Evolve plugins
  run: laravelgraph plugin evolve . --max-generate 2
  schedule:
    - cron: '0 9 * * 1'  # every Monday morning
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
  Use this agent for all LaravelGraph knowledge graph queries, codebase analysis,
  plugin management, and investigation tasks. Delegate to this agent whenever the
  user asks about how the codebase works, what calls what, feature investigations,
  security/performance reviews, or plugin management.
  <example>user: "how does the booking flow work?"</example>
  <example>user: "what calls this method?"</example>
  <example>user: "what's the impact of changing this method?"</example>
  <example>user: "what routes are in this app?"</example>
  <example>user: "generate a plugin for the order domain"</example>
model: inherit
---

"""
    return frontmatter + build_rich_agent_body(data) + "\n"


def build_minimal_block() -> str:
    """Return the minimal CLAUDE.md delegation block."""
    return (
        f"{_SECTION_START}\n\n"
        "## LaravelGraph\n\n"
        "This project is indexed by [LaravelGraph](https://github.com/laravelgraph/laravelgraph) "
        "— a knowledge graph of every PHP class, method, route, model, event, job, and "
        "database table in this codebase.\n\n"
        "For all LaravelGraph-related work (codebase queries, feature investigations, "
        "impact analysis, security/performance reviews, plugin management), "
        "delegate to the **laravelgraph** agent. The agent has full knowledge of all "
        "available tools, their capabilities, investigation protocols, and this project's "
        "specific graph data.\n\n"
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
