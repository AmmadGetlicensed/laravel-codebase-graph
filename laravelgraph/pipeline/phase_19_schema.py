"""Phase 19 — Database Schema from Migrations.

Parse migration files to reconstruct the database schema: tables, columns,
and foreign key constraints.
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

# Match Schema::create or Schema::table calls
_SCHEMA_CREATE_RE = re.compile(
    r"Schema::create\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*function\s*\([^)]*\)\s*\{(.*?)\}",
    re.DOTALL,
)
_SCHEMA_TABLE_RE = re.compile(
    r"Schema::table\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*function\s*\([^)]*\)\s*\{(.*?)\}",
    re.DOTALL,
)

# Match column definition: $table->type('name', ...)
_COLUMN_DEF_RE = re.compile(
    r"\$table\s*->\s*(\w+)\s*\(\s*['\"]([^'\"]+)['\"]",
)

# Nullable: ->nullable()
_NULLABLE_RE = re.compile(r"->nullable\(\s*\)")
# Default: ->default(value)
_DEFAULT_RE = re.compile(r"->default\s*\(\s*([^)]+)\s*\)")
# Unique: ->unique()
_UNIQUE_RE = re.compile(r"->unique\(\s*\)")
# Unsigned: ->unsigned()
_UNSIGNED_RE = re.compile(r"->unsigned\(\s*\)")

# Foreign keys: $table->foreign('col')->references('id')->on('table')
_FOREIGN_KEY_RE = re.compile(
    r"\$table\s*->\s*foreign\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"(?:[^;]*?->references\s*\(\s*['\"]([^'\"]+)['\"]\s*\))?"
    r"(?:[^;]*?->on\s*\(\s*['\"]([^'\"]+)['\"]\s*\))?"
    r"(?:[^;]*?->onDelete\s*\(\s*['\"]([^'\"]+)['\"]\s*\))?"
    r"(?:[^;]*?->onUpdate\s*\(\s*['\"]([^'\"]+)['\"]\s*\))?",
    re.DOTALL,
)

# foreignId shorthand: $table->foreignId('user_id')->constrained()
_FOREIGN_ID_RE = re.compile(
    r"\$table\s*->\s*foreignId\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
    r"(?:[^;]*?->constrained\s*\(\s*(?:['\"]([^'\"]+)['\"]\s*)?\))?"
    r"(?:[^;]*?->onDelete\s*\(\s*['\"]([^'\"]+)['\"]\s*\))?",
    re.DOTALL,
)

# Column types that map to "pseudo-columns" (not actual data columns)
_NON_COLUMN_METHODS = {
    "primary", "unique", "index", "foreign", "dropColumn", "dropForeign",
    "dropIndex", "dropPrimary", "dropUnique", "rename", "after", "first",
    "engine", "charset", "collation", "temporary", "comment",
}

# Mapping from Laravel Blueprint method → simplified type
_TYPE_MAP: dict[str, str] = {
    "id": "biginteger",
    "bigIncrements": "biginteger",
    "increments": "integer",
    "smallIncrements": "smallinteger",
    "tinyIncrements": "tinyinteger",
    "mediumIncrements": "integer",
    "unsignedBigInteger": "biginteger",
    "unsignedInteger": "integer",
    "unsignedSmallInteger": "smallinteger",
    "unsignedTinyInteger": "tinyinteger",
    "unsignedMediumInteger": "integer",
    "foreignId": "biginteger",
    "foreignUuid": "string",
    "foreignUlid": "string",
    "bigInteger": "biginteger",
    "integer": "integer",
    "smallInteger": "smallinteger",
    "tinyInteger": "tinyinteger",
    "mediumInteger": "integer",
    "float": "float",
    "double": "double",
    "decimal": "decimal",
    "boolean": "boolean",
    "string": "string",
    "char": "string",
    "text": "text",
    "mediumText": "text",
    "longText": "text",
    "tinyText": "text",
    "binary": "binary",
    "json": "json",
    "jsonb": "json",
    "uuid": "string",
    "ulid": "string",
    "ipAddress": "string",
    "macAddress": "string",
    "date": "date",
    "dateTime": "datetime",
    "dateTimeTz": "datetime",
    "time": "time",
    "timeTz": "time",
    "timestamp": "timestamp",
    "timestampTz": "timestamp",
    "timestamps": "timestamp",  # virtual
    "nullableTimestamps": "timestamp",
    "softDeletes": "timestamp",
    "softDeletesTz": "timestamp",
    "year": "year",
    "enum": "enum",
    "set": "set",
    "morphs": "morphs",
    "nullableMorphs": "morphs",
    "ulidMorphs": "morphs",
    "uuidMorphs": "morphs",
    "nullableUlidMorphs": "morphs",
    "nullableUuidMorphs": "morphs",
    "rememberToken": "string",
}


def _parse_default_value(raw: str) -> str:
    """Clean up a raw default value string from PHP source."""
    raw = raw.strip().strip("'\"")
    # Handle PHP constants like true/false/null
    lower = raw.lower()
    if lower in ("true", "false", "null"):
        return lower
    return raw


def _parse_blueprint_body(
    table_name: str,
    body: str,
    file_path: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse a Schema::create/table body and return (columns, foreign_keys)."""
    columns: list[dict[str, Any]] = []
    foreign_keys: list[dict[str, Any]] = []

    # Split body into individual statement lines (crude but effective without PHP execution)
    lines = body.split(";")
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Foreign key definitions (explicit ->foreign() chain)
        fk_match = _FOREIGN_KEY_RE.search(line)
        if fk_match and "->foreign(" in line:
            from_col = fk_match.group(1) or ""
            to_col = fk_match.group(2) or "id"
            to_table = fk_match.group(3) or ""
            on_delete = fk_match.group(4) or ""
            on_update = fk_match.group(5) or ""
            if to_table:
                foreign_keys.append({
                    "from_column": from_col,
                    "to_column": to_col,
                    "to_table": to_table,
                    "on_delete": on_delete,
                    "on_update": on_update,
                })
            continue

        # Column definitions
        col_match = _COLUMN_DEF_RE.search(line)
        if not col_match:
            # Handle ->timestamps() and ->softDeletes() which have no column name arg
            if "->timestamps(" in line:
                columns.append({
                    "name": "created_at",
                    "table_name": table_name,
                    "type": "timestamp",
                    "nullable": True,
                    "default_value": "",
                    "unique": False,
                    "unsigned": False,
                    "file_path": file_path,
                })
                columns.append({
                    "name": "updated_at",
                    "table_name": table_name,
                    "type": "timestamp",
                    "nullable": True,
                    "default_value": "",
                    "unique": False,
                    "unsigned": False,
                    "file_path": file_path,
                })
            elif "->softDeletes(" in line:
                columns.append({
                    "name": "deleted_at",
                    "table_name": table_name,
                    "type": "timestamp",
                    "nullable": True,
                    "default_value": "",
                    "unique": False,
                    "unsigned": False,
                    "file_path": file_path,
                })
            elif "->rememberToken(" in line:
                columns.append({
                    "name": "remember_token",
                    "table_name": table_name,
                    "type": "string",
                    "nullable": True,
                    "default_value": "",
                    "unique": False,
                    "unsigned": False,
                    "file_path": file_path,
                })
            continue

        method = col_match.group(1)
        col_name = col_match.group(2)

        if method in _NON_COLUMN_METHODS:
            continue

        col_type = _TYPE_MAP.get(method, method.lower())

        # Handle foreignId separately for FK creation
        if method == "foreignId":
            # Derive referenced table from column name (e.g., user_id → users)
            if col_name.endswith("_id"):
                to_table_guess = col_name[:-3] + "s"
            else:
                to_table_guess = ""

            fid_match = _FOREIGN_ID_RE.search(line)
            if fid_match:
                explicit_table = fid_match.group(2)
                on_delete = fid_match.group(3) or ""
                if explicit_table:
                    to_table_guess = explicit_table
                if to_table_guess:
                    foreign_keys.append({
                        "from_column": col_name,
                        "to_column": "id",
                        "to_table": to_table_guess,
                        "on_delete": on_delete,
                        "on_update": "",
                    })

        nullable = bool(_NULLABLE_RE.search(line))
        unique = bool(_UNIQUE_RE.search(line))
        unsigned = bool(_UNSIGNED_RE.search(line)) or method.startswith("unsigned") or method == "id"

        default_value = ""
        def_match = _DEFAULT_RE.search(line)
        if def_match:
            default_value = _parse_default_value(def_match.group(1))

        columns.append({
            "name": col_name,
            "table_name": table_name,
            "type": col_type,
            "nullable": nullable,
            "default_value": default_value,
            "unique": unique,
            "unsigned": unsigned,
            "file_path": file_path,
        })

    return columns, foreign_keys


