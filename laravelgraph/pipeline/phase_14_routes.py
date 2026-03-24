"""Phase 14 — Route Analysis.

Parse Laravel route files (routes/web.php, routes/api.php, etc.) and build
Route nodes with ROUTES_TO relationships to controller methods.

Closure route delegation is resolved by scanning the closure body for all
known dispatch patterns:
  1. (new Controller())->method(...)
  2. new Controller()->method(...)
  3. app(Controller::class)->method(...)  /  resolve(...)
  4. $app->make(Controller::class)->method(...)
  5. DI-injected param: function(..., Ctrl $c) { $c->method(...) }
  6. Static dispatch: Controller::method(...)
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
    r"([^)]+)\)",  # handler (for closures: captures up to first ')' in params)
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

# ─── Closure-body controller detection ───────────────────────────────────────

# Detects that the handler is a closure/arrow-function
_CLOS_HANDLER = re.compile(r'^\s*(?:function|fn)\s*\(', re.IGNORECASE)

# Detects a plain method-name string: 'createBooking'  or  "createBooking"
# Used by Laravel 9+ group controller syntax inside Route::group(['controller'=>Ctrl::class])
_METHOD_STRING_PATTERN = re.compile(r"""^['"](\w+)['"]$""")

# Pattern 1a: (new Controller()) -> method(   or   (new Controller) -> method(
_CLOS_NEW_PAREN = re.compile(
    r'\(\s*new\s+([A-Za-z_][\w\\]*)\s*(?:\([^)]*\))?\s*\)\s*->\s*(\w+)\s*\('
)
# Pattern 1b: new Controller() -> method(   (no surrounding parens)
_CLOS_NEW_CALL = re.compile(
    r'\bnew\s+([A-Za-z_][\w\\]*)\s*(?:\([^)]*\))?\s*->\s*(\w+)\s*\('
)
# Pattern 2: app(Ctrl::class)->method(   or   resolve(Ctrl::class)->method(
_CLOS_APP = re.compile(
    r'\b(?:app|resolve)\s*\(\s*'
    r'(?:([A-Za-z_][\w\\]*)::class|[\'"]([A-Za-z_\\][\w\\]*)[\'"])'
    r'[^)]*\)\s*->\s*(\w+)\s*\('
)
# Pattern 3: app()->make(Ctrl::class)->method(   or   $app->make(Ctrl::class)->method(
_CLOS_MAKE = re.compile(
    r'(?:app\s*\(\s*\)|\$\w+)\s*->\s*make\s*\(\s*'
    r'(?:([A-Za-z_][\w\\]*)::class|[\'"]([A-Za-z_\\][\w\\]*)[\'"])'
    r'\s*\)\s*->\s*(\w+)\s*\('
)
# Pattern 4: Static dispatch — Controller::method(
_CLOS_STATIC = re.compile(r'\b([A-Za-z_][\w\\]*)\s*::\s*(\w+)\s*\(')
# Pattern 5a: Type-hinted closure param — function(…, ClassName $var, …)
_CLOS_PARAM = re.compile(r'\b([A-Za-z_][\w\\]*)\s+\$(\w+)')
# Pattern 5b: Variable method call — $var->method(
_CLOS_VAR_CALL = re.compile(r'\$(\w+)\s*->\s*(\w+)\s*\(')

