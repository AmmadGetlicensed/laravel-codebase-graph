"""Phase 18 — Blade Template Inheritance and Inclusion Graph.

Build the template inheritance graph: BladeTemplate nodes with EXTENDS_TEMPLATE,
INCLUDES_TEMPLATE, and HAS_COMPONENT relationships. Also links controller methods
that return a view to their BladeTemplate via RENDERS_TEMPLATE.

Also detects static method calls inside Blade expressions ({{ Cls::method() }},
{!! Cls::method() !!}, <?php Cls::method() ?>) and creates CALLS edges from
the BladeTemplate to the target Method. This prevents helper/utility methods
that are only invoked from templates from being falsely flagged as dead code.
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

# Regex patterns for Blade directives
_EXTENDS_RE = re.compile(r"@extends\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")
_INCLUDE_RE = re.compile(r"@include(?:If|Unless|First|When)?\s*\(\s*['\"]([^'\"]+)['\"]\s*")
_SECTION_RE = re.compile(r"@section\s*\(\s*['\"]([^'\"]+)['\"]\s*")
_STACK_RE = re.compile(r"@(?:push|stack)\s*\(\s*['\"]([^'\"]+)['\"]\s*")
_SLOT_RE = re.compile(r"@slot\s*\(\s*['\"]([^'\"]+)['\"]\s*")
_X_COMPONENT_RE = re.compile(r"<x-([\w\-:]+)")
_LIVEWIRE_RE = re.compile(r"@livewire\s*\(\s*['\"]([^'\"]+)['\"]\s*")
_COMPONENT_DIRECTIVE_RE = re.compile(r"@component\s*\(\s*['\"]([^'\"]+)['\"]\s*")
# view() call in PHP source
_VIEW_CALL_RE = re.compile(
    r"""(?:return\s+)?view\s*\(\s*['"]([^'"]+)['"]\s*[,\)]""",
    re.DOTALL,
)

# Static method calls in Blade: ClassName::method( or \Full\Ns\Cls::method(
# Captures class (group 1) and method name (group 2).
_STATIC_CALL_RE = re.compile(
    r"\\?([A-Za-z_][\w\\]*)::([a-zA-Z_]\w*)\s*\("
)

# Laravel facade / framework class names to skip (never app code)
_BLADE_SKIP_CLASSES: frozenset[str] = frozenset({
    "Route", "Auth", "Session", "Cache", "DB", "Log", "Event",
    "Mail", "Queue", "Storage", "Validator", "Hash", "Str", "Arr",
    "Config", "Facade", "Gate", "Bus", "Crypt", "Cookie", "File",
    "Lang", "URL", "Password", "RateLimiter", "Artisan",
    "Broadcast", "Notification", "Pipeline", "Redirect", "Response",
    "Request", "View", "Blade", "Carbon", "Collection", "Closure",
    "Illuminate", "Laravel", "PHP_EOL", "PHP_INT_MAX", "PHP_INT_MIN",
    "Application", "Container", "Model", "Builder",
    "Str", "Arr", "Number", "Date",
})


def _extract_blade_static_calls(
    source: str,
    class_map: dict[str, Path],
    composer_namespace: str,
) -> list[tuple[str, str]]:
    """Return (class_fqn, method_name) pairs for static calls found in a Blade file.

    Resolves short class names to FQNs using the project class_map.
    Skips Laravel facade/framework classes.
    """
    calls: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for m in _STATIC_CALL_RE.finditer(source):
        raw_cls = m.group(1).lstrip("\\")
        method = m.group(2)

        # Skip framework classes
        short = raw_cls.split("\\")[-1]
        if short in _BLADE_SKIP_CLASSES:
            continue
        if raw_cls.startswith(("Illuminate\\", "Laravel\\")):
            continue

        # Resolve to FQN
        if "\\" in raw_cls:
            # Already a qualified name — normalise backslash
            fqn = raw_cls
        elif raw_cls in class_map:
            fqn = raw_cls
        else:
            # Try common App namespaces
            candidates = [
                f"App\\Helpers\\{raw_cls}",
                f"App\\{raw_cls}",
                f"{composer_namespace}{raw_cls}",
            ]
            # Also try matching last segment of any key in class_map
            fqn = ""
            for candidate in candidates:
                if candidate in class_map:
                    fqn = candidate
                    break
            if not fqn:
                # Fuzzy: find any class_map key whose last segment matches
                for key in class_map:
                    if key.split("\\")[-1] == raw_cls:
                        fqn = key
                        break
            if not fqn:
                continue  # Cannot resolve — skip

        pair = (fqn, method)
        if pair not in seen:
            seen.add(pair)
            calls.append(pair)

    return calls


def _view_name_from_path(path: Path, project_root: Path) -> str:
    """Convert a Blade file path to a dot-notation view name."""
    try:
        rel = path.relative_to(project_root / "resources" / "views")
    except ValueError:
        rel = path
    # Strip .blade.php extension
    name = rel.as_posix()
    for ext in (".blade.php", ".php"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    return name.replace("/", ".")


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _build_line_offsets(source: str) -> list[int]:
    """Return a list of byte offsets where each line starts (line_offsets[i] = offset of line i+1)."""
    offsets = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _offset_to_line(offset: int, line_offsets: list[int]) -> int:
    """Binary search: return 1-based line number for the given byte offset."""
    lo, hi = 0, len(line_offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if line_offsets[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _parse_blade_file(path: Path, project_root: Path) -> dict[str, Any]:
    """Parse a Blade file and extract template metadata."""
    source = _read_source(path)
    view_name = _view_name_from_path(path, project_root)

    extends_layout = ""
    m = _EXTENDS_RE.search(source)
    if m:
        extends_layout = m.group(1)

    includes = list(dict.fromkeys(_INCLUDE_RE.findall(source)))
    sections = list(dict.fromkeys(_SECTION_RE.findall(source)))
    stacks = list(dict.fromkeys(_STACK_RE.findall(source)))
    slots = list(dict.fromkeys(_SLOT_RE.findall(source)))
    x_components = list(dict.fromkeys(_X_COMPONENT_RE.findall(source)))
    livewire_components = list(dict.fromkeys(_LIVEWIRE_RE.findall(source)))
    component_directives = list(dict.fromkeys(_COMPONENT_DIRECTIVE_RE.findall(source)))

    return {
        "view_name": view_name,
        "file_path": str(path),
        "extends_layout": extends_layout,
        "includes": includes,
        "sections": sections,
        "stacks": stacks,
        "slots": slots,
        "x_components": x_components,
        "livewire_components": livewire_components,
        "component_directives": component_directives,
    }


def run(ctx: PipelineContext) -> None:
    """Build Blade template inheritance and inclusion graph."""
    db = ctx.db
    templates_parsed = 0
    component_usages = 0
    blade_calls_created = 0

    # Pre-compute composer namespace for class resolution
    composer_namespace = ""
    if ctx.composer and hasattr(ctx.composer, "autoload_psr4"):
        for ns, path in ctx.composer.autoload_psr4.items():
            if path in ("app/", "app"):
                composer_namespace = ns
                break

    # Map view_name → node_id for cross-referencing
    view_nid_map: dict[str, str] = {}

    # Parse all Blade files and create BladeTemplate nodes
    parsed_templates: list[dict[str, Any]] = []
    for blade_path in ctx.blade_files:
        info = _parse_blade_file(blade_path, ctx.project_root)
        view_name = info["view_name"]
        nid = make_node_id("blade", view_name)
        view_nid_map[view_name] = nid

        try:
            rel_path = blade_path.relative_to(ctx.project_root).as_posix()
        except ValueError:
            rel_path = str(blade_path)

        try:
            db.upsert_node("BladeTemplate", {
                "node_id": nid,
                "name": view_name,
                "file_path": str(blade_path),
                "relative_path": rel_path,
                "extends_layout": info["extends_layout"],
                "sections": json.dumps(info["sections"]),
                "stacks": json.dumps(info["stacks"]),
                "slots": json.dumps(info["slots"]),
            })
            templates_parsed += 1
            parsed_templates.append(info)
        except Exception as exc:
            logger.debug("BladeTemplate node insert failed", view=view_name, error=str(exc))

    # Build relationships between templates
    for info in parsed_templates:
        view_name = info["view_name"]
        nid = view_nid_map.get(view_name, make_node_id("blade", view_name))

        # EXTENDS_TEMPLATE
        if info["extends_layout"]:
            layout_name = info["extends_layout"]
            layout_nid = view_nid_map.get(layout_name, make_node_id("blade", layout_name))
            # Ensure layout node exists (create stub if needed)
            if layout_name not in view_nid_map:
                try:
                    db.upsert_node("BladeTemplate", {
                        "node_id": layout_nid,
                        "name": layout_name,
                        "file_path": "",
                        "relative_path": "",
                        "extends_layout": "",
                        "sections": "[]",
                        "stacks": "[]",
                        "slots": "[]",
                    })
                    view_nid_map[layout_name] = layout_nid
                except Exception:
                    pass
            try:
                db.upsert_rel("EXTENDS_TEMPLATE", "BladeTemplate", nid, "BladeTemplate", layout_nid)
            except Exception as exc:
                logger.debug("EXTENDS_TEMPLATE rel failed", view=view_name, layout=layout_name, error=str(exc))

        # INCLUDES_TEMPLATE
        for included in info["includes"]:
            inc_nid = view_nid_map.get(included, make_node_id("blade", included))
            if included not in view_nid_map:
                try:
                    db.upsert_node("BladeTemplate", {
                        "node_id": inc_nid,
                        "name": included,
                        "file_path": "",
                        "relative_path": "",
                        "extends_layout": "",
                        "sections": "[]",
                        "stacks": "[]",
                        "slots": "[]",
                    })
                    view_nid_map[included] = inc_nid
                except Exception:
                    pass
            try:
                db.upsert_rel("INCLUDES_TEMPLATE", "BladeTemplate", nid, "BladeTemplate", inc_nid, {"line": 0})
            except Exception as exc:
                logger.debug("INCLUDES_TEMPLATE rel failed", view=view_name, included=included, error=str(exc))

        # HAS_COMPONENT: <x-component> tags
        for tag in info["x_components"]:
            comp_name = tag.replace("-", "_").replace(":", ".")
            comp_nid = make_node_id("blade_component", comp_name)
            try:
                db._insert_node("BladeComponent", {
                    "node_id": comp_nid,
                    "name": comp_name,
                    "tag": f"x-{tag}",
                    "class_fqn": "",
                    "file_path": "",
                    "props": "[]",
                    "is_anonymous": True,
                })
            except Exception:
                pass
            try:
                db.upsert_rel(
                    "HAS_COMPONENT",
                    "BladeTemplate",
                    nid,
                    "BladeComponent",
                    comp_nid,
                    {"tag": f"x-{tag}", "line": 0},
                )
                component_usages += 1
            except Exception as exc:
                logger.debug("HAS_COMPONENT (x-component) rel failed", view=view_name, tag=tag, error=str(exc))

        # HAS_COMPONENT: @component directives
        for comp_view in info["component_directives"]:
            comp_nid = make_node_id("blade_component", comp_view)
            try:
                db._insert_node("BladeComponent", {
                    "node_id": comp_nid,
                    "name": comp_view,
                    "tag": comp_view,
                    "class_fqn": "",
                    "file_path": "",
                    "props": "[]",
                    "is_anonymous": True,
                })
            except Exception:
                pass
            try:
                db.upsert_rel(
                    "HAS_COMPONENT",
                    "BladeTemplate",
                    nid,
                    "BladeComponent",
                    comp_nid,
                    {"tag": comp_view, "line": 0},
                )
                component_usages += 1
            except Exception as exc:
                logger.debug("HAS_COMPONENT (@component) rel failed", view=view_name, comp=comp_view, error=str(exc))

        # HAS_COMPONENT: @livewire directives
        for lw_name in info["livewire_components"]:
            lw_nid = make_node_id("livewire", lw_name)
            try:
                db._insert_node("LivewireComponent", {
                    "node_id": lw_nid,
                    "name": lw_name,
                    "fqn": "",
                    "file_path": "",
                    "blade_view": "",
                })
            except Exception:
                pass
            try:
                db.upsert_rel(
                    "HAS_COMPONENT",
                    "BladeTemplate",
                    nid,
                    "LivewireComponent",
                    lw_nid,
                    {"tag": f"@livewire({lw_name})", "line": 0},
                )
                component_usages += 1
            except Exception as exc:
                logger.debug("HAS_COMPONENT (@livewire) rel failed", view=view_name, lw=lw_name, error=str(exc))

    # Link PHP symbols to templates via RENDERS_TEMPLATE
    # Scan all PHP files (not just controllers — Mailables, Livewire, etc. also call view())
    for php_path in ctx.php_files:
        try:
            rel_str = php_path.relative_to(ctx.project_root).as_posix()
        except ValueError:
            rel_str = str(php_path)

        try:
            source = php_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        if "view(" not in source:
            continue  # Fast-path: skip files with no view() calls at all

        line_offsets = _build_line_offsets(source)
        parsed = ctx.parsed_php.get(str(php_path))

        for m in _VIEW_CALL_RE.finditer(source):
            view_name = m.group(1)
            template_nid = view_nid_map.get(view_name, make_node_id("blade", view_name))
            call_line = _offset_to_line(m.start(), line_offsets)

            source_nid: str | None = None
            source_label: str | None = None

            if parsed:
                # Strategy 1: find the enclosing method by line range
                for cls in (getattr(parsed, "classes", None) or []):
                    for method in (cls.methods or []):
                        if method.line_start <= call_line <= method.line_end:
                            method_fqn = f"{cls.fqn}::{method.name}"
                            nid = ctx.fqn_index.get(method_fqn)
                            if nid:
                                source_nid = nid
                                source_label = "Method"
                            break
                    if source_nid:
                        break

                # Strategy 2: fall back to enclosing class
                if not source_nid:
                    for cls in (getattr(parsed, "classes", None) or []):
                        if cls.line_start <= call_line <= cls.line_end:
                            nid = ctx.fqn_index.get(cls.fqn)
                            if nid:
                                source_nid = nid
                                source_label = "Class_"
                            break

            if source_nid and source_label:
                try:
                    db.upsert_rel(
                        "RENDERS_TEMPLATE",
                        source_label,
                        source_nid,
                        "BladeTemplate",
                        template_nid,
                        {"line": call_line},
                    )
                except Exception as exc:
                    logger.debug(
                        "RENDERS_TEMPLATE rel failed",
                        source_label=source_label,
                        source_nid=source_nid,
                        view=view_name,
                        error=str(exc),
                    )
            else:
                # Could not resolve source symbol — skip entirely, never use File node
                logger.debug(
                    "RENDERS_TEMPLATE: no enclosing method/class found — skipping",
                    file=rel_str,
                    view=view_name,
                    line=call_line,
                )

    # ── Blade static-call detection → CALLS edges ─────────────────────────────
    # Find ClassName::method() calls in Blade files and create CALLS edges so
    # that phase 10 does not mark those methods as dead code.
    for blade_path in ctx.blade_files:
        source = _read_source(blade_path)
        if "::" not in source:
            continue  # fast-path: skip templates with no static calls at all

        view_name = _view_name_from_path(blade_path, ctx.project_root)
        blade_nid = view_nid_map.get(view_name, make_node_id("blade", view_name))

        calls = _extract_blade_static_calls(source, ctx.class_map, composer_namespace)
        for class_fqn, method_name in calls:
            method_nid = make_node_id("method", class_fqn, method_name)
            try:
                db.upsert_rel(
                    "BLADE_CALLS",
                    "BladeTemplate", blade_nid,
                    "Method", method_nid,
                    {"line": 0},
                )
                blade_calls_created += 1
            except Exception as exc:
                logger.debug(
                    "BLADE_CALLS edge failed",
                    blade=view_name,
                    class_fqn=class_fqn,
                    method=method_name,
                    error=str(exc),
                )

    ctx.stats["templates_parsed"] = templates_parsed
    ctx.stats["component_usages"] = component_usages
    ctx.stats["blade_calls_indexed"] = blade_calls_created
    logger.info(
        "Blade template graph built",
        templates=templates_parsed,
        components=component_usages,
        blade_calls=blade_calls_created,
    )
