"""LaravelGraph plugin template — copy this to .laravelgraph/plugins/my_plugin.py.

PLUGIN SYSTEM OVERVIEW
======================
LaravelGraph supports project-specific plugins that extend both the analysis
pipeline and the MCP tool surface — without touching the LaravelGraph source
code itself.

Plugins live in:
    <project-root>/.laravelgraph/plugins/*.py

They are loaded automatically, sorted alphabetically, at two integration points:

1. PIPELINE HOOK — ``run(ctx: PipelineContext)``
   Called after all 31 built-in phases have completed.  The full graph is
   already populated.  Your plugin can query existing nodes, add new nodes /
   relationships, annotate symbols, or write any extra domain knowledge you
   need.

2. MCP TOOL HOOK — ``register_tools(mcp: FastMCP)``
   Called at MCP server startup (``laravelgraph serve``).  Register any number
   of ``@mcp.tool()`` decorated functions to expose domain-specific intelligence
   to Claude Code and other agents.

Both hooks are optional.  A plugin file may implement one or both.  Any
exception during plugin loading is caught and logged — it never crashes the
pipeline or the server.

IMPORTING LARAVELGRAPH INTERNALS
=================================
Use these patterns (same as the built-in phases):

    from laravelgraph.core.schema import node_id as make_node_id
    from laravelgraph.logging import get_logger
    from laravelgraph.pipeline.orchestrator import PipelineContext

The ``ctx`` object handed to ``run()`` exposes:

    ctx.db              — GraphDB instance (upsert_node, upsert_rel, execute)
    ctx.project_root    — pathlib.Path to the Laravel project root
    ctx.config          — resolved Config (databases[], summary provider, …)
    ctx.fqn_index       — dict[FQN, node_id] built by earlier phases
    ctx.class_map       — dict[FQN, Path] (FQN → source file)
    ctx.parsed_php      — dict[str, PHPFile] (absolute path → parsed AST data)
    ctx.stats           — dict[str, int] accumulated across all phases
    ctx.errors          — list[str] — append non-fatal errors here

AFTER MODIFYING A PLUGIN
=========================
Pipeline analysis picks up changes on the next ``laravelgraph analyze`` run.

For MCP tool changes to take effect in the running server you must reinstall:

    pipx reinstall laravelgraph

(The MCP server is launched from the pipx binary; source changes alone are not
enough.)
"""

from __future__ import annotations

# ── Standard-library imports used in this example ─────────────────────────────
import json
from typing import TYPE_CHECKING, Any

# ── LaravelGraph internals ─────────────────────────────────────────────────────
from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger

if TYPE_CHECKING:
    # PipelineContext is only needed for type hints; importing it at runtime is
    # also fine — this guard just avoids a circular-import risk in edge cases.
    from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# HOOK 1 — Pipeline phase
# ══════════════════════════════════════════════════════════════════════════════

def run(ctx: "PipelineContext") -> None:
    """Pipeline hook: called after all 31 built-in phases have finished.

    This example annotates every EloquentModel node with a custom
    ``domain_tag`` property derived from the model's namespace, then writes a
    lightweight ``DomainGroup`` node that clusters models by their top-level
    namespace segment.

    Replace or extend this logic with anything your project needs — for example:
      • Linking models to Jira tickets via a custom CSV file.
      • Adding 'team_owner' annotations from a CODEOWNERS file.
      • Creating cross-project dependency edges for monorepo setups.
      • Propagating feature flags from a local JSON manifest.
    """

    # ── Query all EloquentModel nodes from the graph ───────────────────────────
    rows: list[dict[str, Any]] = ctx.db.execute(
        "MATCH (m:EloquentModel) RETURN m.node_id AS nid, m.fqn AS fqn"
    )
    if not rows:
        logger.info("plugin_template: no EloquentModel nodes found, skipping")
        return

    domain_counts: dict[str, int] = {}

    for row in rows:
        fqn: str = row.get("fqn") or ""
        nid: str = row.get("nid") or ""
        if not fqn or not nid:
            continue

        # Derive a domain tag from the second namespace segment, e.g.
        #   App\Billing\Invoice  →  "Billing"
        #   App\Models\User      →  "Models"   (generic, but still valid)
        parts = fqn.lstrip("\\").split("\\")
        domain_tag = parts[1] if len(parts) >= 2 else "Unknown"

        # Re-upsert the model node with the extra property.
        # upsert_node does a DETACH DELETE + INSERT, so include ALL properties
        # you want to keep.  In practice, for annotation-only changes you may
        # prefer a raw Cypher SET instead to avoid touching other properties:
        #
        #   ctx.db.execute(
        #       "MATCH (m:EloquentModel {node_id: $nid}) SET m.domain_tag = $tag",
        #       {"nid": nid, "tag": domain_tag},
        #   )
        #
        # We use SET here to avoid clobbering existing properties.
        try:
            ctx.db.execute(
                "MATCH (m:EloquentModel {node_id: $nid}) SET m.domain_tag = $tag",
                {"nid": nid, "tag": domain_tag},
            )
        except Exception as exc:
            # Non-fatal — log and continue
            logger.warning("plugin_template: could not annotate model",
                           nid=nid, error=str(exc))

        domain_counts[domain_tag] = domain_counts.get(domain_tag, 0) + 1

    # ── Write DomainGroup summary nodes ───────────────────────────────────────
    # These are custom node types that don't exist in the built-in schema.
    # They land in the graph as generic "Annotation" nodes with a kind field.
    #
    # NOTE: If you need a first-class node type with its own KuzuDB table, add
    # it to laravelgraph/core/schema.py (NODE_TYPES).  For lightweight
    # annotations, storing them as JSON on existing nodes or as Annotation
    # nodes is simpler.
    for domain_tag, count in domain_counts.items():
        annotation_nid = make_node_id("annotation", "domain_group", domain_tag)
        ctx.db.upsert_node("Annotation", {
            "node_id": annotation_nid,
            "kind":    "domain_group",
            "key":     domain_tag,
            "value":   json.dumps({"model_count": count}),
        })

    annotated = len(rows)
    ctx.stats["plugin_template_models_annotated"] = annotated
    logger.info("plugin_template: annotation complete",
                models=annotated, domains=len(domain_counts))


