"""TTL-based cache for live database query results.

Query results (SELECT, SHOW, DESCRIBE, EXPLAIN) are cached here to avoid
hammering the live database when an AI agent calls the same query multiple
times within a session.

Invalidation is TTL-based — entries expire after ``ttl_seconds`` (default 300).
Unlike the DB context cache (hash-based, permanent until schema changes), query
results are ephemeral: data changes between sessions so a short TTL is correct.

Stored alongside the other caches at:
  .laravelgraph/query_cache.json
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from laravelgraph.logging import get_logger

logger = get_logger(__name__)

# ── SQL safety ────────────────────────────────────────────────────────────────

_ALLOWED_RE = re.compile(
    r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN)\b",
    re.IGNORECASE,
)
_DANGEROUS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE"
    r"|CALL|EXEC|EXECUTE|REPLACE|RENAME|LOCK|UNLOCK|LOAD|OUTFILE|DUMPFILE)\b",
    re.IGNORECASE,
)

_MAX_ROWS = 500  # hard ceiling regardless of what the caller requests


def validate_sql(sql: str) -> str | None:
    """Return an error string if the SQL is not allowed, or None if safe.

    Only SELECT, SHOW, DESCRIBE/DESC, and EXPLAIN are permitted.
    SHOW statements are always safe meta-commands (SHOW CREATE TABLE, SHOW
    TABLES, etc.) so dangerous-keyword scanning is skipped for them.
    For all other allowed statements, any write/DDL keyword causes rejection.
    """
    sql = sql.strip()
    if not sql:
        return "SQL query cannot be empty."
    if not _ALLOWED_RE.match(sql):
        return (
            "Only SELECT, SHOW, DESCRIBE, and EXPLAIN queries are allowed. "
            "This tool is read-only."
        )
    # SHOW is always a read-only meta command — skip dangerous keyword check.
    if re.match(r"^\s*SHOW\b", sql, re.IGNORECASE):
        return None
    if _DANGEROUS_RE.search(sql):
        return "Query contains a disallowed keyword (write or DDL operation)."
    return None


# ── Cache class ───────────────────────────────────────────────────────────────

class QueryResultCache:
    """File-backed TTL cache for live DB query results.

    Keys:
        qcache:{connection}:{sha1(normalized_sql)}

    Each entry stores:
        sql         — original SQL text
        columns     — ordered list of column names
        rows        — list of row dicts  {col: value}
        row_count   — int
        cached_at   — Unix timestamp
        ttl_seconds — int (from connection config or default 300)
        connection  — connection name
    """

    DEFAULT_TTL = 300  # 5 minutes

    def __init__(self, index_dir: Path) -> None:
        self._path = index_dir / "query_cache.json"
        self._data: dict[str, dict] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Could not load query cache", error=str(e))
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not save query cache", error=str(e))

    # ── Key helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def make_key(connection: str, sql: str) -> str:
        """Deterministic cache key — normalise whitespace so spacing differences
        on the same logical query share the same cache slot."""
        normalised = " ".join(sql.split()).upper()
        digest = hashlib.sha1(normalised.encode()).hexdigest()[:16]
        return f"qcache:{connection}:{digest}"

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self, cache_key: str, ttl: int | None = None) -> dict | None:
        """Return a cached entry if it exists and has not expired.

        Args:
            cache_key: Key from ``make_key()``.
            ttl:       Override TTL in seconds. None = use the stored TTL.

        Returns:
            The full entry dict (includes ``columns``, ``rows``, ``row_count``,
            ``cached_at``, ``connection``) or None if missing/expired.
        """
        entry = self._data.get(cache_key)
        if not entry:
            return None

        effective_ttl = ttl if ttl is not None else entry.get("ttl_seconds", self.DEFAULT_TTL)
        age = time.time() - entry.get("cached_at", 0)
        if age > effective_ttl:
            del self._data[cache_key]
            self._save()
            logger.debug("Query cache expired", key=cache_key, age_sec=round(age))
            return None

        return entry

    # ── Write ─────────────────────────────────────────────────────────────────

    def set(
        self,
        cache_key: str,
        sql: str,
        connection: str,
        columns: list[str],
        rows: list[dict],
        ttl: int = DEFAULT_TTL,
    ) -> None:
        """Store a query result."""
        self._data[cache_key] = {
            "sql": sql,
            "connection": connection,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "cached_at": time.time(),
            "ttl_seconds": ttl,
        }
        self._save()
        logger.debug("Query result cached", key=cache_key, rows=len(rows), ttl=ttl)

    # ── Invalidation ──────────────────────────────────────────────────────────

    def invalidate_connection(self, connection: str) -> int:
        """Remove all cached results for a given connection.

        Called automatically when connection config changes or ``--full``
        rebuild is run.
        """
        prefix = f"qcache:{connection}:"
        to_remove = [k for k in self._data if k.startswith(prefix)]
        for k in to_remove:
            del self._data[k]
        if to_remove:
            self._save()
            logger.info("Query cache invalidated", connection=connection, count=len(to_remove))
        return len(to_remove)

    def clear_all(self) -> int:
        """Remove every cached entry."""
        count = len(self._data)
        self._data = {}
        self._save()
        logger.info("Query cache cleared", count=count)
        return count

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns number of entries removed."""
        now = time.time()
        to_remove = [
            k for k, v in self._data.items()
            if now - v.get("cached_at", 0) > v.get("ttl_seconds", self.DEFAULT_TTL)
        ]
        for k in to_remove:
            del self._data[k]
        if to_remove:
            self._save()
        return len(to_remove)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        now = time.time()
        live = sum(
            1 for v in self._data.values()
            if now - v.get("cached_at", 0) <= v.get("ttl_seconds", self.DEFAULT_TTL)
        )
        by_conn: dict[str, int] = {}
        for v in self._data.values():
            conn = v.get("connection", "unknown")
            by_conn[conn] = by_conn.get(conn, 0) + 1
        return {
            "cached_entries": len(self._data),
            "live_entries": live,
            "expired_entries": len(self._data) - live,
            "by_connection": by_conn,
        }
