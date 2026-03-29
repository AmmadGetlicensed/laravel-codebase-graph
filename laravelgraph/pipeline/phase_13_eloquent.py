"""Phase 13 — Eloquent Relationship Graph.

Parse Eloquent model classes to extract relationship methods (hasMany, belongsTo,
etc.) and build EloquentModel nodes with HAS_RELATIONSHIP edges between them.
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

ELOQUENT_RELATIONSHIPS = [
    "hasOne",
    "hasMany",
    "belongsTo",
    "belongsToMany",
    "hasOneThrough",
    "hasManyThrough",
    "morphOne",
    "morphMany",
    "morphTo",
    "morphToMany",
    "morphedByMany",
]

_POLYMORPHIC = frozenset({"morphOne", "morphMany", "morphTo", "morphToMany", "morphedByMany"})

_REL_PATTERN = re.compile(
    r"return\s+\$this->(" + "|".join(ELOQUENT_RELATIONSHIPS) + r")\s*\(\s*([^)]+)\)",
    re.DOTALL,
)

# Pattern to extract the class reference and optional extra args
_CLASS_REF_PATTERN = re.compile(r"([A-Za-z_\\]+)::class")
_STRING_ARG_PATTERN = re.compile(r"['\"]([^'\"]+)['\"]")

# Parse $fillable, $guarded, $casts, $with, $table arrays
# Note: r"\$foo" in Python regex means literal "$foo" (dollar sign, not anchor)
_TABLE_PATTERN = re.compile(r"\$table\s*=\s*['\"]([^'\"]+)['\"]")
_TIMESTAMPS_PATTERN = re.compile(r"\$timestamps\s*=\s*(false|true)")
_SOFT_DELETES_PATTERN = re.compile(r"use\s+SoftDeletes\s*[;,{]")
_FILLABLE_PATTERN = re.compile(r"\$fillable\s*=\s*\[([^\]]*)\]", re.DOTALL)
_GUARDED_PATTERN = re.compile(r"\$guarded\s*=\s*\[([^\]]*)\]", re.DOTALL)
_CASTS_PATTERN = re.compile(r"\$casts\s*=\s*\[([^\]]*)\]", re.DOTALL)
_WITH_PATTERN = re.compile(r"\$with\s*=\s*\[([^\]]*)\]", re.DOTALL)
_STRING_LIST_PATTERN = re.compile(r"['\"]([^'\"]+)['\"]")


def _class_name_to_table(class_name: str) -> str:
    """Convert a class name like UserProfile to user_profiles (snake_case plural)."""
    # Insert underscore before uppercase sequences
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", class_name)
    s2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
    # Simple plural: append 's' (not perfect but good enough without inflection library)
    if s2.endswith("y"):
        return s2[:-1] + "ies"
    if s2.endswith(("s", "x", "z", "ch", "sh")):
        return s2 + "es"
    return s2 + "s"


def _parse_array_strings(raw: str) -> list[str]:
    """Extract string values from a PHP array literal."""
    return _STRING_LIST_PATTERN.findall(raw)


def _parse_casts_dict(raw: str) -> dict[str, str]:
    """Extract key => value pairs from a PHP $casts array."""
    result: dict[str, str] = {}
    pair_pattern = re.compile(r"['\"]([^'\"]+)['\"]\s*=>\s*['\"]([^'\"]+)['\"]")
    for m in pair_pattern.finditer(raw):
        result[m.group(1)] = m.group(2)
    return result


def _parse_model_metadata(source: str, class_name: str) -> dict[str, Any]:
    """Extract table name, fillable, guarded, casts, with, soft_deletes, timestamps."""
    meta: dict[str, Any] = {}

    # Table
    table_m = _TABLE_PATTERN.search(source)
    meta["db_table"] = table_m.group(1) if table_m else _class_name_to_table(class_name)

    # Timestamps
    ts_m = _TIMESTAMPS_PATTERN.search(source)
    meta["timestamps"] = True if not ts_m else (ts_m.group(1) != "false")

    # Soft deletes
    meta["soft_deletes"] = bool(_SOFT_DELETES_PATTERN.search(source))

    # Fillable
    fill_m = _FILLABLE_PATTERN.search(source)
    meta["fillable"] = json.dumps(_parse_array_strings(fill_m.group(1)) if fill_m else [])

    # Guarded
    guard_m = _GUARDED_PATTERN.search(source)
    meta["guarded"] = json.dumps(_parse_array_strings(guard_m.group(1)) if guard_m else [])

    # Casts
    casts_m = _CASTS_PATTERN.search(source)
    meta["casts"] = json.dumps(_parse_casts_dict(casts_m.group(1)) if casts_m else {})

    # With (eager loads)
    with_m = _WITH_PATTERN.search(source)
    meta["eager_loads"] = json.dumps(_parse_array_strings(with_m.group(1)) if with_m else [])

    return meta


def _parse_relationships(source: str) -> list[dict[str, Any]]:
    """Extract all Eloquent relationship method calls from source."""
    # Find each relationship method definition and its body
    method_pattern = re.compile(
        r"public\s+function\s+(\w+)\s*\([^)]*\)\s*(?::\s*\S+\s*)?\{([^}]+)\}",
        re.DOTALL,
    )

    relationships: list[dict[str, Any]] = []

    for method_match in method_pattern.finditer(source):
        method_name = method_match.group(1)
        method_body = method_match.group(2)

        rel_match = _REL_PATTERN.search(method_body)
        if not rel_match:
            continue

        rel_type = rel_match.group(1)
        args_raw = rel_match.group(2)

        # Extract related class (first ::class reference)
        class_ref_m = _CLASS_REF_PATTERN.search(args_raw)
        related_class = class_ref_m.group(1) if class_ref_m else ""
        # Strip leading backslash if present
        related_class = related_class.lstrip("\\")
        # Take just the short class name if no namespace
        if "\\" in related_class:
            related_short = related_class.split("\\")[-1]
        else:
            related_short = related_class

        # Extract string args (foreign_key, local_key, pivot_table)
        string_args = _STRING_ARG_PATTERN.findall(args_raw)

        foreign_key = ""
        local_key = ""
        pivot_table = ""

        if rel_type == "belongsToMany":
            # belongsToMany(Related::class, 'pivot_table', 'fk', 'rk')
            if len(string_args) >= 1:
                pivot_table = string_args[0]
            if len(string_args) >= 2:
                foreign_key = string_args[1]
            if len(string_args) >= 3:
                local_key = string_args[2]
        elif rel_type in ("hasOneThrough", "hasManyThrough"):
            # hasManyThrough(Related::class, Through::class, 'fk1', 'fk2')
            if len(string_args) >= 1:
                foreign_key = string_args[0]
        else:
            if len(string_args) >= 1:
                foreign_key = string_args[0]
            if len(string_args) >= 2:
                local_key = string_args[1]

        relationships.append({
            "method_name": method_name,
            "relationship_type": rel_type,
            "related_class": related_class,
            "related_short": related_short,
            "foreign_key": foreign_key,
            "local_key": local_key,
            "pivot_table": pivot_table,
            "is_polymorphic": rel_type in _POLYMORPHIC,
        })

    return relationships


def run(ctx: PipelineContext) -> None:
    """Build the Eloquent model relationship graph."""
    db = ctx.db
    models_analyzed = 0
    relationships_detected = 0

    # Fetch all model classes
    try:
        model_rows = db.execute(
            "MATCH (c:Class_) WHERE c.laravel_role = 'model' "
            "RETURN c.node_id AS nid, c.name AS name, c.fqn AS fqn, c.file_path AS fp"
        )
    except Exception as exc:
        logger.error("Failed to fetch model classes", error=str(exc))
        return

    # Build a short-name → FQN map for resolving related models
    short_to_fqn: dict[str, str] = {}
    fqn_to_nid: dict[str, str] = {}
    for row in model_rows:
        fqn = row.get("fqn") or ""
        nid = row.get("nid") or ""
        name = row.get("name") or ""
        if fqn and nid:
            fqn_to_nid[fqn] = nid
            if name:
                short_to_fqn[name] = fqn

    for row in model_rows:
        class_nid = row.get("nid") or ""
        class_name = row.get("name") or ""
        class_fqn = row.get("fqn") or ""
        file_path = row.get("fp") or ""

        if not class_fqn or not file_path:
            continue

        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("Cannot read model file", path=file_path, error=str(exc))
            continue

        try:
            meta = _parse_model_metadata(source, class_name)
        except Exception as exc:
            logger.debug("Failed to parse model metadata", fqn=class_fqn, error=str(exc))
            meta = {
                "db_table": _class_name_to_table(class_name),
                "timestamps": True,
                "soft_deletes": False,
                "fillable": "[]",
                "guarded": "[]",
                "casts": "{}",
                "eager_loads": "[]",
            }

        # Create or update EloquentModel node
        model_node_id = make_node_id("model", class_fqn)
        try:
            db.upsert_node("EloquentModel", {
                "node_id": model_node_id,
                "name": class_name,
                "fqn": class_fqn,
                "file_path": file_path,
                "db_table": meta["db_table"],
                "fillable": meta["fillable"],
                "guarded": meta["guarded"],
                "casts": meta["casts"],
                "eager_loads": meta["eager_loads"],
                "soft_deletes": meta["soft_deletes"],
                "timestamps": meta["timestamps"],
            })
        except Exception as exc:
            logger.debug("EloquentModel upsert failed", fqn=class_fqn, error=str(exc))

        # Parse relationships
        try:
            rels = _parse_relationships(source)
        except Exception as exc:
            logger.debug("Failed to parse relationships", fqn=class_fqn, error=str(exc))
            rels = []

        for rel in rels:
            try:
                related_short = rel["related_short"]
                related_fqn = (
                    short_to_fqn.get(related_short)
                    or rel["related_class"]
                )
                related_model_nid = fqn_to_nid.get(related_fqn)

                if not related_model_nid:
                    # Create a placeholder EloquentModel node for the related class
                    related_model_nid = make_node_id("model", related_fqn)
                    try:
                        db._insert_node("EloquentModel", {
                            "node_id": related_model_nid,
                            "name": related_short,
                            "fqn": related_fqn,
                            "file_path": "",
                            "db_table": _class_name_to_table(related_short),
                            "fillable": "[]",
                            "guarded": "[]",
                            "casts": "{}",
                            "eager_loads": "[]",
                            "soft_deletes": False,
                            "timestamps": True,
                        })
                        fqn_to_nid[related_fqn] = related_model_nid
                    except Exception:
                        pass

                db.upsert_rel(
                    "HAS_RELATIONSHIP",
                    "EloquentModel",
                    model_node_id,
                    "EloquentModel",
                    related_model_nid,
                    {
                        "relationship_type": rel["relationship_type"],
                        "foreign_key": rel["foreign_key"],
                        "local_key": rel["local_key"],
                        "pivot_table": rel["pivot_table"],
                        "method_name": rel["method_name"],
                        "is_polymorphic": rel["is_polymorphic"],
                        "morphable_type": "",
                    },
                )
                relationships_detected += 1

            except Exception as exc:
                logger.debug(
                    "Failed to create HAS_RELATIONSHIP",
                    from_fqn=class_fqn,
                    method=rel.get("method_name"),
                    error=str(exc),
                )

        models_analyzed += 1

    ctx.stats["models_analyzed"] = models_analyzed
    ctx.stats["relationships_detected"] = relationships_detected

    logger.info(
        "Eloquent relationship analysis complete",
        models_analyzed=models_analyzed,
        relationships_detected=relationships_detected,
    )
