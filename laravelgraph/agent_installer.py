"""LaravelGraph agent instruction installer.

Writes a LaravelGraph-specific agent instruction block to the config files that
AI agents read at session start.  Supports:

  - Claude Code  → CLAUDE.md  (project root or ~/.claude/CLAUDE.md)
  - OpenCode     → .opencode/instructions.md
  - Cursor       → .cursorrules

Each target file gets a clearly-delimited ``<!-- laravelgraph-agent-instructions -->``
section that is idempotent — running the installer again replaces the section
in place rather than appending a second copy.
"""

from __future__ import annotations

from pathlib import Path

# ── Markers ────────────────────────────────────────────────────────────────────

_SECTION_START = "<!-- laravelgraph-agent-instructions-start -->"
_SECTION_END   = "<!-- laravelgraph-agent-instructions-end -->"

# ── Agent instruction block ────────────────────────────────────────────────────

def build_agent_block() -> str:
    """Return the complete LaravelGraph agent instruction block.

    This is the canonical guide for AI agents using LaravelGraph MCP tools.
    It covers tool hierarchy, investigation protocols, plugin workflows, and
    common pitfalls.
    """
    return """\
<!-- laravelgraph-agent-instructions-start -->

## LaravelGraph — Agent Protocol

This project is indexed by LaravelGraph. You have access to a complete knowledge
graph of every PHP class, method, route, model, event, job, database table, and
their relationships. **Query the graph before reading files.**

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

### Common Pitfalls

- **Don't run `laravelgraph_dead_code` on every session** — it's expensive.
  Run it only when specifically investigating dead code or cleaning up.
- **Don't use `laravelgraph_cypher` when a built-in tool exists** — built-in
  tools handle KuzuDB's per-label quirks automatically.
- **`laravelgraph_explain` is semantic, not keyword** — "explain payment refund
  flow" works better than "PaymentRefund".
- **Route property names**: KuzuDB Route nodes use `http_method` (not `method`)
  and `action_method` (not `action`) in Cypher queries.
- **`laravelgraph_feature_context` is ONE call** — don't chain
  `laravelgraph_routes` + `laravelgraph_models` + `laravelgraph_events`
  separately when feature_context returns all of them together.

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

<!-- laravelgraph-agent-instructions-end -->"""


# ── Upsert helper ──────────────────────────────────────────────────────────────

def _upsert_section(target: Path, block: str) -> None:
    """Replace the LaravelGraph section in *target*, or append it if absent.

    Idempotent — running multiple times leaves exactly one section.
    """
    existing = target.read_text(encoding="utf-8") if target.exists() else ""

    if _SECTION_START in existing and _SECTION_END in existing:
        # Replace the existing section
        before = existing[: existing.index(_SECTION_START)]
        after  = existing[existing.index(_SECTION_END) + len(_SECTION_END):]
        new_content = before + block + after
    else:
        # Append (with a blank line separator)
        sep = "\n\n" if existing and not existing.endswith("\n\n") else ("\n" if existing else "")
        new_content = existing + sep + block + "\n"

    target.write_text(new_content, encoding="utf-8")


# ── Install targets ────────────────────────────────────────────────────────────

def install_for_claude_code(project_root: Path) -> Path:
    """Append/replace the LaravelGraph section in CLAUDE.md."""
    target = project_root / "CLAUDE.md"
    _upsert_section(target, build_agent_block())
    return target


def install_for_opencode(project_root: Path) -> Path:
    """Append/replace the LaravelGraph section in .opencode/instructions.md."""
    target = project_root / ".opencode" / "instructions.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    _upsert_section(target, build_agent_block())
    return target


def install_for_cursor(project_root: Path) -> Path:
    """Append/replace the LaravelGraph section in .cursorrules."""
    target = project_root / ".cursorrules"
    _upsert_section(target, build_agent_block())
    return target


INSTALL_TARGETS: dict[str, str] = {
    "claude-code": "CLAUDE.md",
    "opencode":    ".opencode/instructions.md",
    "cursor":      ".cursorrules",
}
