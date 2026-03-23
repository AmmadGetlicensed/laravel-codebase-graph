"""Phase 02 — Structure.

Build the filesystem hierarchy in the graph: Folder nodes and File nodes,
linked by CONTAINS relationships from parent folders to children.
"""

from __future__ import annotations

from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


def _count_lines(path: Path) -> int:
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return 0


def run(ctx: PipelineContext) -> None:
    """Create Folder and File nodes for every discovered file."""
    with phase_timer("Structure"):
        project_root = ctx.project_root
        db = ctx.db

        # Collect the set of all directories that contain at least one file
        folder_set: set[Path] = set()
        for filepath in ctx.all_files:
            for ancestor in filepath.parents:
                if ancestor == project_root or project_root in ancestor.parents or ancestor == project_root.parent:
                    if ancestor == project_root:
                        break
                    folder_set.add(ancestor)
                else:
                    break
            # Always include the direct parent and project root
            folder_set.add(filepath.parent)
        folder_set.add(project_root)

        # ── Folder nodes ────────────────────────────────────────────────────
        folders_created = 0
        for folder in sorted(folder_set):
            try:
                rel = folder.relative_to(project_root)
                rel_str = rel.as_posix()
            except ValueError:
                rel_str = str(folder)

            nid = make_node_id("folder", rel_str)
            try:
                db._insert_node("Folder", {
                    "node_id": nid,
                    "path": str(folder),
                    "name": folder.name or project_root.name,
                    "relative_path": rel_str,
                })
                folders_created += 1
            except Exception as e:
                logger.debug("Folder node insert failed (may already exist)", path=rel_str, error=str(e))

        ctx.stats["folders_created"] = folders_created

        # ── CONTAINS: parent folder → child folder ───────────────────────
        for folder in sorted(folder_set):
            parent = folder.parent
            if parent == folder:
                continue
            if parent not in folder_set and parent != project_root.parent:
                continue

            try:
                child_rel = folder.relative_to(project_root).as_posix()
            except ValueError:
                continue

            try:
                parent_rel = parent.relative_to(project_root).as_posix()
            except ValueError:
                continue

            parent_nid = make_node_id("folder", parent_rel)
            child_nid = make_node_id("folder", child_rel)

            try:
                db.upsert_rel("CONTAINS", "Folder", parent_nid, "Folder", child_nid)
            except Exception as e:
                logger.debug(
                    "Folder→Folder CONTAINS failed",
                    parent=parent_rel,
                    child=child_rel,
                    error=str(e),
                )

        # ── File nodes + CONTAINS: parent folder → file ──────────────────
        file_roles: dict[str, str] = getattr(ctx, "_file_roles", {})
        files_created = 0

        for filepath in ctx.all_files:
            try:
                rel = filepath.relative_to(project_root)
                rel_str = rel.as_posix()
            except ValueError:
                rel_str = str(filepath)

            role = file_roles.get(str(filepath), "php")
            name = filepath.name
            ext = filepath.suffix
            try:
                size_bytes = filepath.stat().st_size
            except OSError:
                size_bytes = 0
            lines = _count_lines(filepath)

            file_nid = make_node_id("file", rel_str)

            try:
                db._insert_node("File", {
                    "node_id": file_nid,
                    "path": str(filepath),
                    "relative_path": rel_str,
                    "name": name,
                    "extension": ext,
                    "size_bytes": size_bytes,
                    "laravel_role": role,
                    "php_namespace": "",
                    "lines": lines,
                })
                files_created += 1
            except Exception as e:
                logger.debug("File node insert failed", path=rel_str, error=str(e))
                continue

            # CONTAINS: parent folder → this file
            parent = filepath.parent
            try:
                parent_rel = parent.relative_to(project_root).as_posix()
            except ValueError:
                parent_rel = str(parent)

            parent_nid = make_node_id("folder", parent_rel)
            try:
                db.upsert_rel("CONTAINS", "Folder", parent_nid, "File", file_nid)
            except Exception as e:
                logger.debug(
                    "Folder→File CONTAINS failed",
                    folder=parent_rel,
                    file=rel_str,
                    error=str(e),
                )

        ctx.stats["files_created"] = files_created
        logger.info(
            "Structure built",
            folders=folders_created,
            files=files_created,
        )
