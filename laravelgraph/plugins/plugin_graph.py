"""Plugin graph — a separate writable KuzuDB for plugin-stored knowledge.

Plugins write to this graph at runtime (no analyze required).
Core graph stays read-only. Plugin graph grows across conversations.

Schema:
  PluginNode — flexible node with JSON data blob
  PluginEdge — flexible relationship with JSON data blob
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import kuzu


class PluginGraphDB:
    """Writable KuzuDB for plugin-stored knowledge."""

    SCHEMA_INIT = [
        """CREATE NODE TABLE IF NOT EXISTS PluginNode (
            node_id STRING,
            plugin_source STRING,
            label STRING,
            core_ref STRING,
            data STRING,
            created_at STRING,
            updated_at STRING,
            PRIMARY KEY (node_id)
        )""",
        """CREATE NODE TABLE IF NOT EXISTS PluginEdge_Node (
            edge_id STRING,
            plugin_source STRING,
            from_id STRING,
            to_id STRING,
            rel_type STRING,
            data STRING,
            created_at STRING,
            PRIMARY KEY (edge_id)
        )""",
    ]

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = kuzu.Database(str(db_path))
        self._conn = kuzu.Connection(self._db)
        for ddl in self.SCHEMA_INIT:
            try:
                self._conn.execute(ddl)
            except Exception:
                pass  # Table may already exist

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        """Run a Cypher query, returning list of row dicts."""
        try:
            if params:
                result = self._conn.execute(query, parameters=params)
            else:
                result = self._conn.execute(query)
            return self._result_to_list(result)
        except Exception as e:
            raise RuntimeError(f"Plugin graph query failed: {e}") from e

    @staticmethod
    def _result_to_list(result: Any) -> list[dict]:
        rows = []
        col_names = result.get_column_names() if hasattr(result, "get_column_names") else []
        while result.has_next():
            row_vals = result.get_next()
            if col_names:
                rows.append(dict(zip(col_names, row_vals)))
            else:
                rows.append({"_": row_vals})
        return rows

    def upsert_plugin_node(
        self,
        plugin_source: str,
        label: str,
        node_id: str,
        data: dict,
        core_ref: str = "",
    ) -> None:
        """Merge a PluginNode, updating updated_at on each call."""
        import json

        now = datetime.now(timezone.utc).isoformat()
        data_str = json.dumps(data)

        # Check if node exists
        existing = self.execute(
            "MATCH (n:PluginNode {node_id: $nid}) RETURN n.created_at AS created_at",
            params={"nid": node_id},
        )

        if existing:
            created_at = existing[0].get("created_at", now)
            # Delete and recreate (KuzuDB upsert pattern)
            try:
                self._conn.execute(
                    "MATCH (n:PluginNode {node_id: $nid}) DETACH DELETE n",
                    parameters={"nid": node_id},
                )
            except Exception:
                pass
        else:
            created_at = now

        node_id_esc = node_id.replace("\\", "\\\\").replace("'", "\\'")
        plugin_source_esc = plugin_source.replace("\\", "\\\\").replace("'", "\\'")
        label_esc = label.replace("\\", "\\\\").replace("'", "\\'")
        core_ref_esc = core_ref.replace("\\", "\\\\").replace("'", "\\'")
        data_esc = data_str.replace("\\", "\\\\").replace("'", "\\'")
        created_at_esc = created_at.replace("\\", "\\\\").replace("'", "\\'")
        now_esc = now.replace("\\", "\\\\").replace("'", "\\'")

        self._conn.execute(
            f"CREATE (:PluginNode {{"
            f"node_id: '{node_id_esc}', "
            f"plugin_source: '{plugin_source_esc}', "
            f"label: '{label_esc}', "
            f"core_ref: '{core_ref_esc}', "
            f"data: '{data_esc}', "
            f"created_at: '{created_at_esc}', "
            f"updated_at: '{now_esc}'"
            f"}})"
        )

    def delete_plugin_data(self, plugin_source: str) -> int:
        """Delete all PluginNode + PluginEdge_Node rows for a plugin. Returns count deleted."""
        node_rows = self.execute(
            "MATCH (n:PluginNode {plugin_source: $src}) RETURN count(n) AS cnt",
            params={"src": plugin_source},
        )
        edge_rows = self.execute(
            "MATCH (e:PluginEdge_Node {plugin_source: $src}) RETURN count(e) AS cnt",
            params={"src": plugin_source},
        )
        node_count = node_rows[0]["cnt"] if node_rows else 0
        edge_count = edge_rows[0]["cnt"] if edge_rows else 0

        try:
            self._conn.execute(
                "MATCH (n:PluginNode {plugin_source: $src}) DETACH DELETE n",
                parameters={"src": plugin_source},
            )
        except Exception:
            pass
        try:
            self._conn.execute(
                "MATCH (e:PluginEdge_Node {plugin_source: $src}) DETACH DELETE e",
                parameters={"src": plugin_source},
            )
        except Exception:
            pass

        return node_count + edge_count

    def get_plugin_node_count(self, plugin_source: str) -> int:
        """Return the number of PluginNode rows for a given plugin."""
        rows = self.execute(
            "MATCH (n:PluginNode {plugin_source: $src}) RETURN count(n) AS cnt",
            params={"src": plugin_source},
        )
        return rows[0]["cnt"] if rows else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self) -> "PluginGraphDB":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


class DualDB:
    """Unified access to both core (read-only) and plugin (writable) graphs.

    Backwards compatible: db().execute() proxies to core graph so existing
    plugins continue to work unchanged. Use db().plugin() for write access.
    """

    def __init__(self, core_db_factory: Callable, plugin_db: PluginGraphDB) -> None:
        self._core_factory = core_db_factory
        self._plugin_db = plugin_db

    def __call__(self) -> "DualDB":
        """Callable for backwards compat — plugins do db() to get db object."""
        return self

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        """Proxy to core graph — backwards compat for existing plugins."""
        core = self._core_factory()
        return core.execute(query, params)

    def core(self) -> Any:  # GraphDB
        """Explicit core graph access (read-only)."""
        return self._core_factory()

    def plugin(self) -> PluginGraphDB:
        """Explicit plugin graph access (writable)."""
        return self._plugin_db


def init_plugin_graph(index_dir: Path) -> PluginGraphDB:
    """Initialize or open the plugin graph at index_dir/plugin_graph.kuzu.

    If the file has a stale KuzuDB lock (e.g. from a previous server process
    that was killed without a clean shutdown), the lock state is baked into the
    file header and cannot be cleared without deleting the file.  In that case
    the stale file is removed and a fresh plugin graph is created automatically.
    Stored discoveries are lost, but the server starts successfully.
    """
    import logging as _logging
    db_path = index_dir / "plugin_graph.kuzu"
    try:
        return PluginGraphDB(db_path)
    except Exception as exc:
        msg = str(exc)
        if "Could not set lock" in msg or "lock" in msg.lower():
            # Stale lock in file header — delete and recreate
            _logging.getLogger("laravelgraph").warning(
                "plugin_graph.kuzu has a stale lock (server was killed without clean shutdown). "
                "Deleting stale file and creating a fresh plugin graph. "
                "Previously stored discoveries are lost."
            )
            wal = db_path.parent / (db_path.name + ".wal")
            for f in (db_path, wal):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
            return PluginGraphDB(db_path)
        raise
