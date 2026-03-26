"""Phase 25 — Model-to-Table Graph Linking.

Bridges the gap between EloquentModel nodes and DatabaseTable nodes by
creating USES_TABLE edges.  This is the critical connection that allows
agents to traverse: Controller → Model → DatabaseTable → DatabaseColumn.

Strategy (in priority order):

1. Live DB tables first  — prefer connection-prefixed nodes created by
   phase_24 over migration-derived nodes, since live data is authoritative.
2. Migration-derived fallback — if no live DB is configured or the model's
   table doesn't appear in any live DB, fall back to the migration-derived
   "table:{name}" node if it exists.
3. Stub creation — if neither exists, create a minimal DatabaseTable stub
   so the relationship is never missing. Agents can always traverse from
   model to table.

Connection resolution:
- If the EloquentModel class defines ``protected $connection = 'analytics'``
  that connection is used for lookup.
- Otherwise the first configured database is treated as default, matching
  Laravel's own fallback behaviour.
"""

from __future__ import annotations

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


def _infer_table_name(model_name: str) -> str:
    """Laravel convention: ModelName → model_names (snake_case + plural)."""
    import re
    # CamelCase → snake_case
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", model_name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s).lower()
    # Naive pluralisation (covers ~90% of English nouns)
    if s.endswith("y") and not s.endswith("ey"):
        return s[:-1] + "ies"
    if s.endswith(("s", "sh", "ch", "x", "z")):
        return s + "es"
    return s + "s"


def run(ctx: PipelineContext) -> None:
    """Create USES_TABLE edges from every EloquentModel to its DatabaseTable."""
    db = ctx.db
    configured_connections = [c.name for c in ctx.config.databases]

    # Build a lookup of all live-DB table nodes:
    # {connection_name: {table_name: node_id}}
    live_table_map: dict[str, dict[str, str]] = {}
    try:
        rows = db.execute(
            "MATCH (t:DatabaseTable) WHERE t.source = 'live_db' "
            "RETURN t.connection AS conn, t.name AS name, t.node_id AS nid"
        )
        for r in rows:
            conn = r.get("conn") or ""
            name = r.get("name") or ""
            nid = r.get("nid") or ""
            if conn and name and nid:
                live_table_map.setdefault(conn, {})[name] = nid
    except Exception as exc:
        logger.debug("Could not load live table map", error=str(exc))

    # Fetch all EloquentModel nodes
    try:
        models = db.execute(
            "MATCH (m:EloquentModel) RETURN m.node_id AS nid, m.name AS name, "
            "m.fqn AS fqn, m.db_table AS db_table"
        )
    except Exception as exc:
        logger.error("Could not load EloquentModel nodes", error=str(exc))
        return

    linked = 0
    stubbed = 0

    for model in models:
        model_nid = model.get("nid") or ""
        model_name = model.get("name") or ""
        fqn = model.get("fqn") or ""
        explicit_table = model.get("db_table") or ""

        table_name = explicit_table or _infer_table_name(model_name)
        if not table_name:
            continue

        # Determine which connection the model uses.
        # Phase 13 doesn't parse $connection yet, so we check the PHP source.
        connection = _extract_connection_from_source(ctx, fqn, configured_connections)

        # ── 1. Try live DB table (preferred) ──────────────────────────────────
        table_nid: str | None = None

        if connection and connection in live_table_map:
            table_nid = live_table_map[connection].get(table_name)

        if not table_nid:
            # Try every configured connection (table may be on any of them)
            for conn_name, tbl_map in live_table_map.items():
                if table_name in tbl_map:
                    table_nid = tbl_map[table_name]
                    connection = conn_name
                    break

        # ── 2. Fall back to migration-derived node ─────────────────────────────
        if not table_nid:
            migration_nid = make_node_id("table", table_name)
            if db.node_exists("DatabaseTable", migration_nid):
                table_nid = migration_nid
                connection = connection or "default"

        # ── 3. Create a stub if nothing found ─────────────────────────────────
        if not table_nid:
            stub_conn = connection or (configured_connections[0] if configured_connections else "default")
            table_nid = make_node_id("table", stub_conn, table_name)
            try:
                db.upsert_node("DatabaseTable", {
                    "node_id": table_nid,
                    "name": table_name,
                    "connection": stub_conn,
                    "created_in": "",
                    "engine": "",
                    "charset": "",
                    "table_comment": "",
                    "source": "stub",
                })
                stubbed += 1
            except Exception as exc:
                logger.debug("Stub table creation failed", table=table_name, error=str(exc))
                continue

        # ── Create USES_TABLE edge ─────────────────────────────────────────────
        try:
            db.upsert_rel(
                "USES_TABLE",
                "EloquentModel", model_nid,
                "DatabaseTable", table_nid,
                {"connection": connection or ""},
            )
            linked += 1
        except Exception as exc:
            logger.debug(
                "USES_TABLE edge failed",
                model=fqn,
                table=table_name,
                error=str(exc),
            )

    ctx.stats["model_table_links"] = linked
    ctx.stats["model_table_stubs"] = stubbed
    logger.info("Model-table linking complete", linked=linked, stubs_created=stubbed)


def _extract_connection_from_source(
    ctx: PipelineContext,
    model_fqn: str,
    configured_connections: list[str],
) -> str:
    """Try to read the $connection property from the model's PHP source."""
    import re

    parsed_php = ctx.parsed_php
    if not parsed_php or not model_fqn:
        return ""

    # FQN → file path lookup via parsed_php or class_map
    file_path = ctx.class_map.get(model_fqn, "")
    if not file_path:
        return ""

    php_file = parsed_php.get(str(file_path))
    if not php_file:
        # Try reading source directly
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace") if hasattr(file_path, "read_text") else ""
        except Exception:
            return ""
    else:
        source = getattr(php_file, "source", "") or ""

    if not source:
        return ""

    m = re.search(r"protected\s+\$connection\s*=\s*['\"]([^'\"]+)['\"]", source)
    if m:
        conn = m.group(1)
        # Validate it's a connection we know about
        if conn in configured_connections:
            return conn
        # Return it anyway — user may have more connections than configured
        return conn

    return ""
