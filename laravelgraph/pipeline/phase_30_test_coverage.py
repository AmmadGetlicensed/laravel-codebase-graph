"""Phase 30 — Test Coverage Mapping.

Discovers PHP test files and links them to the production code they exercise
via TestCase nodes and TESTS edges.

Algorithm
---------
1. Discover test PHP files by scanning the ``tests/`` directory recursively.
2. For each test file, extract:
   a. HTTP calls (``$this->get/post/put/patch/delete('...')``) → route URI.
   b. Class references (``new Foo(``, ``Foo::class``, ``->mock(Foo::class``).
   c. test_type: "feature" | "unit" | "integration" based on path.
3. Create a ``TestCase`` node per test file.
4. Match extracted URIs to Route nodes (exact URI match, then prefix match).
5. Match extracted class names to Class_ nodes via ``ctx.fqn_index``.
6. Create ``TESTS`` edges: TestCase → Route and TestCase → Class_.

Stats: ``test_cases_found``, ``test_links``
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

# ── Compiled patterns ─────────────────────────────────────────────────────────

# HTTP helper calls in Laravel feature tests: $this->get('/api/users')
_HTTP_CALL_RE = re.compile(
    r"\$this\s*->\s*(?:get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)

# Class instantiation: new FooBar(
_NEW_CLASS_RE = re.compile(r"\bnew\s+([\w\\]+)\s*\(")

# ::class references: FooBar::class  or ->mock(FooBar::class)
_CLASS_REF_RE = re.compile(r"\b([\w\\]+)::class\b")

# ── Helpers ───────────────────────────────────────────────────────────────────

_TEST_FILE_SKIP_NAMES: frozenset[str] = frozenset({
    "TestCase", "CreatesApplication", "RefreshDatabase",
    "DatabaseMigrations", "WithFaker",
})


def _read_file(file_path: Path) -> str | None:
    """Return file content or None on error."""
    try:
        return file_path.read_text(errors="replace")
    except OSError as exc:
        logger.debug("Cannot read test file", path=str(file_path), error=str(exc))
        return None


def _detect_test_type(file_path: Path) -> str:
    """Determine test_type from directory structure."""
    parts = [p.lower() for p in file_path.parts]
    if "feature" in parts:
        return "feature"
    if "unit" in parts:
        return "unit"
    return "integration"


def _extract_uris(source: str) -> list[str]:
    """Extract HTTP call URIs from test source."""
    return list(dict.fromkeys(_HTTP_CALL_RE.findall(source)))  # preserve order, dedupe


def _extract_class_refs(source: str) -> list[str]:
    """Extract referenced class short names from test source."""
    refs: set[str] = set()
    for m in _NEW_CLASS_RE.finditer(source):
        name = m.group(1).split("\\")[-1]
        if name and name not in _TEST_FILE_SKIP_NAMES:
            refs.add(name)
    for m in _CLASS_REF_RE.finditer(source):
        name = m.group(1).split("\\")[-1]
        if name and name not in _TEST_FILE_SKIP_NAMES:
            refs.add(name)
    return sorted(refs)


def _derive_fqn(file_path: Path, project_root: Path) -> str:
    """Derive a best-effort FQN for a test file from its path.

    Example:
        tests/Feature/UserTest.php → Tests\\Feature\\UserTest
    """
    try:
        rel = file_path.relative_to(project_root)
    except ValueError:
        rel = file_path
    parts = list(rel.with_suffix("").parts)
    # Capitalise 'tests' → 'Tests' to match PSR-4 autoloading convention
    if parts and parts[0].lower() == "tests":
        parts[0] = "Tests"
    return "\\".join(parts)


# ── Main phase ────────────────────────────────────────────────────────────────


def run(ctx: PipelineContext) -> None:
    """Discover test files and create TestCase nodes + TESTS edges."""
    db = ctx.db
    project_root = ctx.project_root
    test_cases_found = 0
    test_links = 0

    # ── Step 1: Discover test PHP files ───────────────────────────────────
    tests_dir = project_root / "tests"
    if not tests_dir.is_dir():
        logger.info("No tests/ directory found — skipping test coverage mapping")
        ctx.stats["test_cases_found"] = 0
        ctx.stats["test_links"] = 0
        return

    test_files = list(tests_dir.rglob("*.php"))
    if not test_files:
        logger.info("No PHP test files found in tests/ — skipping")
        ctx.stats["test_cases_found"] = 0
        ctx.stats["test_links"] = 0
        return

    logger.info("Test files discovered", count=len(test_files))

    # ── Pre-load Route node index: uri → route_node_id ────────────────────
    route_by_uri: dict[str, str] = {}
    try:
        route_rows: list[dict[str, Any]] = db.execute(
            "MATCH (r:Route) RETURN r.node_id AS nid, r.uri AS uri"
        )
        for row in route_rows:
            uri = row.get("uri") or ""
            nid = row.get("nid") or ""
            if uri and nid:
                # Normalise: strip trailing slash
                route_by_uri[uri.rstrip("/")] = nid
    except Exception as exc:
        logger.warning("Failed to fetch Route nodes", error=str(exc))

    # ── Pre-load Class_ index: short name → list of node_ids ─────────────
    class_by_name: dict[str, list[str]] = {}
    try:
        class_rows: list[dict[str, Any]] = db.execute(
            "MATCH (c:Class_) RETURN c.node_id AS nid, c.name AS name, c.file_path AS fp"
        )
        for row in class_rows:
            name = row.get("name") or ""
            nid = row.get("nid") or ""
            fp = row.get("fp") or ""
            if not name or not nid:
                continue
            # Skip test files themselves
            if "/tests/" in fp.replace("\\", "/").lower():
                continue
            class_by_name.setdefault(name, []).append(nid)
    except Exception as exc:
        logger.warning("Failed to fetch Class_ nodes", error=str(exc))

    # Also index from fqn_index for FQN-based short-name lookup
    for fqn, nid in ctx.fqn_index.items():
        short = fqn.split("\\")[-1]
        if short and nid:
            existing = class_by_name.setdefault(short, [])
            if nid not in existing:
                existing.append(nid)

    # ── Step 2-6: Process each test file ──────────────────────────────────
    for test_file in test_files:
        source = _read_file(test_file)
        if source is None:
            continue

        abs_path = str(test_file.resolve())
        fqn = _derive_fqn(test_file, project_root)
        name = test_file.stem
        test_type = _detect_test_type(test_file)

        uris = _extract_uris(source)
        class_refs = _extract_class_refs(source)

        test_nid = make_node_id("test", fqn)

        try:
            db.upsert_node("TestCase", {
                "node_id": test_nid,
                "name": name,
                "fqn": fqn,
                "file_path": abs_path,
                "test_type": test_type,
                "covers_routes": json.dumps(uris),
                "covers_classes": json.dumps(class_refs),
            })
            test_cases_found += 1
        except Exception as exc:
            logger.warning(
                "Failed to create TestCase node",
                fqn=fqn,
                error=str(exc),
            )
            continue

        # ── Step 4: Link to Route nodes ───────────────────────────────────
        for uri in uris:
            normalised = uri.rstrip("/")
            route_nid = route_by_uri.get(normalised)

            # Fallback: prefix match (e.g. /api/users/{id} vs /api/users)
            if not route_nid:
                for r_uri, r_nid in route_by_uri.items():
                    if r_uri.startswith(normalised) or normalised.startswith(r_uri):
                        route_nid = r_nid
                        break

            if not route_nid:
                continue

            try:
                db.upsert_rel(
                    "TESTS",
                    from_label="TestCase",
                    from_id=test_nid,
                    to_label="Route",
                    to_id=route_nid,
                    props={},
                )
                test_links += 1
            except Exception as exc:
                logger.debug(
                    "Failed to link TestCase to Route",
                    test_nid=test_nid,
                    route_nid=route_nid,
                    error=str(exc),
                )

        # ── Step 5: Link to Class_ nodes ──────────────────────────────────
        for class_name in class_refs:
            candidate_nids = class_by_name.get(class_name, [])
            for class_nid in candidate_nids[:1]:  # link to first match only
                try:
                    db.upsert_rel(
                        "TESTS",
                        from_label="TestCase",
                        from_id=test_nid,
                        to_label="Class_",
                        to_id=class_nid,
                        props={},
                    )
                    test_links += 1
                except Exception as exc:
                    logger.debug(
                        "Failed to link TestCase to Class_",
                        test_nid=test_nid,
                        class_nid=class_nid,
                        error=str(exc),
                    )

    ctx.stats["test_cases_found"] = test_cases_found
    ctx.stats["test_links"] = test_links

    logger.info(
        "Test coverage mapping complete",
        test_cases_found=test_cases_found,
        test_links=test_links,
    )
