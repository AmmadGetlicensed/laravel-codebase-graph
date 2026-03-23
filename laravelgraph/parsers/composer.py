"""Parses composer.json to extract PSR-4 autoloading, Laravel version, and package info."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PSR4Mapping:
    namespace: str   # e.g. "App\\"
    path: str        # e.g. "app/"


@dataclass
class ComposerInfo:
    name: str
    description: str
    php_constraint: str
    laravel_version: str   # extracted from require["laravel/framework"]
    psr4_mappings: list[PSR4Mapping]
    psr4_dev_mappings: list[PSR4Mapping]
    extra_laravel: dict[str, list[str]]   # providers, aliases
    scripts: dict[str, list[str]]
    packages: dict[str, str]              # all require entries
    dev_packages: dict[str, str]
    errors: list[str] = field(default_factory=list)


def parse_composer(path: Path) -> ComposerInfo:
    """Parse a composer.json file and return structured info."""
    errors: list[str] = []
    if not path.exists():
        return _empty(errors=["composer.json not found"])

    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        return _empty(errors=[f"Failed to parse composer.json: {e}"])

    # PSR-4 mappings
    psr4: list[PSR4Mapping] = []
    psr4_dev: list[PSR4Mapping] = []

    autoload = data.get("autoload", {})
    for ns, paths in autoload.get("psr-4", {}).items():
        if isinstance(paths, str):
            paths = [paths]
        for p in paths:
            psr4.append(PSR4Mapping(namespace=ns, path=p))

    autoload_dev = data.get("autoload-dev", {})
    for ns, paths in autoload_dev.get("psr-4", {}).items():
        if isinstance(paths, str):
            paths = [paths]
        for p in paths:
            psr4_dev.append(PSR4Mapping(namespace=ns, path=p))

    # Laravel version from require
    require = data.get("require", {})
    require_dev = data.get("require-dev", {})
    laravel_constraint = require.get("laravel/framework", "")
    laravel_version = _extract_version(laravel_constraint)

    php_constraint = require.get("php", "")

    # Extra Laravel config (package auto-discovery)
    extra_laravel = data.get("extra", {}).get("laravel", {})
    providers = extra_laravel.get("providers", [])
    aliases = extra_laravel.get("aliases", {})

    # Scripts
    scripts_raw = data.get("scripts", {})
    scripts: dict[str, list[str]] = {}
    for k, v in scripts_raw.items():
        if isinstance(v, str):
            scripts[k] = [v]
        elif isinstance(v, list):
            scripts[k] = [str(x) for x in v]

    return ComposerInfo(
        name=data.get("name", ""),
        description=data.get("description", ""),
        php_constraint=php_constraint,
        laravel_version=laravel_version,
        psr4_mappings=psr4,
        psr4_dev_mappings=psr4_dev,
        extra_laravel={"providers": providers, "aliases": list(aliases.keys())},
        scripts=scripts,
        packages=require,
        dev_packages=require_dev,
        errors=errors,
    )


def _extract_version(constraint: str) -> str:
    """Extract a normalized version string from a Composer version constraint."""
    if not constraint:
        return "unknown"
    # Common patterns: "^10.0", "~11.0", ">=9.0", "10.*"
    m = re.search(r"(\d+)(?:\.(\d+))?", constraint)
    if m:
        major = m.group(1)
        minor = m.group(2) or "x"
        return f"{major}.{minor}"
    return constraint


def build_class_map(
    project_root: Path,
    psr4_mappings: list[PSR4Mapping],
) -> dict[str, Path]:
    """Build a namespace→file mapping from PSR-4 rules.

    Returns: {fully_qualified_class_name: absolute_file_path}
    """
    class_map: dict[str, Path] = {}

    for mapping in psr4_mappings:
        ns_prefix = mapping.namespace  # e.g. "App\\"
        dir_path = (project_root / mapping.path).resolve()
        if not dir_path.exists():
            continue

        for php_file in dir_path.rglob("*.php"):
            rel = php_file.relative_to(dir_path)
            # Convert path to class name
            parts = list(rel.parts)
            parts[-1] = parts[-1][:-4]  # remove .php
            class_suffix = "\\".join(parts)
            fqn = f"{ns_prefix}{class_suffix}"
            class_map[fqn] = php_file

    return class_map


def _empty(errors: list[str] | None = None) -> ComposerInfo:
    return ComposerInfo(
        name="", description="", php_constraint="", laravel_version="unknown",
        psr4_mappings=[], psr4_dev_mappings=[], extra_laravel={},
        scripts={}, packages={}, dev_packages={}, errors=errors or [],
    )