# Class names to skip when identifying controller delegation
# (Laravel facades, helpers, base classes, framework internals)
_SKIP_CLASSES = frozenset({
    # HTTP / Response
    'Request', 'Response', 'JsonResponse', 'StreamedResponse',
    'Redirect', 'RedirectResponse',
    # View / template
    'View',
    # Facades
    'Route', 'Auth', 'Session', 'Cache', 'DB', 'Log', 'Event',
    'Mail', 'Queue', 'Storage', 'Validator', 'Hash', 'Str', 'Arr',
    'Config', 'Facade', 'Gate', 'Bus', 'Crypt', 'Cookie', 'File',
    'Lang', 'URL', 'Password', 'RateLimiter', 'Artisan',
    'Broadcast', 'Notification', 'Pipeline',
    # Base classes
    'Model', 'Controller', 'FormRequest', 'Middleware',
    'ServiceProvider', 'Job', 'Listener', 'Observer',
    # Collection / utilities
    'Collection', 'Carbon', 'Closure', 'Exception',
    # Framework namespaces (partial prefix match)
    'Illuminate', 'Laravel',
    # Container (used in make() calls — the receiver, not the target)
    'App', 'Container',
    # Misc
    'Observable', 'Scope', 'Builder', 'Relation',
    'HasMany', 'BelongsTo', 'BelongsToMany', 'HasOne',
    'Http', 'Console',
})


def _is_delegated_class(name: str) -> bool:
    """Return True if this class name looks like a controller/service, not a facade/helper."""
    base = name.split('\\')[-1]
    return (
        bool(base)
        and base[0].isupper()
        and base not in _SKIP_CLASSES
        and not base.startswith('Illuminate')
        and not base.startswith('Laravel')
    )


def _extract_braced_body(source: str, brace_start: int, max_size: int = 10_000) -> str:
    """Return the content between balanced braces, starting at the opening `{`.

    Correctly skips over string literals and comments to avoid false brace counts.
    Returns best-effort content if max_size is exceeded.
    """
    depth = 0
    i = brace_start
    limit = min(len(source), brace_start + max_size)

    while i < limit:
        ch = source[i]

        # Skip string literals (single or double quoted)
        if ch in ('"', "'"):
            q = ch
            i += 1
            while i < limit:
                c = source[i]
                if c == '\\':
                    i += 2
                    continue
                if c == q:
                    break
                i += 1

        # Skip single-line comments  //  and  #
        elif ch == '#' or (ch == '/' and i + 1 < limit and source[i + 1] == '/'):
            while i < limit and source[i] != '\n':
                i += 1

        # Skip block comments  /* … */
        elif ch == '/' and i + 1 < limit and source[i + 1] == '*':
            end = source.find('*/', i + 2)
            i = (end + 2) if end != -1 else limit
            continue

        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return source[brace_start + 1: i]

        i += 1

    return source[brace_start + 1: min(i, limit)]  # best-effort if truncated


def _extract_arrow_expr(source: str, expr_start: int, max_size: int = 600) -> str:
    """Return a PHP arrow-function expression (everything up to `;` or unmatched `)`)."""
    depth = 0
    i = expr_start
    limit = min(len(source), expr_start + max_size)

    while i < limit:
        ch = source[i]
        if ch in ('(', '[', '{'):
            depth += 1
        elif ch in (')', ']', '}'):
            if depth == 0:
                return source[expr_start: i]
            depth -= 1
        elif ch == ';' and depth == 0:
            return source[expr_start: i]
        i += 1

    return source[expr_start: limit]


def _find_closure_body(source: str, after_match_end: int) -> str:
    """Locate and return the body of a PHP closure or arrow-function.

    ``after_match_end`` should be the position in ``source`` right after the
    regex consumed the route definition (i.e. past the closing ``)`` of the
    function parameter list).

    Handles:
    - Regular function: ``function(…) { body }``
    - Arrow function:   ``fn(…) => expr``
    """
    window = source[after_match_end: after_match_end + 400]

    arrow_m = re.search(r'\s*=>\s*', window)
    brace_m = re.search(r'\{', window)

    # Prefer arrow if it appears before any brace
    if arrow_m and (not brace_m or arrow_m.start() < brace_m.start()):
        return _extract_arrow_expr(source, after_match_end + arrow_m.end())

    if brace_m:
        return _extract_braced_body(source, after_match_end + brace_m.start())

    return ""


