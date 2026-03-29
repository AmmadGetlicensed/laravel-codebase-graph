"""Plugin loader — validates, wraps, and executes plugins safely."""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from typing import Any

from laravelgraph.core.graph import GraphDB
from laravelgraph.logging import get_logger
from laravelgraph.plugins.validator import PluginValidationError, validate_plugin
from laravelgraph.plugins.plugin_graph import DualDB, PluginGraphDB
from laravelgraph.plugins.meta import PluginMeta, PluginMetaStore

logger = get_logger(__name__)


def scan_plugin_manifests(plugins_dir: Path) -> list[dict]:
    """Scan plugins_dir and return manifest info for every valid plugin file.

    Uses AST parsing + regex — no imports, no side effects.  Safe to call before
    the FastMCP server is created.

    Each returned dict has:
        name        str   plugin slug  (e.g. "user-explorer")
        description str   one-line description from PLUGIN_MANIFEST
        tool_prefix str   prefix string  (e.g. "usr_")
        tool_names  list  all @mcp.tool function names defined in the file
        path        Path  absolute path to the plugin .py file
    """
    results: list[dict] = []
    if not plugins_dir.exists():
        return results

    for path in sorted(plugins_dir.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue

        # ── Parse PLUGIN_MANIFEST via AST ────────────────────────────────
        name = ""
        description = ""
        tool_prefix = ""
        try:
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == "PLUGIN_MANIFEST"
                        for t in node.targets
                    )
                ):
                    manifest = ast.literal_eval(node.value)
                    name = manifest.get("name", "")
                    description = manifest.get("description", "")
                    tool_prefix = manifest.get("tool_prefix", "")
                    break
        except Exception:
            pass

        if not name:
            continue

        # ── Find tool function names via regex (no import) ───────────────
        # Generated plugins define tools as `def {prefix}something(` inside
        # register_tools.  Grab all such definitions.
        prefix_stem = tool_prefix.rstrip("_")
        tool_names: list[str] = []
        if prefix_stem:
            for m in re.finditer(r"^\s{4}def\s+(" + re.escape(prefix_stem) + r"\w+)\s*\(", source, re.MULTILINE):
                tool_names.append(m.group(1))

        results.append({
            "name": name,
            "description": description,
            "tool_prefix": tool_prefix,
            "tool_names": tool_names,
            "path": path,
        })

    return results


