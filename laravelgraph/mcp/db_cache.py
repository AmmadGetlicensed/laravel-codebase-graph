"""Lazy database context cache for LaravelGraph.

DB context entries (table semantics, column resolutions, procedure annotations)
are generated on first MCP tool call and cached here. Invalidation is
hash-based — the cache key includes a SHA-1 of the table's column structure
so any schema change automatically busts the cache.

Stored alongside the summary cache at:
  .laravelgraph/db_context.json
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from laravelgraph.logging import get_logger

logger = get_logger(__name__)


class DBContextCache:
    """File-backed cache for AI-generated semantic annotations of DB objects.

    Keys:
        dbctx:table:{connection}:{table_name}
        dbctx:column:{connection}:{table_name}.{column_name}
        dbctx:proc:{connection}:{proc_name}

    Each entry stores:
        annotation   — the LLM-generated text
        schema_hash  — SHA-1 of the serialised column structure at generation time
        model        — LLM model used
        generated_at — Unix timestamp
    """

    def __init__(self, index_dir: Path) -> None:
        self._path = index_dir / "db_context.json"
        self._data: dict[str, dict] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Could not load DB context cache", error=str(e))
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save DB context cache", error=str(e))

    # ── Hash helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def schema_hash(columns: list[dict]) -> str:
        """Stable SHA-1 of a column list — used to detect schema changes."""
        payload = json.dumps(
            sorted(columns, key=lambda c: c.get("name", "")),
            sort_keys=True,
        ).encode()
        return hashlib.sha1(payload).hexdigest()[:12]

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, cache_key: str, current_hash: str = "") -> str | None:
        """Return cached annotation or None if missing/stale.

        If current_hash is provided the entry is invalidated when it no longer
        matches the stored schema_hash (i.e. the table structure changed).
        """
        entry = self._data.get(cache_key)
        if not entry:
            return None

        stored_hash = entry.get("schema_hash", "")
        if current_hash and stored_hash and stored_hash != current_hash:
            del self._data[cache_key]
            self._save()
            logger.debug("DB context auto-invalidated (schema changed)", key=cache_key)
            return None

        return entry.get("annotation") or None

    # ── Write ─────────────────────────────────────────────────────────────────

    def set(
        self,
        cache_key: str,
        annotation: str,
        model: str,
        schema_hash: str = "",
    ) -> None:
        """Store a generated annotation."""
        self._data[cache_key] = {
            "annotation": annotation,
            "model": model,
            "generated_at": time.time(),
            "schema_hash": schema_hash,
        }
        self._save()
        logger.debug("DB context cached", key=cache_key, model=model)

    # ── Bulk invalidation ─────────────────────────────────────────────────────

    def invalidate_connection(self, connection: str) -> int:
        """Remove all cached entries for a given database connection.

        Called when connection config changes or `--full` rebuild is run.
        """
        prefix = f"dbctx:table:{connection}:"
        proc_prefix = f"dbctx:proc:{connection}:"
        col_prefix = f"dbctx:column:{connection}:"
        to_remove = [
            k for k in self._data
            if k.startswith(prefix) or k.startswith(proc_prefix) or k.startswith(col_prefix)
        ]
        for k in to_remove:
            del self._data[k]
        if to_remove:
            self._save()
            logger.info("Invalidated DB context cache", connection=connection, count=len(to_remove))
        return len(to_remove)

    def stats(self) -> dict:
        types: dict[str, int] = {}
        for k in self._data:
            prefix = k.split(":")[1] if ":" in k else "unknown"
            types[prefix] = types.get(prefix, 0) + 1
        return {
            "cached_entries": len(self._data),
            "by_type": types,
            "models_used": sorted({e.get("model", "unknown") for e in self._data.values()}),
        }