def _find_closure_controller(
    body: str,
    params_raw: str,
    use_map: dict[str, str],
    class_map: dict[str, Path],
    composer_namespace: str,
) -> tuple[str, str, float]:
    """Scan a closure/arrow-function body for controller-delegation patterns.

    Patterns tried in order (highest confidence first):
      1. (new Controller())->method(…)        conf 0.85
      2. new Controller()->method(…)          conf 0.85
      3. app(Ctrl::class)->method(…)          conf 0.90
      4. $app->make(Ctrl::class)->method(…)   conf 0.85
      5. DI-injected param $ctrl->method(…)   conf 0.90
      6. Static dispatch Ctrl::method(…)      conf 0.70  (class must be in class_map)

    Returns ``(controller_fqn, action_method, confidence)`` — all empty + 0.0
    if no delegation is found.
    """
    if not body:
        return "", "", 0.0

    def resolve(short: str) -> str:
        return _resolve_controller_fqn(short, use_map, class_map, composer_namespace)

    # 1 – (new Ctrl(…)) -> method(
    for m in _CLOS_NEW_PAREN.finditer(body):
        cls, method = m.group(1), m.group(2)
        if _is_delegated_class(cls):
            return resolve(cls), method, 0.85

    # 2 – new Ctrl(…) -> method(  (no outer parens)
    for m in _CLOS_NEW_CALL.finditer(body):
        cls, method = m.group(1), m.group(2)
        if _is_delegated_class(cls):
            return resolve(cls), method, 0.85

    # 3 – app(Ctrl::class)->method(  /  resolve(Ctrl::class)->method(
    for m in _CLOS_APP.finditer(body):
        cls = m.group(1) or m.group(2) or ""
        method = m.group(3)
        if cls and _is_delegated_class(cls):
            return resolve(cls), method, 0.90

    # 4 – $app->make(Ctrl::class)->method(  /  app()->make(…)->method(
    for m in _CLOS_MAKE.finditer(body):
        cls = m.group(1) or m.group(2) or ""
        method = m.group(3)
        if cls and _is_delegated_class(cls):
            return resolve(cls), method, 0.85

    # 5 – DI-injected parameter: function(…, BookingCtrl $ctrl) { $ctrl->create(…) }
    param_types: dict[str, str] = {}
    for pm in _CLOS_PARAM.finditer(params_raw):
        cls, var = pm.group(1), pm.group(2)
        if _is_delegated_class(cls):
            param_types[var] = cls

    if param_types:
        for vm in _CLOS_VAR_CALL.finditer(body):
            var_name, method = vm.group(1), vm.group(2)
            if var_name in param_types and method not in ('__construct',):
                return resolve(param_types[var_name]), method, 0.90

    # 6 – Static dispatch Ctrl::method(  — only when class is confirmed in class_map
    for m in _CLOS_STATIC.finditer(body):
        cls, method = m.group(1), m.group(2)
        if _is_delegated_class(cls) and method not in ('class', 'make', 'getInstance'):
            fqn = resolve(cls)
            if fqn in class_map:
                return fqn, method, 0.70

    return "", "", 0.0


# ─── Existing helpers ─────────────────────────────────────────────────────────

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
    raw = raw.strip()
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
    if "\\" in short_name and short_name.startswith("\\"):
        return short_name.lstrip("\\")

    short = short_name.split("\\")[-1]
    if short in use_map:
        return use_map[short]

    if short_name in class_map:
        return short_name

    for ns in [
        f"App\\Http\\Controllers\\{short_name}",
        f"{composer_namespace}Http\\Controllers\\{short_name}",
    ]:
        if ns in class_map:
            return ns

    return short_name