class _ToolCollector:
    """Minimal MCP stand-in used by run_plugin_tool to collect tool functions.

    When a plugin calls ``register_tools(mcp, db=...)``, each ``@mcp.tool()``
    decorated function is stored in ``self.tools`` keyed by function name.
    Unknown attribute accesses are silently swallowed so plugins that call
    other mcp methods (e.g. mcp.resource) don't raise.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        """Decorator factory — just stores the function, no FastMCP registration."""
        def _decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn
        return _decorator

    def __getattr__(self, name: str) -> Any:
        # Silently ignore any other mcp method calls (resource, mount, etc.).
        # Return a no-op decorator factory so @mcp.resource("path") works without raising.
        def _noop_factory(*args: Any, **kwargs: Any) -> Any:
            def _noop_decorator(fn: Any) -> Any:
                return fn
            return _noop_decorator
        return _noop_factory


class PluginSafeDB:
    """Proxy around :class:`GraphDB` that blocks destructive operations and
    auto-tags all node writes with the plugin's name.
    """

    def __init__(self, real_db: GraphDB, plugin_name: str) -> None:
        self._db = real_db
        self._plugin_name = plugin_name

    def execute(self, query: str, *args: Any, **kwargs: Any) -> Any:
        """Block destructive Cypher operations."""
        upper = query.strip().upper()
        for forbidden in ("DELETE ", "DROP ", "TRUNCATE "):
            if forbidden in upper:
                raise PermissionError(
                    f"Plugin '{self._plugin_name}' attempted forbidden operation: "
                    f"{forbidden.strip()}"
                )
        return self._db.execute(query, *args, **kwargs)

    def upsert_node(self, label: str, props: dict, *args: Any, **kwargs: Any) -> Any:
        """Enforce plugin_source tagging on all node writes."""
        if "plugin_source" not in props:
            props = {**props, "plugin_source": self._plugin_name}
        return self._db.upsert_node(label, props, *args, **kwargs)

    def upsert_rel(self, *args: Any, **kwargs: Any) -> Any:
        return self._db.upsert_rel(*args, **kwargs)

    def _insert_node(self, *args: Any, **kwargs: Any) -> Any:
        # Not allowed from plugins — must use upsert_node
        raise PermissionError(
            f"Plugin '{self._plugin_name}': use upsert_node() instead of _insert_node()"
        )

    # Passthrough for read-only ops
    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class PluginSafeMCP:
    """Proxy around a FastMCP instance that validates tool name prefixes before
    registering them, preventing namespace collisions with core LaravelGraph tools.
    """

    def __init__(self, real_mcp: Any, plugin_name: str, tool_prefix: str) -> None:
        self._mcp = real_mcp
        self._plugin_name = plugin_name
        self._tool_prefix = tool_prefix

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        """Decorator factory that validates tool names before registering."""
        real_decorator = self._mcp.tool(*args, **kwargs)
        plugin_name = self._plugin_name
        prefix = self._tool_prefix

        def enforcing_decorator(func: Any) -> Any:
            if not func.__name__.startswith(prefix):
                raise PermissionError(
                    f"Plugin '{plugin_name}': tool '{func.__name__}' must start with "
                    f"'{prefix}' (declared in PLUGIN_MANIFEST tool_prefix)"
                )
            if func.__name__.startswith("laravelgraph_"):
                raise PermissionError(
                    f"Plugin '{plugin_name}': tool name '{func.__name__}' cannot use "
                    f"the reserved 'laravelgraph_' prefix"
                )
            return real_decorator(func)

        return enforcing_decorator

    def __getattr__(self, name: str) -> Any:
        return getattr(self._mcp, name)


def _import_plugin_module(path: Path, module_name: str) -> Any:
    """Load a Python file as a module without adding it to sys.modules permanently."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def load_pipeline_plugins(plugins_dir: Path, ctx: Any, log: Any | None = None) -> list[str]:
    """Load and execute all pipeline plugins from *plugins_dir*.

    Each ``.py`` file may expose a ``run(ctx)`` function that is called with a
    :class:`PluginSafeDB`-wrapped context so destructive operations are blocked.

    Returns the list of successfully loaded plugin names.
    """
    _log = log or logger
    loaded: list[str] = []

    for plugin_path in sorted(plugins_dir.glob("*.py")):
        # ── Validate ────────────────────────────────────────────────────────
        _log.debug("Pipeline plugin validation starting", plugin=plugin_path.name)
        try:
            manifest, warnings = validate_plugin(plugin_path)
        except PluginValidationError as exc:
            _log.error(
                "Pipeline plugin failed validation — skipping",
                plugin=plugin_path.name,
                errors=exc.errors,
                error_count=len(exc.errors),
            )
            continue

        _log.debug("Pipeline plugin validation passed", plugin=plugin_path.name)

        for w in warnings:
            _log.warning(
                "Pipeline plugin validation warning",
                plugin=plugin_path.name,
                warning=w,
            )

        plugin_name: str = manifest["name"]

        # ── Check requires_phases ────────────────────────────────────────────
        requires_phases: list[str] = manifest.get("requires_phases", [])
        if requires_phases:
            for required in requires_phases:
                if required not in ctx.stats:
                    _log.warning(
                        "Pipeline plugin requires phase that may not have run",
                        plugin=plugin_name,
                        required_phase=required,
                    )

        # ── Import ───────────────────────────────────────────────────────────
        _log.debug("Pipeline plugin import starting", plugin=plugin_path.name)
        try:
            module = _import_plugin_module(
                plugin_path,
                f"laravelgraph_plugin_{plugin_path.stem}",
            )
        except Exception as exc:
            _log.error(
                "Pipeline plugin import failed — skipping",
                plugin=plugin_path.name,
                error=str(exc),
                exc_info=True,
            )
            continue

        _log.debug("Pipeline plugin import succeeded", plugin=plugin_path.name)

        if not hasattr(module, "run"):
            _log.warning(
                "Pipeline plugin has no run() function — skipping",
                plugin=plugin_name,
            )
            continue

        # ── Execute with safe DB wrapper ─────────────────────────────────────
        _log.debug("Pipeline plugin registration starting", plugin=plugin_name)
        real_db = ctx.db
        safe_db = PluginSafeDB(real_db, plugin_name)
        ctx.db = safe_db
        try:
            module.run(ctx)
            loaded.append(plugin_name)
            _log.info("Pipeline plugin executed successfully", plugin=plugin_name)
            _log.debug("Pipeline plugin registration succeeded", plugin=plugin_name)
        except Exception as exc:
            _log.error(
                "Pipeline plugin run() raised an exception",
                plugin=plugin_name,
                error=str(exc),
                exc_info=True,
            )
            _log.warning("Pipeline plugin registration failed", plugin=plugin_name, error=str(exc))
        finally:
            ctx.db = real_db

    return loaded


