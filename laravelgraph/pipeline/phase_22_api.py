"""Phase 22 — API Contract Analysis.

Extract validation rules from FormRequests, response shapes from API Resources,
and authorization usage from controller/policy calls.
"""

from __future__ import annotations

import json
import re
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Match the rules() method body
_RULES_METHOD_RE = re.compile(
    r"function\s+rules\s*\(\s*\)[^{]*\{(.*?)\}",
    re.DOTALL,
)
# Match individual rule keys: 'key' => or "key" =>
_RULE_KEY_RE = re.compile(r"""['\"]([^'"]+)['"]\s*=>""")

# Match toArray() method body
_TO_ARRAY_RE = re.compile(
    r"function\s+toArray\s*\([^)]*\)[^{]*\{(.*?)\}",
    re.DOTALL,
)
# Match array keys in toArray: 'key' => or "key" =>
_ARRAY_KEY_RE = re.compile(r"""['\"]([^'"]+)['"]\s*=>""")

# Authorize calls: $this->authorize('ability', ...) or $this->authorize('ability')
_AUTHORIZE_RE = re.compile(
    r"""\$this\s*->\s*authorize\s*\(\s*['"]([^'"]+)['"]""",
)
# Gate calls: Gate::allows('ability') or Gate::can('ability')
_GATE_RE = re.compile(
    r"""Gate::(?:allows|can|denies|check)\s*\(\s*['"]([^'"]+)['"]""",
)
# Gate::authorize: Gate::authorize('ability', ...)
_GATE_AUTHORIZE_RE = re.compile(
    r"""Gate::authorize\s*\(\s*['"]([^'"]+)['"]""",
)

# Detect controller method that uses a FormRequest parameter
_METHOD_RE = re.compile(
    r"function\s+(\w+)\s*\(([^)]*)\)",
    re.DOTALL,
)
_TYPED_PARAM_RE = re.compile(
    r"([\w\\]+)\s+\$\w+",
)

# Line number helper
def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _read_text(path: Any) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _extract_namespace(source: str) -> str:
    m = re.search(r"^\s*namespace\s+([\w\\]+)\s*;", source, re.MULTILINE)
    return m.group(1) if m else ""


