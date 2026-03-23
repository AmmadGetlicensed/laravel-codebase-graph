"""Phase 21 — Dependency Injection Tracing.

Trace constructor and method injection through class constructors and
controller action methods, creating INJECTS relationships.
"""

from __future__ import annotations

import json
import re
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# PHP scalar/built-in types that are NOT injectable classes
_SCALAR_TYPES = frozenset({
    "string", "int", "integer", "float", "double", "bool", "boolean",
    "array", "callable", "iterable", "object", "void", "null", "mixed",
    "never", "self", "static", "parent", "false", "true",
    # Common Laravel pseudo-types that aren't resolvable classes
    "Request", "Response",
})

# Match method signature with parameters
_METHOD_RE = re.compile(
    r"(?P<visibility>public|protected|private)?\s*"
    r"(?P<static>static\s+)?"
    r"function\s+(?P<name>\w+)\s*"
    r"\((?P<params>[^)]*)\)",
    re.DOTALL,
)

# Match a single typed parameter: TypeHint $varName
_PARAM_RE = re.compile(
    r"(?:^|,)\s*"
    r"(?:\?)?(?P<type>[\w\\]+(?:\|[\w\\]+)*)\s+"
    r"\$(?P<name>\w+)",
)

# Use statement to extract imported class names
_USE_RE = re.compile(r"^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?;", re.MULTILINE)


def _build_use_map(source: str) -> dict[str, str]:
    """Return {short_name: fqn} from use statements in the file."""
    result: dict[str, str] = {}
    for m in _USE_RE.finditer(source):
        fqn = m.group(1)
        alias = m.group(2)
        short = alias if alias else fqn.split("\\")[-1]
        result[short] = fqn
    return result


def _resolve_type(type_hint: str, use_map: dict[str, str], namespace: str) -> str:
    """Resolve a type hint to a fully-qualified name."""
    if "\\" in type_hint:
        # Already qualified
        return type_hint.lstrip("\\")
    if type_hint in use_map:
        return use_map[type_hint]
    # Assume same namespace
    if namespace:
        return f"{namespace}\\{type_hint}"
    return type_hint


def _extract_namespace(source: str) -> str:
    """Extract the PHP namespace declaration from a source file."""
    m = re.search(r"^\s*namespace\s+([\w\\]+)\s*;", source, re.MULTILINE)
    return m.group(1) if m else ""


def _parse_constructor_injections(
    source: str,
    use_map: dict[str, str],
    namespace: str,
) -> list[dict[str, str]]:
    """Return list of {type_hint, fqn, param_name} for constructor parameters."""
    injections: list[dict[str, str]] = []

    for method_match in _METHOD_RE.finditer(source):
        if method_match.group("name") != "__construct":
            continue

        params_str = method_match.group("params")
        for p in _PARAM_RE.finditer(params_str):
            type_str = p.group("type")
            param_name = p.group("name")

            # Skip union types (pick the first non-null)
            for part in type_str.split("|"):
                part = part.strip().lstrip("?")
                if part.lower() in _SCALAR_TYPES:
                    continue
                fqn = _resolve_type(part, use_map, namespace)
                injections.append({
                    "type_hint": part,
                    "fqn": fqn,
                    "param_name": param_name,
                })
                break  # Only use first resolvable type in union

    return injections


def _parse_method_injections(
    source: str,
    use_map: dict[str, str],
    namespace: str,
    is_controller: bool,
) -> list[dict[str, str]]:
    """Return list of {method_name, type_hint, fqn, param_name} for method-injected parameters."""
    injections: list[dict[str, str]] = []

    if not is_controller:
        return injections

    for method_match in _METHOD_RE.finditer(source):
        method_name = method_match.group("name")
        visibility = method_match.group("visibility") or "public"
        is_static = bool(method_match.group("static"))

        # Controller actions: public non-static methods (excluding __construct)
        if visibility != "public" or is_static or method_name == "__construct":
            continue

        params_str = method_match.group("params")
        for p in _PARAM_RE.finditer(params_str):
            type_str = p.group("type")
            param_name = p.group("name")

            for part in type_str.split("|"):
                part = part.strip().lstrip("?")
                if part.lower() in _SCALAR_TYPES:
                    continue
                fqn = _resolve_type(part, use_map, namespace)
                injections.append({
                    "method_name": method_name,
                    "type_hint": part,
                    "fqn": fqn,
                    "param_name": param_name,
                })
                break

    return injections


def run(ctx: PipelineContext) -> None:
    """Trace dependency injection and create INJECTS relationships."""
    db = ctx.db
    injection_edges = 0

    for php_path in ctx.php_files:
        try:
            source = php_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if "__construct" not in source and not ("Controller" in str(php_path)):
            continue

        namespace = _extract_namespace(source)
        use_map = _build_use_map(source)

        # Determine the class name from parsed_php or filename
        class_name = php_path.stem
        class_fqn = f"{namespace}\\{class_name}" if namespace else class_name
        class_nid = ctx.fqn_index.get(class_fqn, make_node_id("class", class_fqn))

        try:
            rel_str = php_path.relative_to(ctx.project_root).as_posix()
        except ValueError:
            rel_str = str(php_path)

        is_controller = (
            "Controller" in str(php_path)
            or class_name.endswith("Controller")
        )

        # Constructor injections
        constructor_injections = _parse_constructor_injections(source, use_map, namespace)
        for inj in constructor_injections:
            injected_fqn = inj["fqn"]
            injected_nid = ctx.fqn_index.get(injected_fqn, make_node_id("class", injected_fqn))

            try:
                db.upsert_rel(
                    "INJECTS",
                    "Class_",
                    class_nid,
                    "Class_",
                    injected_nid,
                    {
                        "injection_method": "constructor",
                        "parameter": inj["param_name"],
                        "type_hint": inj["type_hint"],
                    },
                )
                injection_edges += 1
            except Exception as exc:
                logger.debug(
                    "INJECTS (constructor) rel failed",
                    class_fqn=class_fqn,
                    injected=injected_fqn,
                    error=str(exc),
                )

        # Method injections for controllers
        if is_controller:
            method_injections = _parse_method_injections(source, use_map, namespace, is_controller=True)
            for inj in method_injections:
                method_name = inj["method_name"]
                method_fqn = f"{class_fqn}::{method_name}"
                method_nid = ctx.fqn_index.get(method_fqn, make_node_id("method", class_fqn, method_name))

                injected_fqn = inj["fqn"]
                injected_nid = ctx.fqn_index.get(injected_fqn, make_node_id("class", injected_fqn))

                try:
                    db.upsert_rel(
                        "INJECTS",
                        "Method",
                        method_nid,
                        "Class_",
                        injected_nid,
                        {
                            "injection_method": "method",
                            "parameter": inj["param_name"],
                            "type_hint": inj["type_hint"],
                        },
                    )
                    injection_edges += 1
                except Exception as exc:
                    logger.debug(
                        "INJECTS (method) rel failed",
                        method_fqn=method_fqn,
                        injected=injected_fqn,
                        error=str(exc),
                    )

    ctx.stats["injection_edges"] = injection_edges
    logger.info("Dependency injection tracing complete", injection_edges=injection_edges)
