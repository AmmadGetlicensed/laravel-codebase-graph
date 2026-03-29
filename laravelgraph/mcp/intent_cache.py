"""Lazy structured intent cache for LaravelGraph.

Intent entries are stored in .laravelgraph/intent.json alongside the graph DB.
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


class IntentCache:
    """File-backed cache for AI-generated structured intent analysis of PHP symbols.

    Lifecycle:
        - First intent call for a symbol: generates structured intent via LLM API,
          stores in cache, returns result immediately.
        - Subsequent calls for same symbol: returns cached intent instantly (no API call,
          no source code shipped).
        - Source file modified: mtime check auto-invalidates stale entries on next access.
        - File re-indexed (watch mode): call invalidate_file() to purge intents for
          all symbols in that file.
    """

    def __init__(self, index_dir: Path) -> None:
        self._path = index_dir / "intent.json"
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Could not load intent cache", error=str(e))
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save intent cache", error=str(e))

    def get(self, node_id: str, file_path: str = "") -> dict | None:
        """Return cached intent dict or None if missing/stale.

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
                    logger.debug("Intent auto-invalidated (file changed)", node_id=node_id)
                    return None
            except OSError:
                pass  # File gone — keep the stale intent rather than crash

        # Return the structured intent fields, excluding internal metadata
        return {
            k: entry[k]
            for k in ("purpose", "reads", "writes", "side_effects", "guards")
            if k in entry
        } or None

    def set(
        self,
        node_id: str,
        intent: dict,
        model: str,
        file_path: str = "",
    ) -> None:
        """Store a generated intent analysis for a symbol."""
        mtime = 0.0
        if file_path:
            try:
                mtime = os.path.getmtime(file_path)
            except OSError:
                pass

        self._data[node_id] = {
            "purpose": intent.get("purpose", ""),
            "reads": intent.get("reads", []),
            "writes": intent.get("writes", []),
            "side_effects": intent.get("side_effects", []),
            "guards": intent.get("guards", []),
            "model": model,
            "generated_at": time.time(),
            "file_path": file_path,
            "file_mtime": mtime,
        }
        self._save()
        logger.debug("Intent cached", node_id=node_id, model=model)

    def invalidate_file(self, file_path: str) -> int:
        """Remove all cached intents for symbols defined in the given file.

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
                "Invalidated intents on file change",
                file=file_path,
                count=len(to_remove),
            )
        return len(to_remove)

    def stats(self) -> dict:
        return {
            "cached_intents": len(self._data),
            "models_used": sorted({e.get("model", "unknown") for e in self._data.values()}),
        }
