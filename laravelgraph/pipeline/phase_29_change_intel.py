"""Phase 29 — Change Intelligence.

Git-aware phase that compares the current HEAD commit to the last indexed
commit and annotates changed symbols (Method, Class_, Feature) so that
downstream tools can highlight recently modified code.

Algorithm
---------
1. Run ``git rev-parse HEAD`` to get the current commit hash.
2. Read the stored hash from ``<index_dir>/last_commit.txt``.
3. If no stored hash, or stored hash == current hash, skip the diff.
4. Run ``git diff --name-only <stored_hash> HEAD`` to list changed files.
5. For each changed PHP file, find matching Method and Class_ nodes by
   ``file_path`` and set ``changed_recently = true``,
   ``changed_in_commit = <current_hash>``.
6. Find Feature nodes linked to any changed Class_/Route and mark
   ``has_changes = true``.
7. Write the current hash back to ``last_commit.txt``.

Stats: ``changed_files``, ``changed_methods``, ``changed_classes``
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from laravelgraph.config import index_dir
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Git helpers ───────────────────────────────────────────────────────────────


def _git(args: list[str], cwd: Path) -> str:
    """Run a git sub-command and return stdout, or "" on any failure."""
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _set_property(db: Any, label: str, node_id: str, **props: Any) -> None:
    """SET individual properties on an existing node, ignoring errors."""
    for prop, value in props.items():
        try:
            db._conn.execute(
                f"MATCH (n:{label} {{node_id: $nid}}) SET n.{prop} = $val",
                parameters={"nid": node_id, "val": value},
            )
        except Exception as exc:
            logger.debug(
                "SET property skipped",
                label=label,
                node_id=node_id,
                prop=prop,
                error=str(exc),
            )


# ── Main phase ────────────────────────────────────────────────────────────────


def run(ctx: PipelineContext) -> None:
    """Annotate recently changed symbols via git diff."""
    db = ctx.db
    project_root = ctx.project_root
    idx_dir = index_dir(project_root)

    # ── Step 1: current HEAD hash ──────────────────────────────────────────
    current_hash = _git(["rev-parse", "HEAD"], project_root)
    if not current_hash:
        logger.debug(
            "git rev-parse HEAD returned nothing — "
            "project may not be a git repo; skipping change intelligence"
        )
        ctx.stats["changed_files"] = 0
        ctx.stats["changed_methods"] = 0
        ctx.stats["changed_classes"] = 0
        return

    # ── Step 2: stored hash from last index run ────────────────────────────
    last_commit_file = idx_dir / "last_commit.txt"
    stored_hash: str = ""
    try:
        if last_commit_file.exists():
            stored_hash = last_commit_file.read_text().strip()
    except OSError as exc:
        logger.debug("Could not read last_commit.txt", error=str(exc))

    if stored_hash == current_hash:
        logger.info(
            "HEAD unchanged since last index — skipping change intelligence",
            commit=current_hash[:12],
        )
        ctx.stats["changed_files"] = 0
        ctx.stats["changed_methods"] = 0
        ctx.stats["changed_classes"] = 0
        return

    # ── Step 3: diff ──────────────────────────────────────────────────────
    changed_php_paths: list[str] = []

    if not stored_hash:
        logger.info(
            "No previous commit hash stored — will write current hash "
            "but skip symbol annotation on first run",
            commit=current_hash[:12],
        )
    else:
        raw_diff = _git(["diff", "--name-only", stored_hash, "HEAD"], project_root)
        if not raw_diff:
            logger.debug(
                "git diff returned nothing",
                from_hash=stored_hash[:12],
                to_hash=current_hash[:12],
            )
        else:
            for rel_path in raw_diff.splitlines():
                if rel_path.endswith(".php"):
                    abs_path = str((project_root / rel_path).resolve())
                    changed_php_paths.append(abs_path)

    logger.info(
        "Changed PHP files detected",
        count=len(changed_php_paths),
        from_hash=stored_hash[:12] if stored_hash else "(none)",
        to_hash=current_hash[:12],
    )

    changed_files = len(changed_php_paths)
    changed_methods = 0
    changed_classes = 0
    affected_feature_nids: set[str] = set()

    # ── Step 4 & 5: annotate Method and Class_ nodes ──────────────────────
    for abs_path in changed_php_paths:
        # --- Methods ---
        try:
            method_rows: list[dict[str, Any]] = db.execute(
                "MATCH (m:Method {file_path: $fp}) RETURN m.node_id AS nid",
                {"fp": abs_path},
            )
        except Exception as exc:
            logger.debug("Method query failed", path=abs_path, error=str(exc))
            method_rows = []

        for row in method_rows:
            nid = row.get("nid") or ""
            if not nid:
                continue
            _set_property(db, "Method", nid, changed_recently=True, changed_in_commit=current_hash)
            changed_methods += 1

        # --- Classes ---
        try:
            class_rows: list[dict[str, Any]] = db.execute(
                "MATCH (c:Class_ {file_path: $fp}) RETURN c.node_id AS nid",
                {"fp": abs_path},
            )
        except Exception as exc:
            logger.debug("Class_ query failed", path=abs_path, error=str(exc))
            class_rows = []

        for row in class_rows:
            nid = row.get("nid") or ""
            if not nid:
                continue
            _set_property(db, "Class_", nid, changed_recently=True, changed_in_commit=current_hash)
            changed_classes += 1

            # Collect features linked to this class
            try:
                feature_rows: list[dict[str, Any]] = db.execute(
                    "MATCH (c:Class_ {node_id: $nid})-[:BELONGS_TO_FEATURE]->(f:Feature) "
                    "RETURN f.node_id AS fnid",
                    {"nid": nid},
                )
                for frow in feature_rows:
                    fnid = frow.get("fnid") or ""
                    if fnid:
                        affected_feature_nids.add(fnid)
            except Exception:
                pass  # best-effort

    # ── Step 6: annotate affected Feature nodes ────────────────────────────
    for fnid in affected_feature_nids:
        _set_property(db, "Feature", fnid, has_changes=True)

    # ── Step 7: persist current hash ──────────────────────────────────────
    try:
        idx_dir.mkdir(parents=True, exist_ok=True)
        last_commit_file.write_text(current_hash)
    except OSError as exc:
        logger.warning("Could not write last_commit.txt", error=str(exc))

    ctx.stats["changed_files"] = changed_files
    ctx.stats["changed_methods"] = changed_methods
    ctx.stats["changed_classes"] = changed_classes

    logger.info(
        "Change intelligence complete",
        changed_files=changed_files,
        changed_methods=changed_methods,
        changed_classes=changed_classes,
        affected_features=len(affected_feature_nids),
        current_commit=current_hash[:12],
    )