def _parse_route_group_context(source: str) -> list[dict[str, Any]]:
    """Return context dicts for route groups (prefix, middleware, domain, controller).

    Handles three group forms:
      - Route::group(['controller' => Ctrl::class, 'prefix' => '...'], function() {...})
      - Route::controller(Ctrl::class)->prefix('...')->group(function() {...})
      - Route::prefix('...')->controller(Ctrl::class)->group(function() {...})

    Nested groups are flattened. Each context applies to everything after its
    opening position in the file.
    """
    contexts = []

    # Match all three group forms
    group_pattern = re.compile(
        r"Route\s*::\s*(?:"
        # Form 1: Route::group(['key' => val, ...], function
        r"group\s*\(\s*\[([^\]]*)\]\s*,|"
        # Form 2/3: Route::controller/prefix/middleware/domain chains ending in ->group(
        r"((?:(?:controller|prefix|middleware|domain)\s*\([^)]*\)"
        r"(?:\s*->\s*(?:controller|prefix|middleware|domain)\s*\([^)]*\))*)"
        r"\s*->group\s*\("
        r"))"
        r"\s*function\s*\(\s*\)\s*\{",
        re.DOTALL,
    )

    # Array-style key patterns
    prefix_pat = re.compile(r"['\"]prefix['\"]\s*=>\s*['\"]([^'\"]+)['\"]")
    mw_pat = re.compile(r"['\"]middleware['\"]\s*=>\s*(\[[^\]]*\]|['\"][^'\"]*['\"])")
    domain_pat = re.compile(r"['\"]domain['\"]\s*=>\s*['\"]([^'\"]+)['\"]")
    # 'controller' => ClassName::class  or  'controller' => 'ClassName'
    ctrl_arr_pat = re.compile(
        r"['\"]controller['\"]\s*=>\s*(?:([A-Za-z_\\]+)::class|['\"]([A-Za-z_\\]+)['\"])"
    )

    # Fluent-chain patterns
    inline_prefix_pat = re.compile(r"->prefix\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
    inline_mw_pat = re.compile(r"->middleware\s*\(\s*([^)]+)\)")
    # ->controller(ClassName::class)  or  controller(ClassName::class)  (-> is optional
    # because Route::controller(...) strips the "Route::" leaving just "controller(...)")
    inline_ctrl_pat = re.compile(
        r"(?:->)?controller\s*\(\s*(?:([A-Za-z_\\]+)::class|['\"]([A-Za-z_\\]+)['\"])\s*\)"
    )

    for m in group_pattern.finditer(source):
        ctx_raw = m.group(1) or m.group(2) or ""
        ctx: dict[str, Any] = {"prefix": "", "middleware": [], "domain": "", "controller": ""}

        # ── prefix ────────────────────────────────────────────────────────────
        prefix_m = prefix_pat.search(ctx_raw)
        if prefix_m:
            ctx["prefix"] = prefix_m.group(1)
        else:
            inline_prefix_m = inline_prefix_pat.search(ctx_raw)
            if inline_prefix_m:
                ctx["prefix"] = inline_prefix_m.group(1)

        # ── middleware ────────────────────────────────────────────────────────
        mw_m = mw_pat.search(ctx_raw)
        if mw_m:
            ctx["middleware"] = _extract_middleware_list(mw_m.group(1))
        else:
            for inline_mw_m in inline_mw_pat.finditer(ctx_raw):
                ctx["middleware"].extend(_extract_middleware_list(inline_mw_m.group(1)))

        # ── domain ────────────────────────────────────────────────────────────
        domain_m = domain_pat.search(ctx_raw)
        if domain_m:
            ctx["domain"] = domain_m.group(1)

        # ── controller (Laravel 9+ group controller syntax) ───────────────────
        ctrl_m = ctrl_arr_pat.search(ctx_raw)
        if ctrl_m:
            ctx["controller"] = ctrl_m.group(1) or ctrl_m.group(2) or ""
        else:
            inline_ctrl_m = inline_ctrl_pat.search(ctx_raw)
            if inline_ctrl_m:
                ctx["controller"] = inline_ctrl_m.group(1) or inline_ctrl_m.group(2) or ""

        ctx["start"] = m.start()
        contexts.append(ctx)

    return contexts


def _get_group_context_for_pos(contexts: list[dict[str, Any]], pos: int) -> dict[str, Any]:
    """Return the merged group context that covers pos, or an empty context."""
    result: dict[str, Any] = {"prefix": "", "middleware": [], "domain": "", "controller": ""}
    for ctx in contexts:
        if ctx.get("start", 0) <= pos:
            if ctx.get("prefix"):
                result["prefix"] = ctx["prefix"]
            if ctx.get("middleware"):
                result["middleware"] = ctx["middleware"]
            if ctx.get("domain"):
                result["domain"] = ctx["domain"]
            if ctx.get("controller"):
                result["controller"] = ctx["controller"]
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

        # ── Resolve handler ───────────────────────────────────────────────────
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

        # ── Laravel 9+ group controller: Route::group(['controller'=>Ctrl::class]) ──
        # Handler is a plain method-name string, e.g. 'createBooking'
        if not controller_fqn:
            method_str_m = _METHOD_STRING_PATTERN.match(handler_raw.strip())
            group_ctrl = group_ctx.get("controller", "")
            if method_str_m and group_ctrl:
                controller_fqn = resolve(group_ctrl)
                action_method = method_str_m.group(1)
                logger.debug(
                    "Resolved group-controller route",
                    route=f"{http_method} {full_uri}",
                    controller=controller_fqn,
                    method=action_method,
                )

        # ── Closure delegation: scan body for controller dispatch ─────────────
        if not controller_fqn and _CLOS_HANDLER.match(handler_raw.lstrip()):
            # handler_raw has the params (truncated at first ')' by the regex).
            # Extract the full closure body from source, starting after the match.
            body = _find_closure_body(source, m.end())
            params_raw = handler_raw  # contains the type-hinted params

            ctrl_fqn, ctrl_method, conf = _find_closure_controller(
                body, params_raw, use_map, class_map, composer_namespace
            )
            if ctrl_fqn and conf >= 0.70:
                controller_fqn = ctrl_fqn
                action_method = ctrl_method
                logger.debug(
                    "Resolved closure controller",
                    route=f"{http_method} {full_uri}",
                    controller=controller_fqn,
                    method=action_method,
                    confidence=conf,
                )

        # ── Chain metadata (name, inline middleware) ──────────────────────────
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
            "index":   ("GET",    full_prefix),
            "create":  ("GET",    full_prefix + "/create"),
            "store":   ("POST",   full_prefix),
            "show":    ("GET",    full_prefix + "/{id}"),
            "edit":    ("GET",    full_prefix + "/{id}/edit"),
            "update":  ("PUT",    full_prefix + "/{id}"),
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
        for ns, path in ctx.composer.autoload_psr4.items():
            if path in ("app/", "app"):
                composer_namespace = ns
                break

    routes_parsed = 0
    all_routes: list[dict[str, Any]] = []

    route_files = list(ctx.route_files)
    if not route_files:
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
            db.upsert_node("Route", {
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
            logger.debug("Route node upsert failed", route=route["node_id"], error=str(exc))
            continue

        controller_fqn = route["controller_fqn"]
        action_method = route["action_method"]

        if controller_fqn and action_method:
            method_nid = make_node_id("method", controller_fqn, action_method)

            try:
                db.upsert_rel(
                    "ROUTES_TO",
                    "Route", route["node_id"],
                    "Method", method_nid,
                    {
                        "http_method": route["http_method"],
                        "uri": route["uri"],
                    },
                )
            except Exception:
                controller_nid = make_node_id("class", controller_fqn)
                try:
                    db.upsert_rel(
                        "ROUTES_TO",
                        "Route", route["node_id"],
                        "Class_", controller_nid,
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

    ctx.route_nodes = all_routes
    ctx.stats["routes_parsed"] = routes_parsed

    logger.info(
        "Route analysis complete",
        routes_parsed=routes_parsed,
        route_files=len(route_files),
    )