# ══════════════════════════════════════════════════════════════════════════════
# HOOK 2 — MCP tool registration
# ══════════════════════════════════════════════════════════════════════════════

def register_tools(mcp: Any, db: Any = None, sql_db: Any = None) -> None:
    """MCP hook: called at server startup to register extra tools.

    Tools registered here appear alongside the built-in LaravelGraph tools in
    Claude Code's tool list.  Name them clearly so agents can discover them.

    Args:
        mcp:    FastMCP instance (wrapped in PluginSafeMCP for safety).
        db:     Factory callable → GraphDB for Cypher queries against the code graph.
                Usage: rows = db().execute("MATCH (m:EloquentModel) RETURN m.name LIMIT 10")
        sql_db: Factory callable → pymysql connection for raw SQL queries.
                Usage: conn = sql_db(); cur = conn.cursor(); cur.execute("SELECT ..."); rows = cur.fetchall(); conn.close()
                May be None if no database is configured — always guard with ``if sql_db is None``.

    This example registers two tools:
      • ``list_domain_groups``   — surfaces the domain annotations from run().
      • ``models_in_domain``     — returns all models belonging to a domain tag.

    Replace these with whatever domain intelligence your project needs, such as:
      • A tool that returns the on-call team for a given PHP class.
      • A tool that looks up a feature-flag manifest and returns enabled flags.
      • A tool that cross-references models with external documentation URLs.
      • A tool that queries live MySQL data (use sql_db for this).
    """

    # Import here (not at module top) to avoid a hard dependency when the
    # plugin file is loaded in a non-server context (e.g. pipeline only).
    from laravelgraph.config import index_dir
    from laravelgraph.core.graph import GraphDB

    # ── Tool 1: list all domain groups ────────────────────────────────────────
    @mcp.tool()
    def list_domain_groups(project_path: str) -> str:  # noqa: ANN202
        """List all domain groups discovered by the plugin_template plugin.

        Returns a JSON object mapping domain tag → model count.

        Args:
            project_path: Absolute path to the indexed Laravel project root.
        """
        from pathlib import Path

        db_path = index_dir(Path(project_path)) / "graph.kuzu"
        if not db_path.exists():
            return json.dumps({"error": "Project not indexed. Run: laravelgraph analyze <path>"})

        db = GraphDB(db_path)
        try:
            rows = db.execute(
                "MATCH (a:Annotation {kind: 'domain_group'}) "
                "RETURN a.key AS domain, a.value AS data"
            )
            result: dict[str, Any] = {}
            for row in rows:
                domain = row.get("domain", "")
                raw = row.get("data", "{}")
                try:
                    result[domain] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    result[domain] = raw
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    # ── Tool 2: models belonging to a domain ──────────────────────────────────
    @mcp.tool()
    def models_in_domain(project_path: str, domain: str) -> str:  # noqa: ANN202
        """Return all EloquentModel FQNs belonging to a given domain tag.

        Domain tags are assigned by the plugin_template pipeline plugin based
        on the second segment of each model's namespace (e.g. "Billing").

        Args:
            project_path: Absolute path to the indexed Laravel project root.
            domain:       Domain tag to filter by (case-sensitive).
        """
        from pathlib import Path

        db_path = index_dir(Path(project_path)) / "graph.kuzu"
        if not db_path.exists():
            return json.dumps({"error": "Project not indexed. Run: laravelgraph analyze <path>"})

        db = GraphDB(db_path)
        try:
            rows = db.execute(
                "MATCH (m:EloquentModel {domain_tag: $tag}) "
                "RETURN m.fqn AS fqn, m.table_name AS table ORDER BY fqn",
                {"tag": domain},
            )
            models = [{"fqn": r.get("fqn"), "table": r.get("table")} for r in rows]
            return json.dumps({"domain": domain, "models": models, "count": len(models)}, indent=2)
        except Exception as exc:
            return json.dumps({"error": str(exc)})
