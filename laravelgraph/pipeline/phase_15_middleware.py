"""Phase 15 — Middleware Resolution.

Resolve the complete middleware stack for every route by parsing the
Kernel.php (Laravel 9/10) or bootstrap/app.php (Laravel 11+) to extract
global middleware, middleware groups, and aliases.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# PHP array value patterns
_STRING_VAL_PATTERN = re.compile(r"['\"]([^'\"]+)['\"]")
_CLASS_REF_PATTERN = re.compile(r"([\w\\]+)::class")
_PROTECTED_ARRAY_PATTERN = re.compile(
    r"protected\s+\\\$(\w+)\s*=\s*\[([^\]]*(?:\[[^\]]*\][^\]]*)*)\]",
    re.DOTALL,
)
# Named array within $middlewareGroups
_GROUP_ENTRY_PATTERN = re.compile(
    r"['\"](\w+)['\"]\s*=>\s*\[([^\]]*)\]",
    re.DOTALL,
)


def _extract_class_or_string(val: str) -> str:
    """Return the class FQN or string alias from a PHP value snippet."""
    val = val.strip()
    class_m = _CLASS_REF_PATTERN.search(val)
    if class_m:
        return class_m.group(1).lstrip("\\")
    string_m = _STRING_VAL_PATTERN.search(val)
    if string_m:
        return string_m.group(1)
    return val.strip("'\"")


def _parse_php_array_values(raw: str) -> list[str]:
    """Extract a flat list of values from a PHP array body string."""
    results: list[str] = []
    # Split on commas not inside nested brackets
    depth = 0
    current = ""
    for ch in raw:
        if ch in ("[", "{"):
            depth += 1
            current += ch
        elif ch in ("]", "}"):
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            val = _extract_class_or_string(current.strip())
            if val:
                results.append(val)
            current = ""
        else:
            current += ch
    if current.strip():
        val = _extract_class_or_string(current.strip())
        if val:
            results.append(val)
    return [v for v in results if v]


def _parse_kernel_php(source: str) -> dict[str, Any]:
    """Parse Kernel.php to extract global middleware, groups, and aliases."""
    result: dict[str, Any] = {
        "global": [],
        "groups": {},
        "aliases": {},
    }

    for m in _PROTECTED_ARRAY_PATTERN.finditer(source):
        prop_name = m.group(1)
        array_body = m.group(2)

        if prop_name == "middleware":
            result["global"] = _parse_php_array_values(array_body)

        elif prop_name in ("middlewareGroups",):
            for group_m in _GROUP_ENTRY_PATTERN.finditer(array_body):
                group_name = group_m.group(1)
                group_body = group_m.group(2)
                result["groups"][group_name] = _parse_php_array_values(group_body)

        elif prop_name in ("routeMiddleware", "middlewareAliases"):
            # Key => value pairs
            pair_pattern = re.compile(
                r"['\"](\w+)['\"]\s*=>\s*([\w\\:\'\"]+)",
                re.DOTALL,
            )
            for pair_m in pair_pattern.finditer(array_body):
                alias = pair_m.group(1)
                concrete = _extract_class_or_string(pair_m.group(2))
                result["aliases"][alias] = concrete

    return result


def _parse_bootstrap_app_php(source: str) -> dict[str, Any]:
    """Parse Laravel 11+ bootstrap/app.php for middleware configuration."""
    result: dict[str, Any] = {
        "global": [],
        "groups": {},
        "aliases": {},
    }

    # Laravel 11 uses ->withMiddleware(function(Middleware $middleware) { ... })
    # Look for $middleware->append(...) for global middleware
    append_pat = re.compile(r"\\\$middleware->append\s*\(\s*([^)]+)\)")
    for m in append_pat.finditer(source):
        val = _extract_class_or_string(m.group(1))
        if val:
            result["global"].append(val)

    prepend_pat = re.compile(r"\\\$middleware->prepend\s*\(\s*([^)]+)\)")
    for m in prepend_pat.finditer(source):
        val = _extract_class_or_string(m.group(1))
        if val:
            result["global"].insert(0, val)

    # Groups: $middleware->group('web', [...])
    group_pat = re.compile(
        r"\\\$middleware->group\s*\(\s*['\"](\w+)['\"]\s*,\s*\[([^\]]*)\]\s*\)"
    )
    for m in group_pat.finditer(source):
        group_name = m.group(1)
        result["groups"][group_name] = _parse_php_array_values(m.group(2))

    # Aliases: $middleware->alias(['auth' => Authenticate::class])
    alias_body_pat = re.compile(
        r"\\\$middleware->alias\s*\(\s*\[([^\]]*)\]\s*\)"
    )
    alias_pair_pat = re.compile(r"['\"](\w+)['\"]\s*=>\s*([\w\\:\'\"]+)")
    for m in alias_body_pat.finditer(source):
        for pair_m in alias_pair_pat.finditer(m.group(1)):
            alias = pair_m.group(1)
            concrete = _extract_class_or_string(pair_m.group(2))
            result["aliases"][alias] = concrete

    return result


def _load_kernel_config(project_root: Path) -> dict[str, Any]:
    """Try to load middleware configuration from Kernel.php or bootstrap/app.php."""
    # Laravel 9/10: app/Http/Kernel.php
    kernel_path = project_root / "app" / "Http" / "Kernel.php"
    if kernel_path.exists():
        try:
            source = kernel_path.read_text(encoding="utf-8", errors="replace")
            config = _parse_kernel_php(source)
            logger.debug(
                "Parsed Kernel.php",
                global_count=len(config["global"]),
                groups=list(config["groups"].keys()),
                aliases=len(config["aliases"]),
            )
            return config
        except Exception as exc:
            logger.warning("Failed to parse Kernel.php", error=str(exc))

    # Laravel 11+: bootstrap/app.php
    bootstrap_path = project_root / "bootstrap" / "app.php"
    if bootstrap_path.exists():
        try:
            source = bootstrap_path.read_text(encoding="utf-8", errors="replace")
            config = _parse_bootstrap_app_php(source)
            logger.debug(
                "Parsed bootstrap/app.php (Laravel 11+)",
                global_count=len(config["global"]),
                groups=list(config["groups"].keys()),
                aliases=len(config["aliases"]),
            )
            return config
        except Exception as exc:
            logger.warning("Failed to parse bootstrap/app.php", error=str(exc))

    return {"global": [], "groups": {}, "aliases": {}}


def _expand_middleware(
    raw_stack: list[str],
    groups: dict[str, list[str]],
    aliases: dict[str, str],
) -> list[str]:
    """Expand group names and resolve aliases to FQNs in the stack."""
    expanded: list[str] = []
    for item in raw_stack:
        # Handle throttle:60,1 style parameters
        base = item.split(":")[0] if ":" in item else item

        if base in groups:
            # Expand the group, preserving parameters on individual items
            for group_item in groups[base]:
                group_base = group_item.split(":")[0] if ":" in group_item else group_item
                resolved = aliases.get(group_base, group_item)
                # Re-attach params if present
                if ":" in group_item:
                    params = group_item[group_item.index(":"):]
                    resolved = resolved.split(":")[0] + params
                expanded.append(resolved)
        else:
            resolved = aliases.get(base, item)
            if ":" in item and resolved != item:
                # Re-attach params
                params = item[item.index(":"):]
                resolved = resolved.split(":")[0] + params
            expanded.append(resolved)

    return expanded


def _ensure_middleware_node(db: Any, fqn_or_alias: str, class_map: dict[str, Path]) -> str:
    """Ensure a Middleware node exists for the given FQN/alias, return its node_id."""
    # Strip parameter part (e.g. throttle:60,1 → throttle)
    base = fqn_or_alias.split(":")[0] if ":" in fqn_or_alias else fqn_or_alias
    nid = make_node_id("middleware", base)

    file_path = ""
    if base in class_map:
        file_path = str(class_map[base])

    short_name = base.split("\\")[-1] if "\\" in base else base

    try:
        db._insert_node("Middleware", {
            "node_id": nid,
            "name": short_name,
            "fqn": base,
            "file_path": file_path,
            "alias": fqn_or_alias if fqn_or_alias != base else "",
            "middleware_group": "",
        })
    except Exception:
        pass  # Already exists

    return nid


def run(ctx: PipelineContext) -> None:
    """Resolve the complete middleware stack for every route."""
    db = ctx.db
    project_root = ctx.project_root
    class_map = ctx.class_map

    kernel_config = _load_kernel_config(project_root)
    global_middleware = kernel_config["global"]
    groups = kernel_config["groups"]
    aliases = kernel_config["aliases"]

    middleware_resolved = 0

    # Fetch all Route nodes
    try:
        route_rows = db.execute(
            "MATCH (r:Route) RETURN r.node_id AS nid, r.middleware_stack AS mw_stack"
        )
    except Exception as exc:
        logger.error("Failed to fetch Route nodes", error=str(exc))
        return

    for row in route_rows:
        route_nid = row.get("nid") or ""
        mw_raw = row.get("mw_stack") or "[]"

        if not route_nid:
            continue

        try:
            route_middleware: list[str] = json.loads(mw_raw) if mw_raw else []
        except (json.JSONDecodeError, TypeError):
            route_middleware = []

        # Build complete stack: global + route-specific, then expand groups/aliases
        full_stack = global_middleware + route_middleware
        expanded = _expand_middleware(full_stack, groups, aliases)

        for order, mw_item in enumerate(expanded):
            try:
                # Extract parameters (e.g. throttle:60,1 → params="60,1")
                if ":" in mw_item:
                    colon_idx = mw_item.index(":")
                    params_str = json.dumps(mw_item[colon_idx + 1:].split(","))
                    mw_fqn = mw_item[:colon_idx]
                else:
                    params_str = "[]"
                    mw_fqn = mw_item

                if not mw_fqn:
                    continue

                mw_nid = _ensure_middleware_node(db, mw_fqn, class_map)

                db.upsert_rel(
                    "APPLIES_MIDDLEWARE",
                    "Route",
                    route_nid,
                    "Middleware",
                    mw_nid,
                    {"middleware_order": order, "parameters": params_str},
                )
                middleware_resolved += 1

            except Exception as exc:
                logger.debug(
                    "Failed to resolve middleware for route",
                    route=route_nid,
                    middleware=mw_item,
                    error=str(exc),
                )

    ctx.stats["middleware_resolved"] = middleware_resolved

    logger.info(
        "Middleware resolution complete",
        middleware_resolved=middleware_resolved,
        global_middleware=len(global_middleware),
        groups=len(groups),
        aliases=len(aliases),
    )
