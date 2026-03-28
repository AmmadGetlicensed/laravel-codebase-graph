"""Plugin scaffolder — generates pre-populated plugin files from graph context."""

from __future__ import annotations

from pathlib import Path

from laravelgraph.plugins.suggest import RECIPES, PluginRecipe


def scaffold_plugin(
    plugin_name: str,
    recipe_slug: str | None,
    project_root: Path,
    db: object,
) -> Path:
    """Generate a pre-populated plugin file at <project_root>/.laravelgraph/plugins/<plugin_name>.py.

    Args:
        plugin_name:  Plugin identifier (alphanumeric + hyphens, e.g. "payment-audit").
        recipe_slug:  Optional slug from RECIPES (e.g. "payment_lifecycle").
        project_root: Root directory of the Laravel project.
        db:           Open GraphDB instance for querying context data.

    Returns:
        Path to the generated plugin file.

    Raises:
        FileExistsError: If the plugin file already exists.
    """
    output_path = project_root / ".laravelgraph" / "plugins" / f"{plugin_name}.py"

    if output_path.exists():
        raise FileExistsError(
            f"Plugin file already exists: {output_path}\n"
            "Delete it first or choose a different name."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Find the matching recipe if a slug was provided
    recipe: PluginRecipe | None = None
    if recipe_slug:
        for r in RECIPES:
            if r.slug == recipe_slug or r.name == recipe_slug:
                recipe = r
                break

    # Query the graph for relevant context
    ctx_data = _gather_context(recipe, db)

    content = _build_plugin_content(plugin_name, recipe, ctx_data)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def _gather_context(recipe: PluginRecipe | None, db: object) -> dict:
    """Query the graph for domain-relevant model names, table names, and route prefixes."""
    ctx: dict = {
        "models": [],
        "tables": [],
        "routes": [],
        "columns": [],
    }

    if recipe is None:
        return ctx

    def _safe_execute(query: str) -> list[dict]:
        try:
            rows = db.execute(query)  # type: ignore[union-attr]
            return rows if rows else []
        except Exception:
            return []

    # Use the recipe's domain signals to derive context query terms
    slug = recipe.slug

    if "payment" in slug:
        for row in _safe_execute(
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'payment' "
            "OR toLower(m.name) CONTAINS 'invoice' OR toLower(m.name) CONTAINS 'charge' "
            "OR toLower(m.name) CONTAINS 'order' RETURN m.name AS name LIMIT 10"
        ):
            ctx["models"].append(row.get("name", ""))
        for row in _safe_execute(
            "MATCH (t:DatabaseTable) WHERE t.name CONTAINS 'payment' OR t.name CONTAINS 'invoice' "
            "OR t.name CONTAINS 'order' RETURN t.name AS name LIMIT 10"
        ):
            ctx["tables"].append(row.get("name", ""))
        for row in _safe_execute(
            "MATCH (r:Route) WHERE toLower(r.uri) CONTAINS 'pay' OR toLower(r.uri) CONTAINS 'checkout' "
            "OR toLower(r.uri) CONTAINS 'webhook' RETURN r.uri AS uri LIMIT 10"
        ):
            ctx["routes"].append(row.get("uri", ""))

    elif "tenant" in slug:
        for row in _safe_execute(
            "MATCH (c:DatabaseColumn) WHERE c.name IN ['tenant_id','organization_id','company_id','account_id'] "
            "RETURN DISTINCT c.table_name AS tname LIMIT 10"
        ):
            ctx["tables"].append(row.get("tname", ""))
        for row in _safe_execute(
            "MATCH (m:Middleware) WHERE toLower(m.name) CONTAINS 'tenant' RETURN m.name AS name LIMIT 5"
        ):
            ctx["models"].append(row.get("name", ""))

    elif "booking" in slug:
        for row in _safe_execute(
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'booking' "
            "OR toLower(m.name) CONTAINS 'appointment' OR toLower(m.name) CONTAINS 'reservation' "
            "RETURN m.name AS name LIMIT 10"
        ):
            ctx["models"].append(row.get("name", ""))
        for row in _safe_execute(
            "MATCH (t:DatabaseTable) WHERE t.name CONTAINS 'booking' OR t.name CONTAINS 'appointment' "
            "OR t.name CONTAINS 'slot' RETURN t.name AS name LIMIT 10"
        ):
            ctx["tables"].append(row.get("name", ""))

    elif "subscription" in slug:
        for row in _safe_execute(
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'subscription' "
            "OR toLower(m.name) CONTAINS 'plan' RETURN m.name AS name LIMIT 10"
        ):
            ctx["models"].append(row.get("name", ""))
        for row in _safe_execute(
            "MATCH (t:DatabaseTable) WHERE t.name CONTAINS 'subscription' OR t.name CONTAINS 'plan' "
            "RETURN t.name AS name LIMIT 10"
        ):
            ctx["tables"].append(row.get("name", ""))

    elif "rbac" in slug:
        for row in _safe_execute(
            "MATCH (m:EloquentModel) WHERE toLower(m.name) IN ['role','permission','userrole','rolepermission'] "
            "RETURN m.name AS name LIMIT 10"
        ):
            ctx["models"].append(row.get("name", ""))
        for row in _safe_execute(
            "MATCH (t:DatabaseTable) WHERE t.name IN ['roles','permissions','role_user','model_has_roles','model_has_permissions'] "
            "RETURN t.name AS name LIMIT 10"
        ):
            ctx["tables"].append(row.get("name", ""))

    elif "audit" in slug:
        for row in _safe_execute(
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'audit' "
            "OR toLower(m.name) CONTAINS 'activity' OR toLower(m.name) CONTAINS 'log' "
            "RETURN m.name AS name LIMIT 10"
        ):
            ctx["models"].append(row.get("name", ""))

    elif "feature" in slug or "flags" in slug:
        for row in _safe_execute(
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'feature' "
            "OR toLower(m.name) CONTAINS 'flag' OR toLower(m.name) CONTAINS 'toggle' "
            "RETURN m.name AS name LIMIT 10"
        ):
            ctx["models"].append(row.get("name", ""))

    # Remove empty strings and deduplicate
    ctx["models"] = list(dict.fromkeys(m for m in ctx["models"] if m))
    ctx["tables"] = list(dict.fromkeys(t for t in ctx["tables"] if t))
    ctx["routes"] = list(dict.fromkeys(r for r in ctx["routes"] if r))
    ctx["columns"] = list(dict.fromkeys(c for c in ctx["columns"] if c))

    return ctx


def _build_plugin_content(
    plugin_name: str,
    recipe: PluginRecipe | None,
    ctx_data: dict,
) -> str:
    """Generate the Python source for a plugin file.

    Produces a self-contained plugin with:
      - Module docstring
      - PLUGIN_MANIFEST dict
      - run(ctx) analysis function with TODO markers pointing at real graph symbols
      - register_tools(mcp) with one example tool using the correct prefix
    """
    # Determine prefix and title
    if recipe:
        tool_prefix = recipe.tool_prefix
        title = recipe.title
        description = recipe.description
        slug = recipe.slug
    else:
        # Derive prefix from name
        tool_prefix = plugin_name.replace("-", "_") + "_"
        title = plugin_name.replace("-", " ").title()
        description = f"Custom analysis plugin: {plugin_name}"
        slug = plugin_name.replace("-", "_")

    models = ctx_data.get("models", [])
    tables = ctx_data.get("tables", [])
    routes = ctx_data.get("routes", [])

    # Format context hints as Python list literals for the generated file
    models_repr = repr(models) if models else "[]  # TODO: add model names from your project"
    tables_repr = repr(tables) if tables else "[]  # TODO: add table names from your project"
    routes_repr = repr(routes) if routes else "[]  # TODO: add route URIs from your project"

    # First tool name = prefix + "analyze"
    first_tool_name = f"{tool_prefix}analyze"

    # Build TODO comment for run() body
    if models:
        todo_models = "\n    # Relevant models detected: " + ", ".join(models[:5])
    else:
        todo_models = "\n    # TODO: query for relevant models in your domain"

    if tables:
        todo_tables = "\n    # Relevant tables detected: " + ", ".join(tables[:5])
    else:
        todo_tables = "\n    # TODO: query for relevant database tables in your domain"

    if routes:
        todo_routes = "\n    # Relevant routes detected: " + ", ".join(routes[:5])
    else:
        todo_routes = "\n    # TODO: query for relevant routes in your domain"

    return f'''\
"""
{title}

{description}

This plugin was auto-generated by `laravelgraph plugin scaffold {plugin_name}{"" if not recipe else f" --recipe {recipe.slug}"}`
and pre-populated with context from your project graph.

To activate this plugin:
  1. Edit the run() function to add your analysis logic.
  2. Edit register_tools() to add MCP tools your agent can call.
  3. Ensure PLUGIN_MANIFEST fields are correct.
  4. Run `laravelgraph plugin validate {plugin_name}.py` to check for issues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ── Plugin Manifest ────────────────────────────────────────────────────────────
# All fields are required. tool_prefix must be unique across all installed plugins
# to avoid name collisions in the MCP server.

PLUGIN_MANIFEST = {{
    "name": "{plugin_name}",
    "version": "0.1.0",
    "description": "{description}",
    "tool_prefix": "{tool_prefix}",
    "author": "",
    "requires": [],
}}

# ── Context from your project graph ───────────────────────────────────────────
# These were detected automatically. Edit as needed.

DOMAIN_MODELS = {models_repr}
DOMAIN_TABLES = {tables_repr}
DOMAIN_ROUTES = {routes_repr}


# ── Pipeline hook (optional) ───────────────────────────────────────────────────
# run() is called automatically after the main analysis pipeline completes.
# Use ctx.db to query/write graph nodes and edges.
# ctx has: ctx.db, ctx.config, ctx.project_root

def run(ctx: object) -> None:
    """Post-pipeline analysis hook — runs after all 26 pipeline phases complete."""
    db = getattr(ctx, "db", None)
    if db is None:
        return
    {todo_models}
    {todo_tables}
    {todo_routes}

    # TODO: implement your analysis logic here.
    # Example — find models without a status column:
    # rows = db.execute(
    #     "MATCH (m:EloquentModel) WHERE NOT EXISTS {{
    #         MATCH (m)-[:USES_TABLE]->(t:DatabaseTable)-[:HAS_COLUMN]->(c:DatabaseColumn)
    #         WHERE c.name = 'status'
    #     }} RETURN m.name AS name"
    # )
    # for row in rows:
    #     print(f"  Missing status column: {{row['name']}}")


# ── MCP Tools ─────────────────────────────────────────────────────────────────
# register_tools() is called when the MCP server starts.
# All tools MUST use the tool_prefix defined in PLUGIN_MANIFEST.

def register_tools(mcp: object) -> None:
    """Register MCP tools for this plugin. Called by the MCP server at startup."""

    @mcp.tool()  # type: ignore[misc]
    def {first_tool_name}(query: str = "") -> str:
        """Analyze {title.lower()} patterns in the codebase.

        Args:
            query: Optional filter — model name, table, or feature keyword.
        """
        # TODO: implement your tool logic here.
        # Access the graph via ctx (injected by the server) or import GraphDB directly.
        #
        # Example:
        # db = _get_db()  # injected by MCP server infrastructure
        # rows = db.execute("MATCH (m:EloquentModel) RETURN m.name LIMIT 20")
        # return "\\n".join(r["m.name"] for r in rows)
        return (
            "TODO: implement {first_tool_name}. "
            "Edit .laravelgraph/plugins/{plugin_name}.py to add your analysis logic."
        )
'''
