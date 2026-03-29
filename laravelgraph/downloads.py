"""Downloadable dependency manager for LaravelGraph.

Tracks all assets that must be downloaded before analysis (embedding models,
tree-sitter grammars, etc.) and provides check / download utilities.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Dependency:
    name: str                          # display name
    key: str                           # slug key
    description: str                   # one-line description
    size_hint: str                     # human-readable size, e.g. "85 MB"
    check: Callable[[], bool]          # returns True if already available
    download: Callable[[Any], None]    # callable(progress_callback: Callable[[int,int], None])
    tags: list[str] = field(default_factory=list)  # e.g. ["embeddings", "optional"]


# ── Dependency 1: fastembed embedding model ────────────────────────────────────

def _check_fastembed() -> bool:
    """Return True if the BAAI/bge-small-en-v1.5 model is already cached."""
    import tempfile

    # fastembed defaults to <tempdir>/fastembed_cache; respects FASTEMBED_CACHE_PATH
    cache_root_str = os.environ.get("FASTEMBED_CACHE_PATH", "")
    if cache_root_str:
        cache_root = Path(cache_root_str)
    else:
        cache_root = Path(tempfile.gettempdir()) / "fastembed_cache"

    if not cache_root.exists():
        return False

    # Model directory may be named with the HF repo slug, e.g.
    # "models--qdrant--bge-small-en-v1.5-onnx-q" or just "bge-small-en-v1.5".
    # Search broadly: any subdir whose name contains "bge-small" with an .onnx inside.
    for entry in cache_root.iterdir():
        if not entry.is_dir():
            continue
        if "bge-small" not in entry.name.lower():
            continue
        onnx_files = list(entry.rglob("*.onnx"))
        if onnx_files:
            return True

    return False


def _download_fastembed(progress_callback: Callable[[int, int], None] | None = None) -> None:
    """Download the BAAI/bge-small-en-v1.5 fastembed model."""
    try:
        from fastembed import TextEmbedding  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "fastembed is not installed. Run: pip install fastembed"
        ) from exc

    # Instantiating TextEmbedding triggers the download and caches the model.
    # fastembed does not expose a streaming progress callback, so we just block.
    TextEmbedding(model_name="BAAI/bge-small-en-v1.5")


_dep_fastembed = Dependency(
    name="Embedding Model (BAAI/bge-small-en-v1.5)",
    key="fastembed-bge-small",
    description=(
        "Sentence transformer for semantic search — enables laravelgraph_explain "
        "and laravelgraph_query vector search"
    ),
    size_hint="~85 MB",
    check=_check_fastembed,
    download=_download_fastembed,
    tags=["embeddings"],
)


# ── Dependency 2: PHP Tree-sitter grammar ─────────────────────────────────────

def _check_tree_sitter_php() -> bool:
    """Return True if tree_sitter_php is importable."""
    try:
        import tree_sitter_php  # type: ignore[import]
        # Confirm the module exposes a usable language attribute
        return hasattr(tree_sitter_php, "language_php") or hasattr(tree_sitter_php, "language") or hasattr(tree_sitter_php, "LANGUAGE")
    except ImportError:
        return False


def _download_tree_sitter_php(progress_callback: Callable[[int, int], None] | None = None) -> None:
    """Instruct the user to install tree-sitter-php via pip."""
    raise RuntimeError(
        "tree-sitter-php is a pip package and must be installed manually.\n"
        "Run: pip install tree-sitter-php\n"
        "Or with pipx: pipx inject laravelgraph tree-sitter-php"
    )


_dep_tree_sitter_php = Dependency(
    name="PHP Tree-sitter Grammar",
    key="tree-sitter-php",
    description="PHP parser grammar — enables accurate AST parsing of PHP source files",
    size_hint="~2 MB",
    check=_check_tree_sitter_php,
    download=_download_tree_sitter_php,
    tags=["parsing"],
)


# ── Registry ──────────────────────────────────────────────────────────────────

DEPENDENCIES: list[Dependency] = [
    _dep_fastembed,
    _dep_tree_sitter_php,
]


def check_all() -> dict[str, bool]:
    """Return {key: is_available} for all dependencies."""
    return {dep.key: dep.check() for dep in DEPENDENCIES}


def download_missing(console: Any, progress: Any) -> tuple[int, int]:
    """Download all missing dependencies.

    Parameters
    ----------
    console:
        A ``rich.console.Console`` instance used for printing status messages.
    progress:
        A ``rich.progress.Progress`` instance (already started) used for task
        display.  Pass ``None`` to skip progress display.

    Returns
    -------
    tuple[int, int]
        ``(downloaded, failed)`` counts.
    """
    downloaded = 0
    failed = 0

    for dep in DEPENDENCIES:
        if dep.check():
            continue  # already available

        task = None
        if progress is not None:
            task = progress.add_task(
                f"[1/1] Downloading {dep.name}...",
                total=None,  # indeterminate
            )

        try:
            dep.download(None)
            downloaded += 1
            if progress is not None and task is not None:
                progress.update(task, description=f"[green]✓[/green] {dep.name}", completed=1, total=1)
        except Exception as exc:
            failed += 1
            if progress is not None and task is not None:
                progress.update(task, description=f"[red]✗[/red] {dep.name}", completed=1, total=1)
            if console is not None:
                console.print(f"  [red]Failed to download {dep.name}:[/red] {exc}")

    return downloaded, failed