def load_mcp_plugins(
    plugins_dir: Path,
    mcp: Any,
    log: Any | None = None,
    db_factory: Any | None = None,
    plugin_db: Any | None = None,       # PluginGraphDB
    meta_store: Any | None = None,      # PluginMetaStore
    sql_db_factory: Any | None = None,  # callable() → pymysql connection
) -> list[str]:
    """Register MCP tools from all plugins in *plugins_dir*.

    Each ``.py`` file may expose a ``register_tools(mcp, db=None)`` function.
    The *mcp* object passed to it is wrapped in :class:`PluginSafeMCP` to enforce
    tool-name prefix rules at registration time.

    *db_factory* is an optional callable that returns a read-only :class:`GraphDB`.
    It is passed as the ``db`` keyword argument to ``register_tools`` so plugin
    tools can query the graph.

    *plugin_db* is an optional :class:`PluginGraphDB` instance. When provided
    together with *db_factory*, the two are wrapped in a :class:`DualDB` that is
    passed to ``register_tools`` instead of *db_factory* directly.

    *meta_store* is an optional :class:`PluginMetaStore`. When provided, newly
    loaded plugins are registered on first load.

    *sql_db_factory* is an optional callable that returns a live pymysql connection.
    Passed as the ``sql_db`` keyword argument to ``register_tools`` when the plugin
    declares that parameter, enabling plugins to run raw SQL queries.

    Returns the list of successfully loaded plugin names.
    """
    _log = log or logger
    loaded: list[str] = []

    for plugin_path in sorted(plugins_dir.glob("*.py")):
        # ── Validate ────────────────────────────────────────────────────────
        _log.debug("MCP plugin validation starting", plugin=plugin_path.name)
        try:
            manifest, warnings = validate_plugin(plugin_path)
        except PluginValidationError as exc:
            _log.warning(
                "MCP plugin failed validation — skipping",
                plugin=plugin_path.name,
                errors=exc.errors,
                error_count=len(exc.errors),
            )
            continue

        _log.debug("MCP plugin validation passed", plugin=plugin_path.name)

        for w in warnings:
            _log.warning(
                "MCP plugin validation warning",
                plugin=plugin_path.name,
                warning=w,
            )

        plugin_name: str = manifest["name"]
        tool_prefix: str = manifest["tool_prefix"]

        # ── Auto-migrate pre-redesign plugin issues ───────────────────────────
        try:
            from laravelgraph.plugins.generator import (
                migrate_plugin_store_tool,
                migrate_plugin_cypher_properties,
            )
            if migrate_plugin_store_tool(plugin_path, tool_prefix, plugin_name):
                _log.info(
                    "Migrated store_discoveries to new signature (findings: str)",
                    plugin=plugin_name,
                )
            cypher_fixes = migrate_plugin_cypher_properties(plugin_path)
            if cypher_fixes:
                _log.info(
                    "Migrated Cypher property names",
                    plugin=plugin_name,
                    fixes=cypher_fixes,
                )
        except Exception as _mig_exc:
            _log.warning("Plugin migration failed", plugin=plugin_name, error=str(_mig_exc))

        # ── Import ───────────────────────────────────────────────────────────
        _log.debug("MCP plugin import starting", plugin=plugin_path.name)
        try:
            module = _import_plugin_module(
                plugin_path,
                f"laravelgraph_plugin_tool_{plugin_path.stem}",
            )
        except Exception as exc:
            _log.warning(
                "MCP plugin import failed — skipping",
                plugin=plugin_path.name,
                error=str(exc),
            )
            continue

        _log.debug("MCP plugin import succeeded", plugin=plugin_path.name)

        if not hasattr(module, "register_tools"):
            _log.warning(
                "MCP plugin has no register_tools() function — skipping",
                plugin=plugin_name,
            )
            continue

        # ── First-time registration in meta store ─────────────────────────
        if meta_store is not None and not meta_store.get(plugin_name):
            from datetime import datetime, timezone
            meta_store.set(PluginMeta(
                name=plugin_name,
                status="active",
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            _log.info("MCP plugin registered in meta store", plugin=plugin_name)

        # ── Determine what db object to pass ──────────────────────────────
        if db_factory is not None and plugin_db is not None:
            db_arg: Any = DualDB(db_factory, plugin_db)
        elif db_factory is not None:
            db_arg = db_factory
        else:
            db_arg = None

        # ── Register with safe MCP wrapper ───────────────────────────────────
        _log.debug("MCP plugin registration starting", plugin=plugin_name, tool_prefix=tool_prefix)
        safe_mcp = PluginSafeMCP(mcp, plugin_name, tool_prefix)
        try:
            import inspect
            sig = inspect.signature(module.register_tools)
            reg_kwargs: dict = {}
            if "db" in sig.parameters and db_arg is not None:
                reg_kwargs["db"] = db_arg
            if "sql_db" in sig.parameters and sql_db_factory is not None:
                reg_kwargs["sql_db"] = sql_db_factory
            module.register_tools(safe_mcp, **reg_kwargs)
            loaded.append(plugin_name)
            _log.info(
                "MCP plugin tools registered successfully",
                plugin=plugin_name,
                has_plugin_db=plugin_db is not None,
            )
            _log.debug("MCP plugin registration succeeded", plugin=plugin_name)
        except Exception as exc:
            _log.warning(
                "MCP plugin register_tools() raised an exception",
                plugin=plugin_name,
                error=str(exc),
            )
            _log.warning("MCP plugin registration failed", plugin=plugin_name, error=str(exc))

    return loaded
