"""Phase 14 — Route Analysis.

Parse Laravel route files (routes/web.php, routes/api.php, etc.) and build
Route nodes with ROUTES_TO relationships to controller methods.
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

# HTTP methods supported by Laravel router
_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "any", "match")

# RESTful resource actions
_RESOURCE_ACTIONS = ["index", "create", "store", "show", "edit", "update", "destroy"]
_API_RESOURCE_ACTIONS = ["index", "store", "show", "update", "destroy"]  # no create/edit

# Regex patterns for route definitions
_ROUTE_SIMPLE_PATTERN = re.compile(
    r"Route\s*::\s*(" + "|".join(_HTTP_METHODS) + r")\s*\(\s*"
    r"['\"]([^'\"]+)['\"]\s*,\s*"  # URI
    r"([^)]+)\)",  # handler
    re.IGNORECASE,
)

_ROUTE_RESOURCE_PATTERN = re.compile(
    r"Route\s*::\s*(resource|apiResource)\s*\(\s*"
    r"['\"]([^'\"]+)['\"]\s*,\s*"  # URI prefix
    r"([A-Za-z_\\:]+)(?:::class)?\s*"  # controller
    r"([^)]*)\)",  # optional extra args
    re.IGNORECASE,
)

_ROUTE_NAME_PATTERN = re.compile(r"->name\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_MIDDLEWARE_PATTERN = re.compile(r"->middleware\s*\(\s*([^)]+)\)")
_PREFIX_PATTERN = re.compile(r"->prefix\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_DOMAIN_PATTERN = re.compile(r"->domain\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

# Controller@method string style
_CONTROLLER_AT_PATTERN = re.compile(r"['\"]([A-Za-z_\\]+)@(\w+)['\"]")
# Array style: [SomeController::class, 'method']
_CONTROLLER_ARRAY_PATTERN = re.compile(
    r"\[\s*([A-Za-z_\\]+)(?:::class)?\s*,\s*['\"](\w+)['\"]\s*\]"
)
# Invokable: just SomeController::class
_CONTROLLER_INVOKABLE_PATTERN = re.compile(r"([A-Za-z_\\]+)::class")

# USE statement parser to resolve short class names
_USE_PATTERN = re.compile(
    r"^use\s+([\w\\]+?)(?:\s+as\s+(\w+))?\s*;",
    re.MULTILINE,
)


def _parse_use_statements(source: str) -> dict[str, str]:
    """Return a dict mapping short name (or alias) → fully-qualified class name."""
    uses: dict[str, str] = {}
    for m in _USE_PATTERN.finditer(source):
        fqn = m.group(1)
        alias = m.group(2) or fqn.split("\\")[-1]
        uses[alias] = fqn
    return uses


def _extract_middleware_list(raw: str) -> list[str]:
    """Parse a raw middleware() argument into a list of middleware strings."""
    # Strip surrounding brackets/quotes
    raw = raw.strip()
    # Array style: ['auth', 'throttle:60,1']
    if raw.startswith("["):
        raw = raw[1:].rstrip("]")
    items = []
    for part in re.split(r",(?![^[]*\])", raw):
        part = part.strip().strip("'\"")
        if part:
            items.append(part)
    return items


def _resolve_controller_fqn(
    short_name: str,
    use_map: dict[str, str],
    class_map: dict[str, Path],
    composer_namespace: str,
) -> str:
    """Resolve a short class name or partial FQN to a fully-qualified name."""
    # Already fully qualified
    if "\\" in short_name and short_name.startswith("\\"):
        return short_name.lstrip("\\")

    # Check use statements
    short = short_name.split("\\")[-1]
    if short in use_map:
        return use_map[short]

    # Try direct lookup in class_map
    if short_name in class_map:
        return short_name

    # Try common controller namespace
    for ns in [
        f"App\\Http\\Controllers\\{short_name}",
        f"{composer_namespace}Http\\Controllers\\{short_name}",
    ]:
        if ns in class_map:
            return ns

    return short_name


def _parse_route_group_context(source: str) -> list[dict[str, Any]]:
    """Return context dicts for route groups (prefix, middleware, domain).

    This is a simplified parser that handles common patterns. Nested groups are
    flattened. Each context applies to the line range within the group closure.
    """
    contexts = []
    group_pattern = re.compile(
        r"Route\s*::\s*(?:group\s*\(\s*\[([^\]]*)\]\s*,|"
        r"((?:prefix|middleware|domain)\s*\([^)]*\)(?:\s*->\s*(?:prefix|middleware|domain)\s*\([^)]*\))*)\s*->group\s*\()"
        r"\s*function\s*\(\s*\)\s*\{",
        re.DOTALL,
    )

    prefix_pat = re.compile(r"['\"]prefix['\"]\s*=>\s*['\"]([^'\"]+)['\"]")
    mw_pat = re.compile(r"['\"]middleware['\"]\s*=>\s*(\[[^\]]*\]|['\"][^'\"]*['\"])")
    domain_pat = re.compile(r"['\"]domain['\"]\s*=>\s*['\"]([^'\"]+)['\"]")

    inline_prefix_pat = re.compile(r"->prefix\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
    inline_mw_pat = re.compile(r"->middleware\s*\(\s*([^)]+)\)")

    for m in group_pattern.finditer(source):
        ctx_raw = m.group(1) or m.group(2) or ""
        ctx: dict[str, Any] = {"prefix": "", "middleware": [], "domain": ""}

        prefix_m = prefix_pat.search(ctx_raw)
        if prefix_m:
            ctx["prefix"] = prefix_m.group(1)
        else:
            inline_prefix_m = inline_prefix_pat.search(ctx_raw)
            if inline_prefix_m:
                ctx["prefix"] = inline_prefix_m.group(1)

        mw_m = mw_pat.search(ctx_raw)
        if mw_m:
            ctx["middleware"] = _extract_middleware_list(mw_m.group(1))
        else:
            for inline_mw_m in inline_mw_pat.finditer(ctx_raw):
                ctx["middleware"].extend(_extract_middleware_list(inline_mw_m.group(1)))

        domain_m = domain_pat.search(ctx_raw)
        if domain_m:
            ctx["domain"] = domain_m.group(1)

        ctx["start"] = m.start()
        contexts.append(ctx)

    return contexts


def _get_group_context_for_pos(contexts: list[dict[str, Any]], pos: int) -> dict[str, Any]:
    """Return the innermost group context that contains pos, or empty context."""
    result: dict[str, Any] = {"prefix": "", "middleware": [], "domain": ""}
    for ctx in contexts:
        if ctx.get("start", 0) <= pos:
            # Merge: later contexts override earlier
            if ctx.get("prefix"):
                result["prefix"] = ctx["prefix"]
            if ctx.get("middleware"):
                result["middleware"] = ctx["middleware"]
            if ctx.get("domain"):
                result["domain"] = ctx["domain"]
    return result


def _parse_routes_from_file(
    file_path: Path,
    is_api: bool,
    class_map: dict[str, Path],
    composer_namespace: str,
) -> list[dict[str, Any]]:
    """Parse a single route file and return a list of route dicts."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Cannot read route file", path=str(file_path), error=str(exc))
        return []

    use_map = _parse_use_statements(source)
    group_contexts = _parse_route_group_context(source)
    routes: list[dict[str, Any]] = []

    def resolve(short: str) -> str:
        return _resolve_controller_fqn(short, use_map, class_map, composer_namespace)

    # --- Simple routes: Route::get/post/etc ---
    for m in _ROUTE_SIMPLE_PATTERN.finditer(source):
        http_method = m.group(1).upper()
        uri = m.group(2)
        handler_raw = m.group(3).strip()
        pos = m.start()

        group_ctx = _get_group_context_for_pos(group_contexts, pos)
        prefix = group_ctx.get("prefix", "")
        domain = group_ctx.get("domain", "")
        group_mw: list[str] = list(group_ctx.get("middleware", []))

        full_uri = ("/" + prefix.strip("/") + "/" + uri.lstrip("/")).replace("//", "/")
        if not prefix:
            full_uri = uri

        # Resolve handler
        controller_fqn = ""
        action_method = ""

        array_m = _CONTROLLER_ARRAY_PATTERN.search(handler_raw)
        at_m = _CONTROLLER_AT_PATTERN.search(handler_raw)
        invokable_m = _CONTROLLER_INVOKABLE_PATTERN.search(handler_raw)

        if array_m:
            controller_fqn = resolve(array_m.group(1))
            action_method = array_m.group(2)
        elif at_m:
            controller_fqn = resolve(at_m.group(1))
            action_method = at_m.group(2)
        elif invokable_m:
            controller_fqn = resolve(invokable_m.group(1))
            action_method = "__invoke"

        # Extract name and middleware from chain after the route definition
        # Look at up to 3 lines after the match for chained calls
        after_chunk = source[pos: pos + 400]
        name_m = _ROUTE_NAME_PATTERN.search(after_chunk)
        route_name = name_m.group(1) if name_m else ""

        inline_mw_list: list[str] = []
        for mw_m in _MIDDLEWARE_PATTERN.finditer(after_chunk):
            inline_mw_list.extend(_extract_middleware_list(mw_m.group(1)))

        middleware_stack = group_mw + inline_mw_list

        node_id_val = (
            make_node_id("route", route_name)
            if route_name
            else make_node_id("route", f"{http_method}:{full_uri}")
        )

        routes.append({
            "node_id": node_id_val,
            "name": route_name,
            "http_method": http_method,
            "uri": full_uri,
            "controller_fqn": controller_fqn,
            "action_method": action_method,
            "middleware_stack": json.dumps(middleware_stack),
            "route_file": str(file_path),
            "prefix": prefix,
            "domain": domain,
            "is_api": is_api,
        })

    # --- Resource / apiResource routes ---
    for m in _ROUTE_RESOURCE_PATTERN.finditer(source):
        resource_type = m.group(1).lower()  # "resource" or "apiresource"
        uri_prefix = m.group(2)
        controller_raw = m.group(3).strip()
        pos = m.start()

        group_ctx = _get_group_context_for_pos(group_contexts, pos)
        prefix = group_ctx.get("prefix", "")
        domain = group_ctx.get("domain", "")
        group_mw: list[str] = list(group_ctx.get("middleware", []))

        controller_fqn = resolve(controller_raw)
        full_prefix = ("/" + prefix.strip("/") + "/" + uri_prefix.lstrip("/")).replace("//", "/")
        if not prefix:
            full_prefix = uri_prefix

        after_chunk = source[pos: pos + 400]
        inline_mw_list = []
        for mw_m in _MIDDLEWARE_PATTERN.finditer(after_chunk):
            inline_mw_list.extend(_extract_middleware_list(mw_m.group(1)))
        middleware_stack = group_mw + inline_mw_list

        actions = _API_RESOURCE_ACTIONS if resource_type == "apiresource" else _RESOURCE_ACTIONS

        # Check for ->only() or ->except() modifiers
        only_m = re.search(r"->only\s*\(\s*\[([^\]]*)\]\s*\)", after_chunk)
        except_m = re.search(r"->except\s*\(\s*\[([^\]]*)\]\s*\)", after_chunk)
        if only_m:
            only_actions = [a.strip().strip("'\"") for a in only_m.group(1).split(",")]
            actions = [a for a in actions if a in only_actions]
        elif except_m:
            except_actions = [a.strip().strip("'\"") for a in except_m.group(1).split(",")]
            actions = [a for a in actions if a not in except_actions]

        resource_name = full_prefix.strip("/").replace("/", ".")

        _METHOD_MAP = {
            "index": ("GET", full_prefix),
            "create": ("GET", full_prefix + "/create"),
            "store": ("POST", full_prefix),
            "show": ("GET", full_prefix + "/{id}"),
            "edit": ("GET", full_prefix + "/{id}/edit"),
            "update": ("PUT", full_prefix + "/{id}"),
            "destroy": ("DELETE", full_prefix + "/{id}"),
        }

        for action in actions:
            http_method, action_uri = _METHOD_MAP.get(action, ("GET", full_prefix))
            route_name = f"{resource_name}.{action}"
            node_id_val = make_node_id("route", route_name)

            routes.append({
                "node_id": node_id_val,
                "name": route_name,
                "http_method": http_method,
                "uri": action_uri,
                "controller_fqn": controller_fqn,
                "action_method": action,
                "middleware_stack": json.dumps(middleware_stack),
                "route_file": str(file_path),
                "prefix": prefix,
                "domain": domain,
                "is_api": is_api or resource_type == "apiresource",
            })

    return routes


