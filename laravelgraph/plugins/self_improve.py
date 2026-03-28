"""Proactive plugin self-improvement and auto-generation.

Two functions run on MCP server startup and after laravelgraph analyze:

1. run_improvement_check_all() — improves EXISTING underperforming plugins
2. auto_generate_suggested()   — generates NEW plugins for unmet domain signals

Together these ensure the plugin library continuously grows and improves as
the product evolves.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from laravelgraph.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# How long to wait before re-generating a plugin that was already auto-generated (seconds)
_AUTO_GEN_COOLDOWN_DAYS = 7
# Max plugins to auto-generate in a single run (prevents LLM cost explosion)
_AUTO_GEN_MAX_PER_RUN = 3


def check_and_improve(
    plugin_name: str,
    plugin_path: Path,
    meta_store: Any,        # PluginMetaStore
    project_root: Path,
    core_db: Any,           # GraphDB
    cfg: Any,               # Config
    log: Any | None = None,
) -> tuple[bool, str]:
    """Check if plugin needs improvement and trigger if so.

    Returns: (improved: bool, message: str)
    """
    _log = log or logger

    # Step 1: check if improvement is needed
    if not meta_store.check_improvement_needed(plugin_name):
        return (False, "No improvement needed")

    # Step 2: read existing plugin code
    try:
        existing_code = plugin_path.read_text(encoding="utf-8")
    except OSError as exc:
        message = f"Cannot read plugin file {plugin_path}: {exc}"
        _log.warning("Self-improvement skipped — cannot read plugin", plugin=plugin_name, error=str(exc))
        meta_store.set_cooldown(plugin_name)
        return (False, message)

    # Step 3: extract original description from PLUGIN_MANIFEST
    description = plugin_name  # fallback
    try:
        tree = ast.parse(existing_code, filename=str(plugin_path))
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "PLUGIN_MANIFEST"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Dict)
            ):
                manifest = ast.literal_eval(node.value)
                if isinstance(manifest, dict) and "description" in manifest:
                    description = str(manifest["description"])
                break
    except Exception as exc:
        _log.debug(
            "Could not extract description from PLUGIN_MANIFEST — using plugin name",
            plugin=plugin_name,
            error=str(exc),
        )

    # Step 4: build auto-critique from meta stats
    meta = meta_store.get(plugin_name)
    call_count = getattr(meta, "call_count", 0)
    error_count = getattr(meta, "error_count", 0)
    empty_count = getattr(meta, "empty_result_count", 0)
    followup_count = getattr(meta, "followup_count", 0)

    error_rate = error_count / call_count if call_count > 0 else 0.0
    empty_rate = empty_count / call_count if call_count > 0 else 0.0
    followup_rate = followup_count / call_count if call_count > 0 else 0.0

    critique = f"This plugin has been called {call_count} times. "
    if empty_rate > 0.25:
        critique += f"It returns empty results {empty_rate:.0%} of the time. "
    if error_rate > 0.15:
        critique += f"It errors {error_rate:.0%} of the time. "
    if followup_rate > 0.40:
        critique += (
            f"Agents immediately call other tools after using it {followup_rate:.0%} of the time, "
            f"suggesting it doesn't fully answer questions. "
        )
    critique += "Please improve the Cypher queries and output formatting."

    _log.info(
        "Self-improvement triggered",
        plugin=plugin_name,
        call_count=call_count,
        error_rate=round(error_rate, 3),
        empty_rate=round(empty_rate, 3),
        followup_rate=round(followup_rate, 3),
        critique=critique,
    )

    # Step 5: call generate_plugin
    try:
        from laravelgraph.plugins.generator import generate_plugin
        generated_code, message = generate_plugin(description, project_root, core_db, cfg)
    except ImportError as exc:
        message = f"generator module not available: {exc}"
        _log.error("Self-improvement failed — generator unavailable", plugin=plugin_name, error=str(exc))
        meta_store.set_cooldown(plugin_name)
        return (False, f"Self-improvement failed: {message}")
    except Exception as exc:
        message = str(exc)
        _log.error("Self-improvement failed — generate_plugin raised", plugin=plugin_name, error=str(exc), exc_info=True)
        meta_store.set_cooldown(plugin_name)
        return (False, f"Self-improvement failed: {message}")

    # Step 6: write new code if generation succeeded
    if generated_code:
        try:
            plugin_path.write_text(generated_code, encoding="utf-8")
        except OSError as exc:
            _log.error("Self-improvement failed — cannot write plugin", plugin=plugin_name, error=str(exc))
            meta_store.set_cooldown(plugin_name)
            return (False, f"Self-improvement failed: cannot write file: {exc}")

        # Update meta
        meta_store.increment_self_improvement_count(plugin_name)
        meta_store.set_last_improved_at(plugin_name, time.time())
        meta_store.set_cooldown(plugin_name)

        _log.info(
            "Plugin self-improved successfully",
            plugin=plugin_name,
            path=str(plugin_path),
        )
        return (True, "Plugin improved successfully")

    # Step 7: generation returned no code — set cooldown anyway
    meta_store.set_cooldown(plugin_name)
    _log.warning("Self-improvement generated no code", plugin=plugin_name, detail=message)
    return (False, f"Self-improvement failed: {message}")


def run_improvement_check_all(
    plugins_dir: Path,
    meta_store: Any,
    project_root: Path,
    cfg: Any,
    log: Any | None = None,
) -> list[tuple[str, bool, str]]:
    """Check all active plugins for improvement needs. Called on server startup.

    Returns list of (plugin_name, improved, message) for plugins that were checked.
    Only checks plugins where improvement_needed returns True.
    """
    _log = log or logger
    results: list[tuple[str, bool, str]] = []

    for plugin_path in sorted(plugins_dir.glob("*.py")):
        plugin_name = plugin_path.stem

        # Get meta — skip if not found or not active
        try:
            meta = meta_store.get(plugin_name)
        except Exception as exc:
            _log.debug(
                "Skipping plugin — no meta entry",
                plugin=plugin_name,
                error=str(exc),
            )
            continue

        is_active = getattr(meta, "active", True)
        if not is_active:
            _log.debug("Skipping inactive plugin", plugin=plugin_name)
            continue

        needs_improvement = False
        try:
            needs_improvement = meta_store.check_improvement_needed(plugin_name)
        except Exception as exc:
            _log.debug(
                "check_improvement_needed raised — skipping",
                plugin=plugin_name,
                error=str(exc),
            )
            continue

        if not needs_improvement:
            continue

        _log.info("Plugin queued for self-improvement check", plugin=plugin_name)

        # core_db is not available at this call site; pass None — check_and_improve
        # handles ImportError from generator gracefully.
        improved, message = check_and_improve(
            plugin_name=plugin_name,
            plugin_path=plugin_path,
            meta_store=meta_store,
            project_root=project_root,
            core_db=None,
            cfg=cfg,
            log=_log,
        )
        results.append((plugin_name, improved, message))

    return results


def auto_generate_suggested(
    plugins_dir: Path,
    meta_store: Any,          # PluginMetaStore
    project_root: Path,
    core_db: Any,             # GraphDB
    cfg: Any,                 # Config
    log: Any | None = None,
    max_per_run: int = _AUTO_GEN_MAX_PER_RUN,
) -> list[tuple[str, bool, str]]:
    """Detect applicable domain recipes and auto-generate missing plugins.

    Runs after ``laravelgraph analyze`` and on MCP server startup.
    Each recipe has a 7-day cooldown so it won't be re-generated constantly.

    Returns list of (plugin_name, generated: bool, message).
    """
    _log = log or logger
    results: list[tuple[str, bool, str]] = []

    # Step 1: detect applicable recipes from the graph
    try:
        from laravelgraph.plugins.suggest import detect_applicable_recipes
    except ImportError as exc:
        _log.debug("auto_generate_suggested skipped — suggest module unavailable", error=str(exc))
        return results

    try:
        recipes = detect_applicable_recipes(core_db)
    except Exception as exc:
        _log.warning("auto_generate_suggested skipped — recipe detection failed", error=str(exc))
        return results

    if not recipes:
        _log.debug("auto_generate_suggested: no applicable recipes detected")
        return results

    _log.info("Auto-generation: detected applicable recipes", count=len(recipes))

    generated_count = 0
    for recipe in recipes:
        if generated_count >= max_per_run:
            _log.debug("Auto-generation cap reached", cap=max_per_run)
            break

        plugin_name: str = recipe.get("name", "")
        if not plugin_name:
            continue

        # Step 2: skip if plugin already exists on disk
        plugin_file = plugins_dir / f"{plugin_name}.py"
        if plugin_file.exists():
            _log.debug("Auto-generation skipped — plugin already exists", plugin=plugin_name)
            continue

        # Step 3: check cooldown via meta store (prevents regeneration within 7 days)
        try:
            meta = meta_store.get(plugin_name)
            if meta is not None:
                cooldown_until = getattr(meta, "improvement_cooldown_until", 0.0) or 0.0
                if cooldown_until > time.time():
                    remaining_h = (cooldown_until - time.time()) / 3600
                    _log.debug(
                        "Auto-generation skipped — cooldown active",
                        plugin=plugin_name,
                        remaining_hours=round(remaining_h, 1),
                    )
                    continue
        except Exception:
            pass  # no meta yet is fine — proceed with generation

        # Step 4: generate the plugin
        description: str = recipe.get("description", plugin_name)
        _log.info(
            "Auto-generating plugin from recipe",
            plugin=plugin_name,
            description=description[:80],
        )

        try:
            from laravelgraph.plugins.generator import generate_plugin
            generated_code, message = generate_plugin(description, project_root, core_db, cfg)
        except ImportError as exc:
            msg = f"generator unavailable: {exc}"
            _log.warning("Auto-generation failed — generator not importable", plugin=plugin_name, error=msg)
            results.append((plugin_name, False, msg))
            continue
        except Exception as exc:
            msg = str(exc)
            _log.warning("Auto-generation failed — generate_plugin raised", plugin=plugin_name, error=msg)
            results.append((plugin_name, False, msg))
            continue

        if not generated_code:
            _log.warning("Auto-generation produced no code", plugin=plugin_name, detail=message)
            # Set cooldown so we don't retry immediately
            try:
                meta_store.set_cooldown(plugin_name, hours=_AUTO_GEN_COOLDOWN_DAYS * 24)
            except Exception:
                pass
            results.append((plugin_name, False, f"No code generated: {message}"))
            continue

        # Step 5: write plugin to disk
        try:
            plugins_dir.mkdir(parents=True, exist_ok=True)
            plugin_file.write_text(generated_code, encoding="utf-8")
        except OSError as exc:
            msg = f"cannot write plugin file: {exc}"
            _log.error("Auto-generation failed — write error", plugin=plugin_name, error=msg)
            results.append((plugin_name, False, msg))
            continue

        # Step 6: register in meta store with 7-day cooldown
        try:
            from laravelgraph.plugins.meta import PluginMeta
            existing = meta_store.get(plugin_name)
            if existing is None:
                meta_store.set(plugin_name, PluginMeta(name=plugin_name, status="active"))
            meta_store.set_cooldown(plugin_name, hours=_AUTO_GEN_COOLDOWN_DAYS * 24)
            meta_store.increment_self_improvement_count(plugin_name)
        except Exception as exc:
            _log.debug("Auto-generation meta registration failed (non-fatal)", plugin=plugin_name, error=str(exc))

        generated_count += 1
        _log.info(
            "Plugin auto-generated successfully",
            plugin=plugin_name,
            path=str(plugin_file),
        )
        results.append((plugin_name, True, f"Generated: {plugin_file.name}"))

    return results
