"""Phase 01 — File Discovery.

Walk the Laravel project root, respect .gitignore, classify every PHP/Blade
file by its Laravel role, and populate the discovery lists on ctx.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Directories that are always skipped regardless of .gitignore
_ALWAYS_SKIP = {
    "vendor",
    "storage",
    "node_modules",
    ".git",
    ".laravelgraph",
}

# Top-level paths that are skipped (these may have trailing slashes in .gitignore)
_ALWAYS_SKIP_PREFIXES = (
    "bootstrap/cache",
    "public",
)


def _load_gitignore_spec(project_root: Path) -> Any:
    """Return a pathspec.PathSpec for the project's .gitignore, or None."""
    try:
        import pathspec  # type: ignore[import]
    except ImportError:
        logger.warning("pathspec not installed; .gitignore will not be respected")
        return None

    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        return None

    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)
    except Exception as e:
        logger.warning("Failed to parse .gitignore", error=str(e))
        return None


def _should_skip_dir(
    dirpath: Path,
    project_root: Path,
    gitignore_spec: Any,
) -> bool:
    """Return True if the directory should be skipped entirely."""
    name = dirpath.name
    if name in _ALWAYS_SKIP:
        return True

    try:
        rel = dirpath.relative_to(project_root)
    except ValueError:
        return False

    rel_str = rel.as_posix()

    for prefix in _ALWAYS_SKIP_PREFIXES:
        if rel_str == prefix or rel_str.startswith(prefix + "/"):
            return True

    if gitignore_spec is not None:
        # pathspec expects a path with trailing slash to match directories
        if gitignore_spec.match_file(rel_str + "/") or gitignore_spec.match_file(rel_str):
            return True

    return False


def _classify_php_role(path: Path, project_root: Path) -> str:
    """Determine the Laravel role for a PHP (or Blade) file based on its path."""
    try:
        rel_str = path.relative_to(project_root).as_posix()
    except ValueError:
        rel_str = str(path)

    # Blade files checked first
    if rel_str.endswith(".blade.php") and rel_str.startswith("resources/views/"):
        return "blade"

    # Explicit directory-based roles
    if rel_str.startswith("app/Models/"):
        return "model"
    if rel_str.startswith("app/Http/Controllers/"):
        return "controller"
    if rel_str.startswith("app/Http/Middleware/"):
        return "middleware"
    if rel_str.startswith("app/Jobs/"):
        return "job"
    if rel_str.startswith("app/Events/"):
        return "event"
    if rel_str.startswith("app/Listeners/"):
        return "listener"
    if rel_str.startswith("app/Policies/"):
        return "policy"
    if rel_str.startswith("app/Http/Requests/"):
        return "request"
    if rel_str.startswith("app/Http/Resources/"):
        return "resource"
    if rel_str.startswith("app/Notifications/"):
        return "notification"
    if rel_str.startswith("app/Observers/"):
        return "observer"
    if rel_str.startswith("app/Console/Commands/"):
        return "command"
    if rel_str.startswith("app/Providers/"):
        return "provider"
    if rel_str.startswith("database/factories/"):
        return "factory"
    if rel_str.startswith("database/seeders/"):
        return "seeder"
    if rel_str.startswith("database/migrations/"):
        return "migration"
    if rel_str.startswith("routes/"):
        return "route_file"

    # Content-based heuristic for files outside conventional directories:
    # Only peek if the file is small enough that reading is cheap.
    if path.suffix == ".php":
        try:
            stat = path.stat()
            if stat.st_size < 64 * 1024:  # 64 KB
                source = path.read_text(encoding="utf-8", errors="replace")
                if "extends Model" in source or "extends Authenticatable" in source:
                    return "model"
        except OSError:
            pass

    return "php"


def run(ctx: PipelineContext) -> None:
    """Discover all project files and populate ctx file lists."""
    with phase_timer("File Discovery"):
        project_root = ctx.project_root
        gitignore_spec = _load_gitignore_spec(project_root)
        blade_ext = ctx.config.pipeline.blade_extension  # ".blade.php"
        php_exts = set(ctx.config.pipeline.php_extensions)  # {".php"}
        max_size_bytes = ctx.config.pipeline.max_file_size_kb * 1024

        php_files: list[Path] = []
        blade_files: list[Path] = []
        route_files: list[Path] = []
        migration_files: list[Path] = []
        all_files: list[Path] = []

        for dirpath_str, dirnames, filenames in os.walk(project_root):
            dirpath = Path(dirpath_str)

            # Prune dirnames in-place so os.walk won't descend into skipped dirs
            dirnames[:] = [
                d for d in dirnames
                if not _should_skip_dir(dirpath / d, project_root, gitignore_spec)
            ]
            dirnames.sort()  # deterministic traversal

            for filename in sorted(filenames):
                filepath = dirpath / filename

                # Respect .gitignore for files too
                try:
                    rel_file = filepath.relative_to(project_root)
                except ValueError:
                    continue

                rel_str = rel_file.as_posix()

                if gitignore_spec is not None and gitignore_spec.match_file(rel_str):
                    continue

                # Size guard
                try:
                    size = filepath.stat().st_size
                except OSError:
                    continue

                if size > max_size_bytes:
                    logger.debug(
                        "Skipping oversized file",
                        path=rel_str,
                        size_kb=size // 1024,
                    )
                    continue

                suffix = filepath.suffix
                name = filepath.name

                is_blade = name.endswith(blade_ext)
                is_php = suffix in php_exts

                if not is_php and not is_blade:
                    continue

                role = _classify_php_role(filepath, project_root)
                # Attach role as an attribute so phase 02 can read it without re-classifying
                # We store it in a side-table on ctx to avoid mutating Path objects.
                if not hasattr(ctx, "_file_roles"):
                    ctx._file_roles = {}  # type: ignore[attr-defined]
                ctx._file_roles[str(filepath)] = role  # type: ignore[attr-defined]

                all_files.append(filepath)

                if is_blade or role == "blade":
                    blade_files.append(filepath)
                elif role == "route_file":
                    route_files.append(filepath)
                    php_files.append(filepath)
                elif role == "migration":
                    migration_files.append(filepath)
                    php_files.append(filepath)
                else:
                    php_files.append(filepath)

        ctx.php_files = php_files
        ctx.blade_files = blade_files
        ctx.route_files = route_files
        ctx.migration_files = migration_files
        ctx.all_files = all_files

        ctx.stats["files_discovered"] = len(all_files)
        ctx.stats["php_files"] = len(php_files)
        ctx.stats["blade_files"] = len(blade_files)

        logger.info(
            "Discovery complete",
            total=len(all_files),
            php=len(php_files),
            blade=len(blade_files),
            routes=len(route_files),
            migrations=len(migration_files),
        )
