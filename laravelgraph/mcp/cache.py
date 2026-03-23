"""Lazy semantic summary cache for LaravelGraph.

Summaries are stored in .laravelgraph/summaries.json alongside the graph DB.
Automatically invalidated when the source file's mtime changes — no manual
cache busting required.

No graph schema changes needed: this is a sidecar cache that works with any
existing indexed database.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from laravelgraph.logging import get_logger

logger = get_logger(__name__)


class SummaryCache:
    """File-backed cache for AI-generated semantic summaries of PHP symbols.

    Lifecycle:
        - First explain/context call for a symbol: generates summary via Claude API,
          stores in cache, returns Options 1+2 result immediately.
        - Subsequent calls for same symbol: returns cached summary instantly (no API call,
          no source code shipped).
        - Source file modified: mtime check auto-invalidates stale entries on next access.
        - File re-indexed (watch mode): call invalidate_file() to purge summaries for
          all symbols in that file.
    """

    def __init__(self, index_dir: Path) -> None:
        self._path = index_dir / "summaries.json"
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Could not load summary cache", error=str(e))
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save summary cache", error=str(e))

    def get(self, node_id: str, file_path: str = "") -> str | None:
        """Return cached summary or None if missing/stale.

        If file_path is provided, checks the file's mtime against the stored mtime.
        Returns None (and removes the entry) if the file has been modified.
        """
        entry = self._data.get(node_id)
        if not entry:
            return None

        if file_path:
            try:
                current_mtime = os.path.getmtime(file_path)
                stored_mtime = entry.get("file_mtime", 0.0)
                # 1-second tolerance for filesystem mtime precision
                if current_mtime > stored_mtime + 1.0:
                    del self._data[node_id]
                    self._save()
                    logger.debug("Summary auto-invalidated (file changed)", node_id=node_id)
                    return None
            except OSError:
                pass  # File gone — keep the stale summary rather than crash

        return entry.get("summary") or None

    def set(
        self,
        node_id: str,
        summary: str,
        model: str,
        file_path: str = "",
    ) -> None:
        """Store a generated summary for a symbol."""
        mtime = 0.0
        if file_path:
            try:
                mtime = os.path.getmtime(file_path)
            except OSError:
                pass

        self._data[node_id] = {
            "summary": summary,
            "model": model,
            "generated_at": time.time(),
            "file_path": file_path,
            "file_mtime": mtime,
        }
        self._save()
        logger.debug("Summary cached", node_id=node_id, model=model)

    def invalidate_file(self, file_path: str) -> int:
        """Remove all cached summaries for symbols defined in the given file.

        Called by watch mode when a file is re-indexed.
        Returns the number of entries removed.
        """
        to_remove = [
            nid for nid, entry in self._data.items()
            if entry.get("file_path") == file_path
        ]
        for nid in to_remove:
            del self._data[nid]
        if to_remove:
            self._save()
            logger.info(
                "Invalidated summaries on file change",
                file=file_path,
                count=len(to_remove),
            )
        return len(to_remove)

    def stats(self) -> dict:
        return {
            "cached_summaries": len(self._data),
            "models_used": sorted({e.get("model", "unknown") for e in self._data.values()}),
        }