def run(ctx: PipelineContext) -> None:
    """Parse Laravel route files and build Route nodes with ROUTES_TO edges."""
    db = ctx.db
    class_map = ctx.class_map
    composer_namespace = ""
    if ctx.composer and hasattr(ctx.composer, "autoload_psr4"):
        # Find the main app namespace
        for ns, path in ctx.composer.autoload_psr4.items():
            if path in ("app/", "app"):
                composer_namespace = ns
                break

    routes_parsed = 0
    all_routes: list[dict[str, Any]] = []

    # Determine which route files to parse
    route_files = list(ctx.route_files)
    if not route_files:
        # Fallback: scan routes/ directory
        routes_dir = ctx.project_root / "routes"
        if routes_dir.exists():
            route_files = list(routes_dir.glob("*.php"))

    for route_file in route_files:
        is_api = route_file.name in ("api.php",) or "api" in route_file.stem.lower()
        try:
            parsed = _parse_routes_from_file(route_file, is_api, class_map, composer_namespace)
            all_routes.extend(parsed)
            logger.debug(
                "Parsed route file",
                file=route_file.name,
                routes_found=len(parsed),
            )
        except Exception as exc:
            logger.warning("Failed to parse route file", path=str(route_file), error=str(exc))

    # Insert Route nodes and ROUTES_TO relationships
    for route in all_routes:
        try:
            db._insert_node("Route", {
                "node_id": route["node_id"],
                "name": route["name"],
                "http_method": route["http_method"],
                "uri": route["uri"],
                "controller_fqn": route["controller_fqn"],
                "action_method": route["action_method"],
                "middleware_stack": route["middleware_stack"],
                "route_file": route["route_file"],
                "prefix": route["prefix"],
                "domain": route["domain"],
                "wheres": "{}",
                "rate_limit": "",
                "is_api": route["is_api"],
            })
        except Exception as exc:
            logger.debug("Route node insert failed", route=route["node_id"], error=str(exc))
            continue

        # Create ROUTES_TO → Method node if controller/action are known
        controller_fqn = route["controller_fqn"]
        action_method = route["action_method"]

        if controller_fqn and action_method:
            method_fqn = f"{controller_fqn}::{action_method}"
            method_nid = make_node_id("method", controller_fqn, action_method)

            # Try Method node first
            try:
                db.upsert_rel(
                    "ROUTES_TO",
                    "Route",
                    route["node_id"],
                    "Method",
                    method_nid,
                    {
                        "http_method": route["http_method"],
                        "uri": route["uri"],
                    },
                )
            except Exception:
                # Try Class_ as fallback (invokable controllers)
                controller_nid = make_node_id("class", controller_fqn)
                try:
                    db.upsert_rel(
                        "ROUTES_TO",
                        "Route",
                        route["node_id"],
                        "Class_",
                        controller_nid,
                        {
                            "http_method": route["http_method"],
                            "uri": route["uri"],
                        },
                    )
                except Exception as exc2:
                    logger.debug(
                        "ROUTES_TO rel failed",
                        route=route["node_id"],
                        controller=controller_fqn,
                        error=str(exc2),
                    )

        routes_parsed += 1

    # Store in ctx for subsequent phases (middleware resolution, flow detection)
    ctx.route_nodes = all_routes
    ctx.stats["routes_parsed"] = routes_parsed

    logger.info(
        "Route analysis complete",
        routes_parsed=routes_parsed,
        route_files=len(route_files),
    )
