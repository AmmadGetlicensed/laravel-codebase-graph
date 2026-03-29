"""Log management utilities for LaravelGraph.

Reads, filters, and manages structured log files from ~/.laravelgraph/logs/.
Logs are written as JSONL (one JSON object per line) by structlog.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


class LogManager:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def get_log_files(self) -> list[Path]:
        """Return all .log and .jsonl files sorted by modification time (newest first)."""
        files: list[Path] = []
        for ext in ("*.log", "*.jsonl"):
            files.extend(self.log_dir.glob(ext))
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        return files

    def get_recent(
        self,
        limit: int = 100,
        level: str = "",
        tool: str = "",
        plugin: str = "",
        since_hours: float = 0,
        search: str = "",
    ) -> list[dict]:
        """Read log files newest-first, return filtered entries as list of dicts."""
        since_ts: float = 0.0
        if since_hours > 0:
            since_ts = time.time() - since_hours * 3600.0

        entries: list[dict] = []
        for log_file in self.get_log_files():
            if len(entries) >= limit:
                break
            try:
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            # Read newest lines first
            for line in reversed(lines):
                if len(entries) >= limit:
                    break
                entry = self._parse_line(line)
                if entry is None:
                    continue
                if self._matches_filters(entry, level, tool, plugin, since_ts, search):
                    entries.append(entry)

        return entries

    def tail(
        self,
        callback: Callable[[dict], None],
        level: str = "",
        tool: str = "",
        plugin: str = "",
        poll_interval: float = 0.5,
    ) -> None:
        """Tail the most recent log file. Calls callback for each new matching line.
        Blocks until KeyboardInterrupt.
        """
        log_files = self.get_log_files()
        if not log_files:
            return

        target = log_files[0]
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                # Seek to end
                fh.seek(0, 2)
                while True:
                    line = fh.readline()
                    if line:
                        entry = self._parse_line(line)
                        if entry is not None and self._matches_filters(
                            entry, level, tool, plugin, since_ts=0.0, search=""
                        ):
                            callback(entry)
                    else:
                        time.sleep(poll_interval)
        except KeyboardInterrupt:
            pass

    def get_stats(self) -> dict:
        """Return stats: total_entries, by_level dict, by_tool dict (top 10),
        disk_size_mb, file_count, oldest_entry, newest_entry.
        """
        log_files = self.get_log_files()
        total_entries = 0
        by_level: Counter[str] = Counter()
        by_tool: Counter[str] = Counter()
        oldest_ts: float | None = None
        newest_ts: float | None = None
        disk_bytes = 0

        for log_file in log_files:
            try:
                disk_bytes += log_file.stat().st_size
            except OSError:
                pass
            try:
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                entry = self._parse_line(line)
                if entry is None:
                    continue
                total_entries += 1
                lvl = str(entry.get("level", "")).lower()
                if lvl:
                    by_level[lvl] += 1
                tool_name = str(entry.get("tool", ""))
                if tool_name:
                    by_tool[tool_name] += 1

                # Parse timestamp for oldest/newest
                ts_str = entry.get("timestamp", "") or entry.get("ts", "") or entry.get("time", "")
                if ts_str:
                    try:
                        ts = _parse_iso_timestamp(str(ts_str))
                        if oldest_ts is None or ts < oldest_ts:
                            oldest_ts = ts
                        if newest_ts is None or ts > newest_ts:
                            newest_ts = ts
                    except Exception:
                        pass

        top_tools = dict(by_tool.most_common(10))

        return {
            "total_entries": total_entries,
            "by_level": dict(by_level),
            "by_tool": top_tools,
            "disk_size_mb": round(disk_bytes / (1024 * 1024), 3),
            "file_count": len(log_files),
            "oldest_entry": datetime.fromtimestamp(oldest_ts, tz=timezone.utc).isoformat() if oldest_ts else None,
            "newest_entry": datetime.fromtimestamp(newest_ts, tz=timezone.utc).isoformat() if newest_ts else None,
        }

    def clear_old(self, days: int = 30) -> int:
        """Delete log files older than N days. Returns count of files deleted."""
        cutoff = time.time() - days * 86400.0
        deleted = 0
        for log_file in self.get_log_files():
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
                    deleted += 1
            except OSError:
                pass
        return deleted

    def clear_all(self) -> int:
        """Delete ALL log files. Returns count deleted."""
        deleted = 0
        for log_file in self.get_log_files():
            try:
                log_file.unlink()
                deleted += 1
            except OSError:
                pass
        return deleted

    def get_domain_query_frequencies(
        self,
        since_hours: int = 168,
        min_calls: int = 3,
    ) -> list[dict]:
        """Scan MCP logs for frequently-queried domains that may need plugins.

        Looks for ``laravelgraph_feature_context`` and ``laravelgraph_explain``
        tool calls and extracts domain tokens from the ``feature`` parameter.
        Returns domains with call count >= *min_calls*, sorted by count descending.

        Each returned dict has: slug, count, last_seen (ISO string or "").
        """
        import re as _re
        since_ts: float = 0.0
        if since_hours > 0:
            since_ts = time.time() - since_hours * 3600.0

        counts: Counter[str] = Counter()
        last_seen: dict[str, str] = {}
        _TARGET_TOOLS = {"laravelgraph_feature_context", "laravelgraph_explain"}

        def _to_slug(text: str) -> str:
            """Normalize a domain description to a slug for deduplication."""
            text = text.lower().strip()
            text = _re.sub(r"[^a-z0-9]+", "-", text)
            return text.strip("-")[:50]

        for log_file in self.get_log_files():
            try:
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for line in lines:
                entry = self._parse_line(line)
                if entry is None:
                    continue

                # Apply since_ts filter
                if since_ts > 0:
                    ts_str = (
                        entry.get("timestamp", "")
                        or entry.get("ts", "")
                        or entry.get("time", "")
                    )
                    if ts_str:
                        try:
                            if _parse_iso_timestamp(str(ts_str)) < since_ts:
                                continue
                        except Exception:
                            pass

                tool_name = str(entry.get("tool", ""))
                if tool_name not in _TARGET_TOOLS:
                    continue

                # Extract feature param — may live under "params" or top-level
                feature: str = ""
                params = entry.get("params", {})
                if isinstance(params, dict):
                    feature = str(params.get("feature", ""))
                if not feature:
                    feature = str(entry.get("feature", ""))
                if not feature:
                    continue

                slug = _to_slug(feature)
                if not slug:
                    continue

                counts[slug] += 1
                ts_str = (
                    entry.get("timestamp", "")
                    or entry.get("ts", "")
                    or entry.get("time", "")
                    or ""
                )
                last_seen[slug] = str(ts_str)

        results = [
            {"slug": slug, "count": cnt, "last_seen": last_seen.get(slug, "")}
            for slug, cnt in counts.most_common()
            if cnt >= min_calls
        ]
        return results

    def _parse_line(self, line: str) -> dict | None:
        """Parse a log line. Return dict or None if not parseable."""
        line = line.strip()
        if not line:
            return None
        # Try JSON first
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        # Non-JSON lines (e.g. startup banners) are skipped — logs are always JSONL
        return None

    def _matches_filters(
        self,
        entry: dict,
        level: str,
        tool: str,
        plugin: str,
        since_ts: float,
        search: str,
    ) -> bool:
        """Check if a log entry matches all active filters."""
        # Level filter
        if level:
            entry_level = str(entry.get("level", "")).lower()
            if entry_level != level.lower():
                return False

        # Tool filter
        if tool:
            entry_tool = str(entry.get("tool", ""))
            if tool.lower() not in entry_tool.lower():
                return False

        # Plugin filter
        if plugin:
            entry_plugin = str(entry.get("plugin", ""))
            if not entry_plugin:
                params = entry.get("params", {})
                if isinstance(params, dict):
                    entry_plugin = str(params.get("plugin", ""))
            if plugin.lower() not in entry_plugin.lower():
                return False

        # Since timestamp filter
        if since_ts > 0:
            ts_str = entry.get("timestamp", "") or entry.get("ts", "") or entry.get("time", "")
            if ts_str:
                try:
                    ts = _parse_iso_timestamp(str(ts_str))
                    if ts < since_ts:
                        return False
                except Exception:
                    pass

        # Text search filter
        if search:
            if search.lower() not in str(entry).lower():
                return False

        return True


def _parse_iso_timestamp(ts_str: str) -> float:
    """Parse an ISO 8601 timestamp string to a Unix float. Best-effort."""
    # structlog emits e.g. "2024-01-15T12:34:56.789Z" or with offset
    ts_str = ts_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(ts_str).timestamp()
    except ValueError:
        pass
    # Try truncated forms
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts_str[:len(fmt) + 2], fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {ts_str!r}")


def format_log_entry(entry: dict, color: bool = True) -> str:
    """Format a log entry for CLI display.

    Format: TIMESTAMP [LEVEL] message  key=value key=value
    Colors: error=red, warning=yellow, info=cyan, debug=dim
    """
    RESET = "\033[0m" if color else ""
    COLORS = {
        "error": "\033[31m",       # red
        "warning": "\033[33m",     # yellow
        "info": "\033[36m",        # cyan
        "debug": "\033[2m",        # dim
    }

    ts = entry.get("timestamp") or entry.get("ts") or entry.get("time") or ""
    if ts and len(str(ts)) > 19:
        ts = str(ts)[:19]

    level = str(entry.get("level", "info")).lower()
    message = str(entry.get("message") or entry.get("event") or entry.get("msg") or "")

    color_code = COLORS.get(level, "") if color else ""

    # Extra key=value pairs (skip known meta fields)
    skip_keys = {"timestamp", "ts", "time", "level", "logger", "message", "event", "msg"}
    extras = " ".join(
        f"{k}={v!r}" if " " in str(v) else f"{k}={v}"
        for k, v in entry.items()
        if k not in skip_keys and v is not None and v != ""
    )

    level_tag = f"[{level.upper()}]"
    parts = [ts, f"{color_code}{level_tag}{RESET}", message]
    if extras:
        parts.append(f"  {extras}")

    return " ".join(p for p in parts if p)


def format_log_table(entries: list[dict]) -> "Table":
    """Return a rich Table of log entries with columns: Time, Level, Message, Details."""
    from rich.table import Table

    LEVEL_STYLES = {
        "error": "bold red",
        "warning": "yellow",
        "info": "cyan",
        "debug": "dim",
    }

    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("Time", style="dim", width=20, no_wrap=True)
    table.add_column("Level", width=9, no_wrap=True)
    table.add_column("Message", ratio=2)
    table.add_column("Details", ratio=3)

    for entry in entries:
        ts = str(entry.get("timestamp") or entry.get("ts") or entry.get("time") or "")[:19]
        level = str(entry.get("level", "info")).lower()
        message = str(entry.get("message") or entry.get("event") or entry.get("msg") or "")
        style = LEVEL_STYLES.get(level, "")

        skip_keys = {"timestamp", "ts", "time", "level", "logger", "message", "event", "msg"}
        details = "  ".join(
            f"{k}={v}" for k, v in entry.items()
            if k not in skip_keys and v is not None and v != ""
        )

        table.add_row(ts, f"[{style}]{level.upper()}[/{style}]" if style else level.upper(), message, details)

    return table


def get_domain_query_frequencies(
    log_dir: Path,
    since_hours: int = 168,
    min_calls: int = 3,
) -> list[dict]:
    """Module-level convenience wrapper around LogManager.get_domain_query_frequencies.

    Args:
        log_dir:     Directory containing JSONL log files.
        since_hours: Look back this many hours (default: 168 = 7 days).
        min_calls:   Minimum call count to include a domain (default: 3).

    Returns list of {"slug": str, "count": int, "last_seen": str}.
    """
    return LogManager(log_dir).get_domain_query_frequencies(
        since_hours=since_hours,
        min_calls=min_calls,
    )
