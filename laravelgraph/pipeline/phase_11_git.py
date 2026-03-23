"""Phase 11 — Change Coupling Analysis (Git History).

Analyze git history for change coupling between files. Files that are
frequently modified together in the same commit are likely coupled and
this relationship is surfaced as a COUPLED_WITH edge.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Binary file extensions to skip
_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".ttf", ".woff", ".woff2", ".eot", ".otf",
    ".pdf", ".zip", ".tar", ".gz", ".rar",
    ".mp4", ".mp3", ".avi", ".mov",
    ".exe", ".dll", ".so", ".dylib",
    ".lock",  # composer.lock changes are noise
})


def _is_binary(path_str: str) -> bool:
    suffix = Path(path_str).suffix.lower()
    return suffix in _BINARY_EXTENSIONS


def run(ctx: PipelineContext) -> None:
    """Analyze git history and create COUPLED_WITH relationships."""
    try:
        from git import InvalidGitRepositoryError, Repo
    except ImportError:
        logger.warning("gitpython not installed; skipping change coupling analysis")
        return

    project_root = str(ctx.project_root)
    history_months = getattr(ctx.config.pipeline, "git_history_months", 6)
    coupling_threshold = getattr(ctx.config.pipeline, "change_coupling_threshold", 0.3)
    db = ctx.db

    # Open the repo
    try:
        repo = Repo(project_root)
    except InvalidGitRepositoryError:
        logger.info("Not a git repository; skipping change coupling analysis", path=project_root)
        return
    except Exception as exc:
        logger.warning("Failed to open git repo", path=project_root, error=str(exc))
        return

    # Determine cutoff date
    cutoff = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(days=30 * history_months)

    # co_changes[file_a][file_b] = count of commits where both changed
    co_changes: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    # total_changes[file] = count of commits where this file changed
    total_changes: defaultdict[str, int] = defaultdict(int)
    commits_analyzed = 0

    logger.info(
        "Analyzing git history",
        months=history_months,
        cutoff=cutoff.isoformat(),
    )

    try:
        for commit in repo.iter_commits(rev=repo.head.commit, max_count=5000):
            try:
                # Skip commits older than cutoff
                commit_dt = datetime.datetime.fromtimestamp(
                    commit.committed_date, tz=datetime.timezone.utc
                )
                if commit_dt < cutoff:
                    break

                # Get changed files in this commit
                if not commit.parents:
                    # Initial commit — diff against empty tree
                    changed: list[str] = [item.a_path for item in commit.diff(None)]
                else:
                    parent = commit.parents[0]
                    changed = [item.a_path for item in parent.diff(commit)]

                # Filter out binary/irrelevant files
                changed_filtered = [f for f in changed if not _is_binary(f) and f]

                if len(changed_filtered) < 2:
                    commits_analyzed += 1
                    continue

                # Update total change counts
                for f in changed_filtered:
                    total_changes[f] += 1

                # Update co-change matrix for all pairs
                for fa, fb in combinations(sorted(changed_filtered), 2):
                    co_changes[fa][fb] += 1

                commits_analyzed += 1

            except Exception as exc:
                logger.debug("Error processing commit", sha=str(commit)[:8], error=str(exc))
                continue

    except Exception as exc:
        logger.warning("Error iterating git commits", error=str(exc))

    logger.info(
        "Git history traversal complete",
        commits_analyzed=commits_analyzed,
        unique_files=len(total_changes),
    )

    # Compute coupling strength and create COUPLED_WITH relationships
    coupled_pairs = 0

    # Build a map from relative file path → File node_id
    file_nid_map: dict[str, str] = {}
    try:
        rows = db.execute("MATCH (f:File) RETURN f.node_id AS nid, f.relative_path AS rp")
        for row in rows:
            rp = row.get("rp") or ""
            nid = row.get("nid") or ""
            if rp and nid:
                file_nid_map[rp] = nid
    except Exception as exc:
        logger.warning("Failed to load File nodes for coupling", error=str(exc))

    for file_a, co_map in co_changes.items():
        for file_b, co_count in co_map.items():
            try:
                max_total = max(
                    total_changes.get(file_a, 1),
                    total_changes.get(file_b, 1),
                )
                strength = co_count / max_total if max_total > 0 else 0.0

                if strength < coupling_threshold:
                    continue

                nid_a = file_nid_map.get(file_a)
                nid_b = file_nid_map.get(file_b)

                if not nid_a or not nid_b:
                    continue

                db.upsert_rel(
                    "COUPLED_WITH",
                    "File",
                    nid_a,
                    "File",
                    nid_b,
                    {
                        "strength": round(strength, 4),
                        "co_changes": co_count,
                        "period_months": history_months,
                    },
                )
                coupled_pairs += 1

            except Exception as exc:
                logger.debug(
                    "Failed to create COUPLED_WITH",
                    file_a=file_a,
                    file_b=file_b,
                    error=str(exc),
                )

    ctx.stats["coupled_pairs"] = coupled_pairs
    ctx.stats["commits_analyzed"] = commits_analyzed

    logger.info(
        "Change coupling analysis complete",
        commits_analyzed=commits_analyzed,
        coupled_pairs=coupled_pairs,
        threshold=coupling_threshold,
    )