def run(ctx: PipelineContext) -> None:
    """Parse migration files and build database schema graph."""
    db = ctx.db
    tables_parsed = 0
    columns_parsed = 0

    # Track table_name → node_id for FK references
    table_nid_map: dict[str, str] = {}

    for migration_path in ctx.migration_files:
        filename = migration_path.name
        migration_nid = make_node_id("migration", filename)

        try:
            source = migration_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("Cannot read migration file", path=str(migration_path))
            continue

        try:
            db._insert_node("Migration", {
                "node_id": migration_nid,
                "name": filename,
                "file_path": str(migration_path),
                "batch": 0,
                "ran_at": "",
            })
        except Exception as exc:
            logger.debug("Migration node insert failed", name=filename, error=str(exc))

        file_path_str = str(migration_path)

        # Find Schema::create blocks
        for m in _SCHEMA_CREATE_RE.finditer(source):
            table_name = m.group(1)
            body = m.group(2)

            table_nid = make_node_id("table", table_name)
            table_nid_map[table_name] = table_nid

            try:
                db._insert_node("DatabaseTable", {
                    "node_id": table_nid,
                    "name": table_name,
                    "created_in": file_path_str,
                    "engine": "",
                    "charset": "",
                })
                tables_parsed += 1
            except Exception as exc:
                logger.debug("DatabaseTable node insert failed", table=table_name, error=str(exc))

            # MIGRATES_TABLE: Migration → DatabaseTable
            try:
                db.upsert_rel("MIGRATES_TABLE", "Migration", migration_nid, "DatabaseTable", table_nid)
            except Exception as exc:
                logger.debug("MIGRATES_TABLE rel failed", migration=filename, table=table_name, error=str(exc))

            columns, foreign_keys = _parse_blueprint_body(table_name, body, file_path_str)

            for col in columns:
                col_nid = make_node_id("column", f"{table_name}.{col['name']}")
                try:
                    db._insert_node("DatabaseColumn", {
                        "node_id": col_nid,
                        "name": col["name"],
                        "table_name": table_name,
                        "type": col["type"],
                        "nullable": col["nullable"],
                        "default_value": col["default_value"],
                        "unique": col["unique"],
                        "indexed": False,
                        "unsigned": col["unsigned"],
                        "length": 0,
                    })
                    columns_parsed += 1
                except Exception as exc:
                    logger.debug("DatabaseColumn node insert failed", col=col["name"], error=str(exc))

                # HAS_COLUMN: DatabaseTable → DatabaseColumn
                try:
                    db.upsert_rel("HAS_COLUMN", "DatabaseTable", table_nid, "DatabaseColumn", col_nid)
                except Exception as exc:
                    logger.debug("HAS_COLUMN rel failed", table=table_name, col=col["name"], error=str(exc))

            # Deferred FK processing — stash for second pass
            for fk in foreign_keys:
                fk["from_table"] = table_name
                fk["from_table_nid"] = table_nid

            # REFERENCES_TABLE: need to defer until all tables are created
            # Store them for a second pass
            for fk in foreign_keys:
                to_table = fk["to_table"]
                to_nid = table_nid_map.get(to_table, make_node_id("table", to_table))

                # Ensure target table stub exists
                if to_table not in table_nid_map:
                    try:
                        db._insert_node("DatabaseTable", {
                            "node_id": to_nid,
                            "name": to_table,
                            "created_in": "",
                            "engine": "",
                            "charset": "",
                        })
                        table_nid_map[to_table] = to_nid
                    except Exception:
                        pass

                try:
                    db.upsert_rel(
                        "REFERENCES_TABLE",
                        "DatabaseTable",
                        fk["from_table_nid"],
                        "DatabaseTable",
                        to_nid,
                        {
                            "from_column": fk["from_column"],
                            "to_column": fk["to_column"],
                            "on_delete": fk["on_delete"],
                            "on_update": fk["on_update"],
                        },
                    )
                except Exception as exc:
                    logger.debug(
                        "REFERENCES_TABLE rel failed",
                        from_table=fk["from_table"],
                        to_table=to_table,
                        error=str(exc),
                    )

        # Schema::table blocks (modifications only create columns, no new tables by default)
        for m in _SCHEMA_TABLE_RE.finditer(source):
            table_name = m.group(1)
            body = m.group(2)

            table_nid = table_nid_map.get(table_name, make_node_id("table", table_name))

            columns, foreign_keys = _parse_blueprint_body(table_name, body, file_path_str)

            for col in columns:
                col_nid = make_node_id("column", f"{table_name}.{col['name']}")
                try:
                    db._insert_node("DatabaseColumn", {
                        "node_id": col_nid,
                        "name": col["name"],
                        "table_name": table_name,
                        "type": col["type"],
                        "nullable": col["nullable"],
                        "default_value": col["default_value"],
                        "unique": col["unique"],
                        "indexed": False,
                        "unsigned": col["unsigned"],
                        "length": 0,
                    })
                    columns_parsed += 1
                except Exception:
                    pass  # Column may already exist from Schema::create

                try:
                    db.upsert_rel("HAS_COLUMN", "DatabaseTable", table_nid, "DatabaseColumn", col_nid)
                except Exception:
                    pass

            for fk in foreign_keys:
                to_table = fk["to_table"]
                to_nid = table_nid_map.get(to_table, make_node_id("table", to_table))
                try:
                    db.upsert_rel(
                        "REFERENCES_TABLE",
                        "DatabaseTable",
                        table_nid,
                        "DatabaseTable",
                        to_nid,
                        {
                            "from_column": fk["from_column"],
                            "to_column": fk["to_column"],
                            "on_delete": fk["on_delete"],
                            "on_update": fk["on_update"],
                        },
                    )
                except Exception:
                    pass

    ctx.stats["tables_parsed"] = tables_parsed
    ctx.stats["columns_parsed"] = columns_parsed
    logger.info(
        "Database schema built",
        tables=tables_parsed,
        columns=columns_parsed,
    )