def _build_use_map(source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for m in re.finditer(r"^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?;", source, re.MULTILINE):
        fqn = m.group(1)
        alias = m.group(2)
        short = alias if alias else fqn.split("\\")[-1]
        result[short] = fqn
    return result


def _resolve_type(type_str: str, use_map: dict[str, str], namespace: str) -> str:
    if "\\" in type_str:
        return type_str.lstrip("\\")
    if type_str in use_map:
        return use_map[type_str]
    if namespace:
        return f"{namespace}\\{type_str}"
    return type_str


def run(ctx: PipelineContext) -> None:
    """Analyze API contracts: FormRequests, Resources, and authorization."""
    db = ctx.db
    api_contracts_analyzed = 0

    for php_path in ctx.php_files:
        source = _read_text(php_path)
        if not source:
            continue

        try:
            rel_str = php_path.relative_to(ctx.project_root).as_posix()
        except ValueError:
            rel_str = str(php_path)

        namespace = _extract_namespace(source)
        use_map = _build_use_map(source)
        class_name = php_path.stem
        class_fqn = f"{namespace}\\{class_name}" if namespace else class_name

        # ── FormRequest: parse rules() method ────────────────────────────────
        is_request = (
            rel_str.startswith("app/Http/Requests/")
            or "extends FormRequest" in source
        )
        if is_request:
            rules_match = _RULES_METHOD_RE.search(source)
            if rules_match:
                rules_body = rules_match.group(1)
                rule_keys = _RULE_KEY_RE.findall(rules_body)
                rules_summary = json.dumps(rule_keys)

                # Update FormRequest node with rules_summary
                request_nid = ctx.fqn_index.get(class_fqn, make_node_id("class", class_fqn))
                try:
                    db._insert_node("FormRequest", {
                        "node_id": make_node_id("request", class_fqn),
                        "name": class_name,
                        "fqn": class_fqn,
                        "file_path": str(php_path),
                        "rules_summary": rules_summary,
                    })
                except Exception:
                    pass  # May already exist from phase 03

                api_contracts_analyzed += 1

        # ── API Resource: parse toArray() method ─────────────────────────────
        is_resource = (
            rel_str.startswith("app/Http/Resources/")
            or "extends JsonResource" in source
            or "extends ResourceCollection" in source
        )
        if is_resource:
            to_array_match = _TO_ARRAY_RE.search(source)
            if to_array_match:
                to_array_body = to_array_match.group(1)
                # Attempt to find any TRANSFORMS_WITH relationships from methods that use this resource
                # The resource itself is already created; we just note it was analyzed
                api_contracts_analyzed += 1

        # ── Authorization: $this->authorize() and Gate:: calls ───────────────
        file_nid = make_node_id("file", rel_str)

        for m in _AUTHORIZE_RE.finditer(source):
            ability = m.group(1)
            line = _line_of(source, m.start())
            # Create a relationship from the file/class to a policy ability
            # We use AUTHORIZES_WITH: File → (no target node — self-referential note)
            # Best effort: log the usage without a target policy node
            try:
                db.upsert_rel(
                    "AUTHORIZES_WITH",
                    "File",
                    file_nid,
                    "File",
                    file_nid,
                    {"ability": ability, "line": line},
                )
            except Exception:
                pass

        for m in _GATE_RE.finditer(source):
            ability = m.group(1)
            line = _line_of(source, m.start())
            try:
                db.upsert_rel(
                    "AUTHORIZES_WITH",
                    "File",
                    file_nid,
                    "File",
                    file_nid,
                    {"ability": ability, "line": line},
                )
            except Exception:
                pass

        # ── VALIDATES_WITH: Route (controller method) → FormRequest ──────────
        # For each controller method, check if it type-hints a FormRequest class
        if "Controller" in class_name or rel_str.startswith("app/Http/Controllers/"):
            for method_match in _METHOD_RE.finditer(source):
                method_name = method_match.group(1)
                if method_name == "__construct":
                    continue
                params_str = method_match.group(2)
                for param_match in _TYPED_PARAM_RE.finditer(params_str):
                    type_str = param_match.group(1)
                    if type_str.lower() in ("string", "int", "bool", "array", "float"):
                        continue
                    resolved_fqn = _resolve_type(type_str, use_map, namespace)
                    # Check if this resolved type looks like a FormRequest
                    short = resolved_fqn.split("\\")[-1]
                    if "Request" in short:
                        # Find if this controller method is linked to a route
                        method_fqn = f"{class_fqn}::{method_name}"
                        method_nid = ctx.fqn_index.get(
                            method_fqn,
                            make_node_id("method", class_fqn, method_name),
                        )
                        request_nid = ctx.fqn_index.get(
                            resolved_fqn,
                            make_node_id("class", resolved_fqn),
                        )
                        try:
                            db.upsert_rel(
                                "VALIDATES_WITH",
                                "Method",
                                method_nid,
                                "Class_",
                                request_nid,
                                {"line": 0},
                            )
                        except Exception as exc:
                            logger.debug(
                                "VALIDATES_WITH rel failed",
                                method=method_fqn,
                                request=resolved_fqn,
                                error=str(exc),
                            )

                    # TRANSFORMS_WITH: method → Resource class
                    if "Resource" in short:
                        method_fqn = f"{class_fqn}::{method_name}"
                        method_nid = ctx.fqn_index.get(
                            method_fqn,
                            make_node_id("method", class_fqn, method_name),
                        )
                        resource_nid = ctx.fqn_index.get(
                            resolved_fqn,
                            make_node_id("class", resolved_fqn),
                        )
                        try:
                            db.upsert_rel(
                                "TRANSFORMS_WITH",
                                "Method",
                                method_nid,
                                "Class_",
                                resource_nid,
                                {"line": 0},
                            )
                        except Exception as exc:
                            logger.debug(
                                "TRANSFORMS_WITH rel failed",
                                method=method_fqn,
                                resource=resolved_fqn,
                                error=str(exc),
                            )

    ctx.stats["api_contracts_analyzed"] = api_contracts_analyzed
    logger.info("API contract analysis complete", contracts=api_contracts_analyzed)
