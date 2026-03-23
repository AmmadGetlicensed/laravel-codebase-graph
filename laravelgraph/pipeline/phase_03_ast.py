"""Phase 03 — AST Parsing.

Parse all PHP and Blade files, store every symbol as a graph node, and
link them back to their source File nodes with DEFINES relationships.
Also builds ctx.class_map and ctx.fqn_index.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.parsers.blade import BladeParser
from laravelgraph.parsers.composer import build_class_map
from laravelgraph.parsers.php import (
    ELOQUENT_RELATIONSHIPS,
    PHPFile,
    PHPParser,
    ParsedClass,
)
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Superclass → laravel_role mapping used for role detection when directory
# heuristics aren't enough.
_SUPERCLASS_ROLE: dict[str, str] = {
    "Model": "model",
    "Authenticatable": "model",
    "Pivot": "model",
    "Controller": "controller",
    "BaseController": "controller",
    "FormRequest": "request",
    "Resource": "resource",
    "JsonResource": "resource",
    "ResourceCollection": "resource",
    "Notification": "notification",
    "Mailable": "mailable",
    "Job": "job",
    "Queueable": "job",
    "Event": "event",
    "Listener": "listener",
    "Policy": "policy",
    "Observer": "observer",
    "Command": "command",
    "ServiceProvider": "provider",
    "Factory": "factory",
    "Seeder": "seeder",
    "DatabaseSeeder": "seeder",
    "Middleware": "middleware",
}


def _derive_laravel_role(cls: ParsedClass, file_role: str) -> str:
    """Return a refined laravel_role for a class node."""
    if file_role not in ("php", ""):
        return file_role
    if cls.extends:
        base = cls.extends.split("\\")[-1]
        role = _SUPERCLASS_ROLE.get(base)
        if role:
            return role
    return "php"


def _method_laravel_role(method_name: str) -> str:
    """Tag well-known Laravel method names with a semantic role."""
    if method_name in ELOQUENT_RELATIONSHIPS:
        return "relationship"
    if method_name.startswith("scope") and len(method_name) > 5 and method_name[5].isupper():
        return "scope"
    if method_name.startswith("get") and method_name.endswith("Attribute"):
        return "accessor"
    if method_name.startswith("set") and method_name.endswith("Attribute"):
        return "mutator"
    if method_name in ("boot", "booted"):
        return "boot"
    if method_name in ("register",):
        return "register"
    if method_name in ("handle",):
        return "handle"
    return ""


def _insert_namespace(db: Any, namespace: str) -> str:
    """Ensure a Namespace node exists and return its node_id."""
    nid = make_node_id("namespace", namespace)
    try:
        db._insert_node("Namespace", {
            "node_id": nid,
            "name": namespace.split("\\")[-1],
            "fqn": namespace,
        })
    except Exception:
        pass  # already exists
    return nid


def run(ctx: PipelineContext) -> None:
    """Parse all PHP and Blade files and populate the graph with symbols."""
    with phase_timer("AST Parsing"):
        project_root = ctx.project_root
        db = ctx.db
        php_parser = PHPParser()
        blade_parser = BladeParser()

        file_roles: dict[str, str] = getattr(ctx, "_file_roles", {})

        # Build class_map from PSR-4 mappings
        all_psr4 = ctx.composer.psr4_mappings + ctx.composer.psr4_dev_mappings
        ctx.class_map = build_class_map(project_root, all_psr4)

        views_root = project_root / "resources" / "views"

        classes_parsed = 0
        methods_parsed = 0
        functions_parsed = 0
        traits_parsed = 0
        interfaces_parsed = 0

        # ── PHP files ────────────────────────────────────────────────────
        for filepath in ctx.php_files:
            rel_str = _rel(filepath, project_root)
            file_nid = make_node_id("file", rel_str)

            try:
                parsed: PHPFile = php_parser.parse_file(filepath)
            except Exception as e:
                msg = f"PHP parse failed: {filepath}: {e}"
                ctx.errors.append(msg)
                logger.warning("PHP parse exception", path=rel_str, error=str(e))
                continue

            ctx.parsed_php[str(filepath)] = parsed

            if parsed.errors:
                for err in parsed.errors:
                    ctx.errors.append(f"{rel_str}: {err}")

            # Update namespace on File node
            if parsed.namespace:
                try:
                    db._conn.execute(
                        f"MATCH (f:File {{node_id: '{_esc(file_nid)}'}}) "
                        f"SET f.php_namespace = '{_esc(parsed.namespace)}'"
                    )
                except Exception:
                    pass

                _insert_namespace(db, parsed.namespace)

            # ── Classes ──────────────────────────────────────────────────
            for cls in parsed.classes:
                fqn = cls.fqn
                class_nid = make_node_id("class", fqn)
                ctx.fqn_index[fqn] = class_nid

                # Also index the short name within the namespace
                if parsed.namespace and cls.name:
                    ctx.fqn_index[cls.name] = class_nid

                role = _derive_laravel_role(cls, file_roles.get(str(filepath), "php"))

                try:
                    db._insert_node("Class_", {
                        "node_id": class_nid,
                        "name": cls.name,
                        "fqn": fqn,
                        "file_path": str(filepath),
                        "line_start": cls.line_start,
                        "line_end": cls.line_end,
                        "is_abstract": cls.is_abstract,
                        "is_final": cls.is_final,
                        "laravel_role": role,
                        "is_dead_code": False,
                        "community_id": -1,
                        "embedding": [],
                    })
                except Exception as e:
                    logger.debug("Class_ insert failed", fqn=fqn, error=str(e))

                # DEFINES: File → Class_
                try:
                    db.upsert_rel(
                        "DEFINES", "File", file_nid, "Class_", class_nid,
                        props={"symbol_type": "class", "line_start": cls.line_start},
                    )
                except Exception as e:
                    logger.debug("File→Class_ DEFINES failed", fqn=fqn, error=str(e))

                classes_parsed += 1

                # ── Methods ──────────────────────────────────────────────
                for method in cls.methods:
                    method_fqn = f"{fqn}::{method.name}"
                    method_nid = make_node_id("method", fqn, method.name)
                    ctx.fqn_index[method_fqn] = method_nid

                    param_types = json.dumps([p.type_hint for p in method.params])
                    method_role = _method_laravel_role(method.name)

                    try:
                        db._insert_node("Method", {
                            "node_id": method_nid,
                            "name": method.name,
                            "fqn": method_fqn,
                            "file_path": str(filepath),
                            "line_start": method.line_start,
                            "line_end": method.line_end,
                            "visibility": method.visibility,
                            "is_static": method.is_static,
                            "is_abstract": method.is_abstract,
                            "return_type": method.return_type,
                            "param_types": param_types,
                            "docblock": method.docblock,
                            "is_dead_code": False,
                            "laravel_role": method_role,
                            "community_id": -1,
                            "embedding": [],
                        })
                    except Exception as e:
                        logger.debug("Method insert failed", fqn=method_fqn, error=str(e))
                        continue

                    # DEFINES: Class_ → Method
                    try:
                        db.upsert_rel(
                            "DEFINES", "Class_", class_nid, "Method", method_nid,
                            props={"symbol_type": "method", "line_start": method.line_start},
                        )
                    except Exception as e:
                        logger.debug("Class_→Method DEFINES failed", fqn=method_fqn, error=str(e))

                    methods_parsed += 1

            # ── Traits ───────────────────────────────────────────────────
            for trait in parsed.traits:
                fqn = trait.fqn
                trait_nid = make_node_id("trait", fqn)
                ctx.fqn_index[fqn] = trait_nid

                try:
                    db._insert_node("Trait_", {
                        "node_id": trait_nid,
                        "name": trait.name,
                        "fqn": fqn,
                        "file_path": str(filepath),
                        "line_start": trait.line_start,
                        "is_dead_code": False,
                    })
                except Exception as e:
                    logger.debug("Trait_ insert failed", fqn=fqn, error=str(e))

                try:
                    db.upsert_rel(
                        "DEFINES", "File", file_nid, "Trait_", trait_nid,
                        props={"symbol_type": "trait", "line_start": trait.line_start},
                    )
                except Exception as e:
                    logger.debug("File→Trait_ DEFINES failed", fqn=fqn, error=str(e))

                # Methods on trait
                for method in trait.methods:
                    method_fqn = f"{fqn}::{method.name}"
                    method_nid = make_node_id("method", fqn, method.name)
                    ctx.fqn_index[method_fqn] = method_nid
                    param_types = json.dumps([p.type_hint for p in method.params])
                    try:
                        db._insert_node("Method", {
                            "node_id": method_nid,
                            "name": method.name,
                            "fqn": method_fqn,
                            "file_path": str(filepath),
                            "line_start": method.line_start,
                            "line_end": method.line_end,
                            "visibility": method.visibility,
                            "is_static": method.is_static,
                            "is_abstract": method.is_abstract,
                            "return_type": method.return_type,
                            "param_types": param_types,
                            "docblock": method.docblock,
                            "is_dead_code": False,
                            "laravel_role": _method_laravel_role(method.name),
                            "community_id": -1,
                            "embedding": [],
                        })
                        db.upsert_rel(
                            "DEFINES", "Trait_", trait_nid, "Method", method_nid,
                            props={"symbol_type": "method", "line_start": method.line_start},
                        )
                    except Exception as e:
                        logger.debug("Trait method insert failed", fqn=method_fqn, error=str(e))
                    methods_parsed += 1

                traits_parsed += 1

            # ── Interfaces ───────────────────────────────────────────────
            for iface in parsed.interfaces:
                fqn = iface.fqn
                iface_nid = make_node_id("interface", fqn)
                ctx.fqn_index[fqn] = iface_nid

                try:
                    db._insert_node("Interface_", {
                        "node_id": iface_nid,
                        "name": iface.name,
                        "fqn": fqn,
                        "file_path": str(filepath),
                        "line_start": iface.line_start,
                    })
                except Exception as e:
                    logger.debug("Interface_ insert failed", fqn=fqn, error=str(e))

                try:
                    db.upsert_rel(
                        "DEFINES", "File", file_nid, "Interface_", iface_nid,
                        props={"symbol_type": "interface", "line_start": iface.line_start},
                    )
                except Exception as e:
                    logger.debug("File→Interface_ DEFINES failed", fqn=fqn, error=str(e))

                interfaces_parsed += 1

            # ── Enums ────────────────────────────────────────────────────
            for enum in parsed.enums:
                fqn = enum.fqn
                enum_nid = make_node_id("enum", fqn)
                ctx.fqn_index[fqn] = enum_nid

                try:
                    db._insert_node("Enum_", {
                        "node_id": enum_nid,
                        "name": enum.name,
                        "fqn": fqn,
                        "file_path": str(filepath),
                        "line_start": enum.line_start,
                        "backed_type": enum.backed_type,
                    })
                except Exception as e:
                    logger.debug("Enum_ insert failed", fqn=fqn, error=str(e))

                try:
                    db.upsert_rel(
                        "DEFINES", "File", file_nid, "Enum_", enum_nid,
                        props={"symbol_type": "enum", "line_start": enum.line_start},
                    )
                except Exception as e:
                    logger.debug("File→Enum_ DEFINES failed", fqn=fqn, error=str(e))

            # ── Free functions ───────────────────────────────────────────
            for fn in parsed.functions:
                fqn = fn.fqn
                fn_nid = make_node_id("function", fqn)
                ctx.fqn_index[fqn] = fn_nid

                try:
                    db._insert_node("Function_", {
                        "node_id": fn_nid,
                        "name": fn.name,
                        "fqn": fqn,
                        "file_path": str(filepath),
                        "line_start": fn.line_start,
                        "return_type": fn.return_type,
                        "is_dead_code": False,
                        "embedding": [],
                    })
                except Exception as e:
                    logger.debug("Function_ insert failed", fqn=fqn, error=str(e))

                try:
                    db.upsert_rel(
                        "DEFINES", "File", file_nid, "Function_", fn_nid,
                        props={"symbol_type": "function", "line_start": fn.line_start},
                    )
                except Exception as e:
                    logger.debug("File→Function_ DEFINES failed", fqn=fqn, error=str(e))

                functions_parsed += 1

        # ── Blade files ──────────────────────────────────────────────────
        views_root_path = views_root if views_root.exists() else None
        for filepath in ctx.blade_files:
            rel_str = _rel(filepath, project_root)
            try:
                parsed_blade = blade_parser.parse_file(filepath, views_root_path)
                ctx.parsed_blade[str(filepath)] = parsed_blade
            except Exception as e:
                msg = f"Blade parse failed: {filepath}: {e}"
                ctx.errors.append(msg)
                logger.warning("Blade parse exception", path=rel_str, error=str(e))

        ctx.stats["classes_parsed"] = classes_parsed
        ctx.stats["methods_parsed"] = methods_parsed
        ctx.stats["functions_parsed"] = functions_parsed
        ctx.stats["traits_parsed"] = traits_parsed
        ctx.stats["interfaces_parsed"] = interfaces_parsed

        logger.info(
            "AST parsing complete",
            classes=classes_parsed,
            methods=methods_parsed,
            functions=functions_parsed,
            traits=traits_parsed,
            interfaces=interfaces_parsed,
            blade=len(ctx.parsed_blade),
            errors=len(ctx.errors),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _esc(s: str) -> str:
    return s.replace("'", "\\'")
