"""Phase 16 — Service Container Binding Analysis.

Parse service providers to extract IoC container bindings (singleton, bind,
instance, tag, contextual) and build ServiceBinding nodes with BINDS_TO edges
pointing to concrete implementations.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Binding method → binding_type mapping
_BINDING_METHODS: dict[str, str] = {
    "singleton": "singleton",
    "scoped": "singleton",
    "bind": "transient",
    "instance": "instance",
    "tag": "tagged",
    "extend": "transient",
}

# Pattern: $this->app->singleton(Interface::class, Concrete::class)
# or       $this->app->bind(Interface::class, fn(...) => new Concrete(...))
# or       app()->singleton('key', ...)
_BINDING_PATTERN = re.compile(
    r"(?:\\\$(?:this->app|app)|app\s*\(\s*\))\s*->\s*"
    r"(singleton|scoped|bind|instance|tag|extend)\s*\(\s*"
    r"([^,)]+)"                  # abstract (first arg)
    r"(?:\s*,\s*([^)]+))?"       # optional concrete (second arg, may span multiple lines)
    r"\s*\)",
    re.DOTALL,
)

# Contextual binding:
# $this->app->when(Consumer::class)->needs(Interface::class)->give(Concrete::class)
_CONTEXTUAL_PATTERN = re.compile(
    r"(?:\\\$(?:this->app|app)|app\s*\(\s*\))\s*->\s*when\s*\(\s*([^)]+)\s*\)"
    r"\s*->\s*needs\s*\(\s*([^)]+)\s*\)"
    r"\s*->\s*give\s*\(\s*([^)]+)\s*\)",
    re.DOTALL,
)

# Extract class reference: SomeClass::class or 'some.key' or "some.key"
_CLASS_REF_PAT = re.compile(r"([\w\\]+)::class")
_STRING_PAT = re.compile(r"['\"]([^'\"]+)['\"]")

# Try to extract a concrete class from closure body: fn(...) => new ClassName(...)
_NEW_INSTANCE_PAT = re.compile(r"new\s+([\w\\]+)\s*\(")

# Tagged bindings: $this->app->tag([A::class, B::class], 'tag_name')
_TAG_ARRAY_PAT = re.compile(
    r"(?:\\\$(?:this->app|app)|app\s*\(\s*\))\s*->\s*tag\s*\(\s*\[([^\]]+)\]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
    re.DOTALL,
)


def _extract_fqn(snippet: str) -> str:
    """Extract the FQN from a PHP snippet (class::class or 'string')."""
    snippet = snippet.strip()
    class_m = _CLASS_REF_PAT.search(snippet)
    if class_m:
        return class_m.group(1).lstrip("\\")
    str_m = _STRING_PAT.search(snippet)
    if str_m:
        return str_m.group(1)
    return snippet.strip("'\" ")


def _extract_concrete(concrete_raw: str) -> str:
    """Extract a concrete class FQN from the second argument of a binding.

    Handles:
    - Concrete::class
    - 'concrete.key'
    - fn($app) => new Concrete($app->make(...))
    - Closure with new statement
    """
    if not concrete_raw:
        return ""
    concrete_raw = concrete_raw.strip()

    # Direct class reference
    class_m = _CLASS_REF_PAT.search(concrete_raw)
    if class_m:
        return class_m.group(1).lstrip("\\")

    # String literal
    str_m = _STRING_PAT.match(concrete_raw)
    if str_m:
        return str_m.group(1)

    # Closure with new statement
    new_m = _NEW_INSTANCE_PAT.search(concrete_raw)
    if new_m:
        return new_m.group(1).lstrip("\\")

    return ""


def _parse_bindings_in_source(
    source: str,
    file_path: str,
    provider_fqn: str,
) -> list[dict[str, Any]]:
    """Return a list of binding dicts found in a service provider source file."""
    bindings: list[dict[str, Any]] = []

    # Standard bindings
    for m in _BINDING_PATTERN.finditer(source):
        method = m.group(1)
        abstract_raw = m.group(2) or ""
        concrete_raw = m.group(3) or ""

        abstract = _extract_fqn(abstract_raw)
        concrete = _extract_concrete(concrete_raw)
        binding_type = _BINDING_METHODS.get(method, "transient")

        if not abstract:
            continue

        # Approximate line number
        line = source[: m.start()].count("\n") + 1

        bindings.append({
            "abstract": abstract,
            "concrete": concrete,
            "binding_type": binding_type,
            "provider_fqn": provider_fqn,
            "file_path": file_path,
            "line": line,
        })

    # Contextual bindings
    for m in _CONTEXTUAL_PATTERN.finditer(source):
        consumer_raw = m.group(1)
        abstract_raw = m.group(2)
        concrete_raw = m.group(3)

        abstract = _extract_fqn(abstract_raw)
        concrete = _extract_concrete(concrete_raw)
        consumer = _extract_fqn(consumer_raw)

        if not abstract:
            continue

        line = source[: m.start()].count("\n") + 1

        bindings.append({
            "abstract": abstract,
            "concrete": concrete,
            "binding_type": "contextual",
            "provider_fqn": provider_fqn,
            "file_path": file_path,
            "line": line,
            "contextual_for": consumer,
        })

    # Tagged bindings
    for m in _TAG_ARRAY_PAT.finditer(source):
        classes_raw = m.group(1)
        tag_name = m.group(2)
        line = source[: m.start()].count("\n") + 1

        for class_m in _CLASS_REF_PAT.finditer(classes_raw):
            abstract = class_m.group(1).lstrip("\\")
            bindings.append({
                "abstract": abstract,
                "concrete": abstract,
                "binding_type": "tagged",
                "provider_fqn": provider_fqn,
                "file_path": file_path,
                "line": line,
                "tag": tag_name,
            })

    return bindings


def run(ctx: PipelineContext) -> None:
    """Parse service providers to extract container bindings."""
    db = ctx.db
    class_map = ctx.class_map
    bindings_detected = 0

    # Fetch all ServiceProvider classes
    try:
        provider_rows = db.execute(
            "MATCH (c:Class_ {laravel_role: 'provider'}) "
            "RETURN c.node_id AS nid, c.fqn AS fqn, c.file_path AS fp"
        )
    except Exception as exc:
        logger.error("Failed to fetch ServiceProvider classes", error=str(exc))
        provider_rows = []

    # Also check ServiceProvider nodes created in earlier phases
    try:
        sp_rows = db.execute(
            "MATCH (sp:ServiceProvider) "
            "RETURN sp.node_id AS nid, sp.fqn AS fqn, sp.file_path AS fp"
        )
        provider_rows = list(provider_rows) + list(sp_rows)
    except Exception:
        pass

    # Deduplicate by FQN
    seen_fqns: set[str] = set()
    unique_providers = []
    for row in provider_rows:
        fqn = row.get("fqn") or ""
        if fqn and fqn not in seen_fqns:
            seen_fqns.add(fqn)
            unique_providers.append(row)

    logger.info("Analyzing service providers", count=len(unique_providers))

    for row in unique_providers:
        provider_fqn = row.get("fqn") or ""
        file_path = row.get("fp") or ""

        if not file_path:
            # Try to find via class_map
            if provider_fqn in class_map:
                file_path = str(class_map[provider_fqn])

        if not file_path:
            continue

        try:
            source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("Cannot read provider file", path=file_path, error=str(exc))
            continue

        try:
            bindings = _parse_bindings_in_source(source, file_path, provider_fqn)
        except Exception as exc:
            logger.warning("Failed to parse bindings", fqn=provider_fqn, error=str(exc))
            continue

        for binding in bindings:
            try:
                abstract = binding["abstract"]
                concrete = binding["concrete"]
                binding_type = binding["binding_type"]
                line = binding.get("line", 0)
                contextual_for = binding.get("contextual_for", "")

                binding_nid = make_node_id("binding", abstract)

                db._insert_node("ServiceBinding", {
                    "node_id": binding_nid,
                    "abstract": abstract,
                    "concrete": concrete,
                    "binding_type": binding_type,
                    "provider_fqn": provider_fqn,
                    "file_path": file_path,
                    "line": line,
                })

                # Create BINDS_TO relationship if concrete class is known
                if concrete:
                    concrete_nid = make_node_id("class", concrete)

                    # Check if the concrete class exists as Class_ node
                    if concrete in class_map or db.node_exists("Class_", concrete_nid):
                        try:
                            db.upsert_rel(
                                "BINDS_TO",
                                "ServiceBinding",
                                binding_nid,
                                "Class_",
                                concrete_nid,
                                {
                                    "binding_type": binding_type,
                                    "contextual_for": contextual_for,
                                },
                            )
                        except Exception as exc2:
                            logger.debug(
                                "BINDS_TO rel failed",
                                binding=binding_nid,
                                concrete=concrete,
                                error=str(exc2),
                            )

                bindings_detected += 1

            except Exception as exc:
                logger.debug(
                    "Failed to insert binding",
                    abstract=binding.get("abstract"),
                    provider=provider_fqn,
                    error=str(exc),
                )

    # Also scan the main config/app.php for static provider registrations
    app_config = ctx.project_root / "config" / "app.php"
    if app_config.exists():
        try:
            source = app_config.read_text(encoding="utf-8", errors="replace")
            extra = _parse_bindings_in_source(source, str(app_config), "config/app.php")
            for binding in extra:
                try:
                    abstract = binding["abstract"]
                    concrete = binding["concrete"]
                    binding_type = binding["binding_type"]
                    binding_nid = make_node_id("binding", abstract)

                    db._insert_node("ServiceBinding", {
                        "node_id": binding_nid,
                        "abstract": abstract,
                        "concrete": concrete,
                        "binding_type": binding_type,
                        "provider_fqn": "config/app.php",
                        "file_path": str(app_config),
                        "line": binding.get("line", 0),
                    })
                    bindings_detected += 1
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Failed to parse config/app.php bindings", error=str(exc))

    ctx.stats["bindings_detected"] = bindings_detected

    logger.info(
        "Service container binding analysis complete",
        bindings_detected=bindings_detected,
        providers_analyzed=len(unique_providers),
    )
