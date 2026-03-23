"""KuzuDB graph database interface for LaravelGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import kuzu

from laravelgraph.core.schema import NODE_TYPES, REL_TYPES
from laravelgraph.logging import get_logger

logger = get_logger(__name__)


class GraphDB:
    """Thin wrapper around KuzuDB providing typed node/edge operations."""

    def __init__(self, db_path: Path, force_reinit: bool = False, read_only: bool = False) -> None:
        self.full_build: bool = force_reinit
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(db_path), read_only=read_only)
        self._conn = kuzu.Connection(self._db)
        if force_reinit:
            self._drop_all_tables()
        if not read_only:
            self._init_schema()

    # ── Schema initialization ─────────────────────────────────────────────

    def _drop_all_tables(self) -> None:
        """Drop all relationship then node tables — used for force_reinit."""
        existing = self._existing_tables()
        rel_names = {self._rel_table(label) for label, _, _ in REL_TYPES}
        node_names = {self._node_table(label) for label, _ in NODE_TYPES}
        for name in rel_names & existing:
            try:
                self._conn.execute(f"DROP TABLE {name}")
            except Exception:
                pass
        for name in node_names & existing:
            try:
                self._conn.execute(f"DROP TABLE {name}")
            except Exception:
                pass

    def _init_schema(self) -> None:
        """Create all node and relationship tables if they don't exist."""
        existing = self._existing_tables()

        for label, props in NODE_TYPES:
            table_name = self._node_table(label)
            if table_name not in existing:
                cols = ", ".join(f"{n} {t}" for n, t in props)
                pk = props[0][0]
                ddl = f"CREATE NODE TABLE {table_name} ({cols}, PRIMARY KEY ({pk}))"
                try:
                    self._conn.execute(ddl)
                    logger.debug("Created node table", table=table_name)
                except Exception as e:
                    logger.warning("Node table creation failed", table=table_name, error=str(e))

        for label, node_pairs, props in REL_TYPES:
            table_name = self._rel_table(label)
            if table_name not in existing:
                # Build FROM/TO pairs — KuzuDB 0.11.3 requires explicit node types
                pairs_ddl = ", ".join(f"FROM {f} TO {t}" for f, t in node_pairs)
                if props:
                    cols = ", ".join(f"{n} {tp}" for n, tp in props)
                    ddl = f"CREATE REL TABLE {table_name} ({pairs_ddl}, {cols})"
                else:
                    ddl = f"CREATE REL TABLE {table_name} ({pairs_ddl})"
                try:
                    self._conn.execute(ddl)
                    logger.debug("Created relationship table", table=table_name)
                except Exception as e:
                    logger.warning("Rel table creation failed", table=table_name, error=str(e))

    def _existing_tables(self) -> set[str]:
        try:
            result = self._conn.execute("CALL show_tables() RETURN name")
            tables = set()
            while result.has_next():
                row = result.get_next()
                tables.add(row[0])
            return tables
        except Exception:
            return set()

    @staticmethod
    def _node_table(label: str) -> str:
        return label

    @staticmethod
    def _rel_table(label: str) -> str:
        return label

    # ── Core query interface ──────────────────────────────────────────────

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a Cypher query, returning list of row dicts."""
        try:
            if params:
                result = self._conn.execute(query, parameters=params)
            else:
                result = self._conn.execute(query)
            return self._result_to_list(result)
        except Exception as e:
            logger.error("Query execution failed", query=query[:200], error=str(e))
            raise

    def execute_raw(self, query: str, params: dict[str, Any] | None = None) -> Any:
        """Run a Cypher query, returning the raw KuzuDB result."""
        if params:
            return self._conn.execute(query, parameters=params)
        return self._conn.execute(query)

    @staticmethod
    def _result_to_list(result: Any) -> list[dict[str, Any]]:
        rows = []
        col_names = result.get_column_names() if hasattr(result, "get_column_names") else []
        while result.has_next():
            row_vals = result.get_next()
            if col_names:
                rows.append(dict(zip(col_names, row_vals)))
            else:
                rows.append({"_": row_vals})
        return rows

    # ── Upsert helpers ────────────────────────────────────────────────────

    def upsert_node(self, label: str, props: dict[str, Any]) -> None:
        """Merge a node by its primary key (first property)."""
        table = self._node_table(label)
        # Find the PK field (first property in schema)
        schema = dict(NODE_TYPES)
        pk_field = schema[label][0][0] if label in schema else "node_id"
        pk_val = props[pk_field]

        # Serialize list/dict values to JSON strings for storage
        clean = {k: self._serialize(v) for k, v in props.items()}

        # Kuzu doesn't have native MERGE, so we delete + create
        try:
            self._conn.execute(
                f"MATCH (n:{table} {{{pk_field}: $pk}}) DELETE n",
                parameters={"pk": pk_val},
            )
        except Exception:
            pass  # Node may not exist yet

        self._insert_node(table, clean)

    def _insert_node(self, table: str, props: dict[str, Any]) -> None:
        """INSERT a node, replacing any existing node with same PK."""
        assignments = []
        for k, v in props.items():
            if v is None:
                assignments.append(f"{k}: null")
            elif isinstance(v, bool):
                assignments.append(f"{k}: {str(v).lower()}")
            elif isinstance(v, (int, float)):
                assignments.append(f"{k}: {v}")
            elif isinstance(v, list):
                # Float[] for embeddings
                inner = ", ".join(str(x) for x in v)
                assignments.append(f"{k}: [{inner}]")
            else:
                # Escape backslashes first, then quotes (KuzuDB treats \ as escape char)
                escaped = str(v).replace("\\", "\\\\").replace("'", "\\'")
                assignments.append(f"{k}: '{escaped}'")

        props_str = ", ".join(assignments)
        self._conn.execute(f"CREATE (:{table} {{{props_str}}})")

    def upsert_rel(
        self,
        rel_label: str,
        from_label: str,
        from_id: str,
        to_label: str,
        to_id: str,
        props: dict[str, Any] | None = None,
        from_pk: str = "node_id",
        to_pk: str = "node_id",
    ) -> None:
        """Create or replace a relationship between two nodes."""
        from_table = self._node_table(from_label)
        to_table = self._node_table(to_label)
        rel_table = self._rel_table(rel_label)

        # Skip DELETE on full builds — the DB is fresh, nothing to remove
        if not self.full_build:
            try:
                self._conn.execute(
                    f"""
                    MATCH (a:{from_table} {{{from_pk}: $fid}})-[r:{rel_table}]->(b:{to_table} {{{to_pk}: $tid}})
                    DELETE r
                    """,
                    parameters={"fid": from_id, "tid": to_id},
                )
            except Exception:
                pass

        # Create new relationship
        if props:
            clean = {k: self._serialize(v) for k, v in props.items()}
            prop_assignments = []
            for k, v in clean.items():
                if v is None:
                    prop_assignments.append(f"{k}: null")
                elif isinstance(v, bool):
                    prop_assignments.append(f"{k}: {str(v).lower()}")
                elif isinstance(v, (int, float)):
                    prop_assignments.append(f"{k}: {v}")
                elif isinstance(v, list):
                    inner = ", ".join(str(x) for x in v)
                    prop_assignments.append(f"{k}: [{inner}]")
                else:
                    escaped = str(v).replace("\\", "\\\\").replace("'", "\\'")
                    prop_assignments.append(f"{k}: '{escaped}'")

            props_str = "{" + ", ".join(prop_assignments) + "}"
            query = f"""
                MATCH (a:{from_table} {{{from_pk}: $fid}}), (b:{to_table} {{{to_pk}: $tid}})
                CREATE (a)-[:{rel_table} {props_str}]->(b)
            """
        else:
            query = f"""
                MATCH (a:{from_table} {{{from_pk}: $fid}}), (b:{to_table} {{{to_pk}: $tid}})
                CREATE (a)-[:{rel_table}]->(b)
            """

        try:
            self._conn.execute(query, parameters={"fid": from_id, "tid": to_id})
        except Exception as e:
            logger.warning(
                "Relationship creation failed",
                rel=rel_label,
                from_id=from_id,
                to_id=to_id,
                error=str(e),
            )

    @staticmethod
    def _serialize(v: Any) -> Any:
        if isinstance(v, (list, dict)) and not all(isinstance(x, float) for x in (v if isinstance(v, list) else [])):
            return json.dumps(v)
        return v

    # ── Bulk operations ───────────────────────────────────────────────────

    def delete_file_symbols(self, file_path: str) -> None:
        """Remove all symbols defined in a given file (for incremental updates)."""
        for label, _ in NODE_TYPES:
            if label in ("Folder", "File", "Community", "Process", "ScheduledTask"):
                continue
            try:
                self._conn.execute(
                    f"MATCH (n:{label} {{file_path: $fp}}) DETACH DELETE n",
                    parameters={"fp": file_path},
                )
            except Exception:
                pass

    def clear_all(self) -> None:
        """Wipe all data — used before a full re-index."""
        for label, _ in NODE_TYPES:
            try:
                self._conn.execute(f"MATCH (n:{label}) DETACH DELETE n")
            except Exception:
                pass

    # ── Convenience queries ───────────────────────────────────────────────

    def node_exists(self, label: str, node_id: str) -> bool:
        table = self._node_table(label)
        try:
            result = self._conn.execute(
                f"MATCH (n:{table} {{node_id: $id}}) RETURN count(n) AS cnt",
                parameters={"id": node_id},
            )
            rows = self._result_to_list(result)
            return bool(rows and rows[0].get("cnt", 0) > 0)
        except Exception:
            return False

    def get_node(self, label: str, node_id: str) -> dict[str, Any] | None:
        table = self._node_table(label)
        try:
            result = self._conn.execute(
                f"MATCH (n:{table} {{node_id: $id}}) RETURN n.*",
                parameters={"id": node_id},
            )
            rows = self._result_to_list(result)
            return rows[0] if rows else None
        except Exception:
            return None

    def stats(self) -> dict[str, int]:
        """Return node/edge counts by type."""
        counts: dict[str, int] = {}
        for label, _ in NODE_TYPES:
            try:
                result = self._conn.execute(f"MATCH (n:{label}) RETURN count(n) AS cnt")
                rows = self._result_to_list(result)
                cnt = rows[0]["cnt"] if rows else 0
                if cnt > 0:
                    counts[label] = cnt
            except Exception:
                pass
        return counts

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "GraphDB":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
