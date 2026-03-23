"""Phase 20 — Config and Environment Variable Dependencies.

Map config() and env() calls to ConfigKey and EnvVariable nodes,
and create USES_CONFIG / USES_ENV relationships.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# config() and Config::get() calls
_CONFIG_CALL_RE = re.compile(
    r"""(?:config\s*\(\s*|Config::get\s*\(\s*)['"]([^'"]+)['"]""",
)
# env() calls: env('KEY') or env('KEY', 'default')
_ENV_CALL_RE = re.compile(
    r"""env\s*\(\s*['"]([^'"]+)['"]\s*(?:,\s*([^)]+))?\s*\)""",
)
# Line number helper
_NEWLINE_RE = re.compile(r"\n")


def _line_of_offset(source: str, offset: int) -> int:
    """Return 1-based line number for a given character offset in source."""
    return source.count("\n", 0, offset) + 1


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env or .env.example file and return {VAR: default_value}."""
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name:
                result[name] = value
    return result


def _parse_config_file(path: Path) -> list[str]:
    """Return top-level keys from a config/*.php file as 'filename.key' strings."""
    source = _read_text(path)
    config_name = path.stem  # e.g. "app", "database"

    # Match top-level array keys: 'key' =>
    key_re = re.compile(r"^\s*['\"](\w+)['\"]\s*=>", re.MULTILINE)
    keys = [f"{config_name}.{m.group(1)}" for m in key_re.finditer(source)]
    return keys


def run(ctx: PipelineContext) -> None:
    """Map config() and env() calls to graph nodes."""
    db = ctx.db
    config_keys_found = 0
    env_vars_found = 0

    # ── Step 1: Collect defined env variables from .env.example / .env ──────
    defined_env: dict[str, str] = {}
    for env_filename in (".env.example", ".env"):
        env_path = ctx.project_root / env_filename
        if env_path.exists():
            defined_env.update(_parse_env_file(env_path))

    # ── Step 2: Collect defined config keys from config/*.php ────────────────
    defined_config: dict[str, str] = {}  # key → file_path
    config_dir = ctx.project_root / "config"
    if config_dir.is_dir():
        for config_file in sorted(config_dir.glob("*.php")):
            for key in _parse_config_file(config_file):
                defined_config[key] = str(config_file)
                # Create ConfigKey nodes for defined keys
                key_nid = make_node_id("config", key)
                try:
                    db._insert_node("ConfigKey", {
                        "node_id": key_nid,
                        "key": key,
                        "file_path": str(config_file),
                        "default_value": "",
                    })
                except Exception:
                    pass  # May already exist

    # ── Step 3: Collect defined env vars and create EnvVariable nodes ────────
    for var_name, default_val in defined_env.items():
        var_nid = make_node_id("env", var_name)
        try:
            db._insert_node("EnvVariable", {
                "node_id": var_nid,
                "name": var_name,
                "default_value": default_val,
                "has_default": True,
            })
        except Exception:
            pass

    # ── Step 4: Scan PHP files for config() and env() calls ─────────────────
    # Track seen config keys and env vars to avoid duplicate node creation
    seen_config: set[str] = set()
    seen_env: set[str] = set()

    for php_path in ctx.php_files:
        try:
            rel_str = php_path.relative_to(ctx.project_root).as_posix()
        except ValueError:
            rel_str = str(php_path)

        source = _read_text(php_path)
        if not source:
            continue

        file_nid = make_node_id("file", rel_str)

        # config() calls
        for m in _CONFIG_CALL_RE.finditer(source):
            key = m.group(1)
            line = _line_of_offset(source, m.start())
            key_nid = make_node_id("config", key)

            if key not in seen_config:
                seen_config.add(key)
                # Create node if not already defined from config files
                if key not in defined_config:
                    try:
                        db._insert_node("ConfigKey", {
                            "node_id": key_nid,
                            "key": key,
                            "file_path": "",
                            "default_value": "",
                        })
                    except Exception:
                        pass
                config_keys_found += 1

            # USES_CONFIG: File → ConfigKey
            try:
                db.upsert_rel(
                    "USES_CONFIG",
                    "File",
                    file_nid,
                    "ConfigKey",
                    key_nid,
                    {"key": key, "line": line},
                )
            except Exception as exc:
                logger.debug("USES_CONFIG rel failed", file=rel_str, key=key, error=str(exc))

        # env() calls
        for m in _ENV_CALL_RE.finditer(source):
            var_name = m.group(1)
            raw_default = (m.group(2) or "").strip()
            has_default = bool(raw_default)
            default_val = raw_default.strip("'\"") if has_default else ""
            line = _line_of_offset(source, m.start())
            var_nid = make_node_id("env", var_name)

            if var_name not in seen_env:
                seen_env.add(var_name)
                # Create node if not already from .env.example
                if var_name not in defined_env:
                    try:
                        db._insert_node("EnvVariable", {
                            "node_id": var_nid,
                            "name": var_name,
                            "default_value": default_val,
                            "has_default": has_default,
                        })
                    except Exception:
                        pass
                env_vars_found += 1

            # USES_ENV: File → EnvVariable
            try:
                db.upsert_rel(
                    "USES_ENV",
                    "File",
                    file_nid,
                    "EnvVariable",
                    var_nid,
                    {"variable": var_name, "line": line},
                )
            except Exception as exc:
                logger.debug("USES_ENV rel failed", file=rel_str, var=var_name, error=str(exc))

    ctx.stats["config_keys_found"] = config_keys_found
    ctx.stats["env_vars_found"] = env_vars_found
    logger.info(
        "Config/Env mapping complete",
        config_keys=config_keys_found,
        env_vars=env_vars_found,
    )
