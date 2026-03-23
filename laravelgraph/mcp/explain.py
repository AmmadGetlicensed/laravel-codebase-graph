"""Graph traversal helpers for laravelgraph_explain and laravelgraph_overview.

Each function appends to a `lines: list[str]` passed in by the caller.
All queries are parameterised and individually wrapped in try/except so
a missing edge type or empty table never crashes the tool.

Option 1 — Source injection: key methods (controller actions, listener
  handle() methods) are read from disk and included as fenced PHP code
  blocks so the AI agent has actual code to reason from, not just names.

Option 2 — Docblock enrichment: full PHPDoc description (not just the
  first sentence) is cleaned of @tags and shown before the source snippet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from laravelgraph.logging import get_logger

if TYPE_CHECKING:
    from laravelgraph.core.graph import GraphDB

logger = get_logger(__name__)

# Maximum lines to include per source snippet — keeps context manageable
# Large enough for most methods; summary is generated separately for longer ones
_MAX_SNIPPET_LINES = 120


# ── Source reading (Option 1) ─────────────────────────────────────────────────

def read_source_snippet(
    file_path: str,
    line_start: int,
    line_end: int,
    project_root: Path | None = None,
) -> str:
    """Read the PHP source lines for a method and return them as a string.

    Returns empty string on any failure (missing file, bad line numbers, etc.)
    so callers never have to worry about error handling.
    """
    if not file_path or line_start < 1:
        return ""

    path = Path(file_path)
    if project_root and not path.is_absolute():
        path = project_root / path

    try:
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    if not all_lines:
        return ""

    start_idx = max(0, line_start - 1)
    # Cap at _MAX_SNIPPET_LINES so we don't flood the agent context
    end_idx = min(len(all_lines), line_end, line_start - 1 + _MAX_SNIPPET_LINES)
    snippet = all_lines[start_idx:end_idx]

    # If there were more lines, add a truncation note
    actual_end = line_end if line_end <= len(all_lines) else len(all_lines)
    if actual_end > line_start - 1 + _MAX_SNIPPET_LINES:
        snippet.append(f"    // ... ({actual_end - (line_start - 1 + _MAX_SNIPPET_LINES)} more lines)")

    return "\n".join(snippet)


# ── Docblock cleaning (Option 2) ──────────────────────────────────────────────

def clean_docblock(raw: str) -> str:
    """Return the prose description from a PHPDoc block.

    Strips /**  */ delimiters, leading * characters, and all @tag lines
    (@param, @return, @throws, etc.). Joins remaining non-empty lines into
    a paragraph suitable for display.
    """
    if not raw:
        return ""

    description_lines: list[str] = []
    for line in raw.splitlines():
        # Strip /** */ and leading * markers
        stripped = line.strip().lstrip("/").lstrip("*").strip()
        # Stop collecting when we hit @tags
        if stripped.startswith("@"):
            break
        if stripped:
            description_lines.append(stripped)

    return " ".join(description_lines)


def _append_source_block(
    file_path: str,
    line_start: int,
    line_end: int,
    project_root: Path | None,
    lines: list[str],
    label: str = "",
) -> None:
    """Read source and append a fenced PHP code block to lines."""
    snippet = read_source_snippet(file_path, line_start, line_end, project_root)
    if not snippet:
        return
    rel = Path(file_path).name  # just the filename for brevity in the header
    header = f"**Source** (`{rel}:{line_start}-{line_end}`)"
    if label:
        header = f"**`{label}` source** (`{rel}:{line_start}-{line_end}`)"
    lines.append(header + ":")
    lines.append("```php")
    lines.append(snippet)
    lines.append("```")


# ── Entry-point discovery ─────────────────────────────────────────────────────

def find_routes_for_feature(db: "GraphDB", terms: list[str]) -> list[dict]:
    """Return routes whose URI, name, or controller FQN contains any search term."""
    try:
        rows = db.execute(
            "MATCH (r:Route) RETURN r.node_id AS nid, r.http_method AS hm, r.uri AS uri, "
            "r.name AS rname, r.controller_fqn AS ctrl, r.action_method AS action, "
            "r.middleware_stack AS mw LIMIT 300"
        )
    except Exception:
        return []

    matched = []
    for row in rows:
        haystack = " ".join(filter(None, [
            row.get("uri") or "",
            row.get("rname") or "",
            row.get("ctrl") or "",
            row.get("action") or "",
        ])).lower()
        if any(t in haystack for t in terms):
            matched.append(row)
    return matched


def find_commands_for_feature(db: "GraphDB", terms: list[str]) -> list[dict]:
    """Return commands whose signature or description contains any search term."""
    try:
        rows = db.execute(
            "MATCH (c:Command) RETURN c.node_id AS nid, c.name AS name, "
            "c.signature AS sig, c.description AS desc LIMIT 100"
        )
    except Exception:
        return []

    matched = []
    for row in rows:
        haystack = " ".join(filter(None, [
            row.get("sig") or "",
            row.get("desc") or "",
            row.get("name") or "",
        ])).lower()
        if any(t in haystack for t in terms):
            matched.append(row)
    return matched


# ── Controller method flow tracing ────────────────────────────────────────────

def trace_method_flow(
    db: "GraphDB",
    ctrl_fqn: str,
    action: str,
    lines: list[str],
    project_root: Path | None = None,
) -> None:
    """Find a controller method and trace what it does into `lines`.

    Includes:
      - Cleaned full docblock description (Option 2)
      - Actual PHP source snippet (Option 1)
      - Injected dependencies, form request validation, calls, dispatches,
        rendered views, and API resource transforms
    """
    try:
        method_rows = db.execute(
            "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
            "WHERE c.fqn = $cfqn AND m.name = $mname "
            "RETURN m.node_id AS nid, m.docblock AS doc, m.return_type AS rt, "
            "m.file_path AS fp, m.line_start AS ls, m.line_end AS le, "
            "c.node_id AS class_nid",
            {"cfqn": ctrl_fqn, "mname": action},
        )
    except Exception:
        return

    if not method_rows:
        return

    method     = method_rows[0]
    method_nid = method.get("nid") or ""
    class_nid  = method.get("class_nid") or ""
    file_path  = method.get("fp") or ""
    line_start = method.get("ls") or 0
    line_end   = method.get("le") or 0

    # Option 2: full cleaned docblock as Purpose
    raw_doc = method.get("doc") or ""
    description = clean_docblock(raw_doc)
    if description:
        lines.append(f"**Purpose:** {description}")

    # Option 1: actual PHP source
    if file_path and line_start:
        _append_source_block(file_path, line_start, line_end, project_root, lines)

    _append_class_injections(db, class_nid, lines)
    _append_validates(db, method_nid, lines)
    _append_method_injections(db, method_nid, lines)
    _append_calls(db, method_nid, lines)
    _append_dispatches(db, method_nid, lines, project_root=project_root)
    _append_renders(db, method_nid, lines)
    _append_transforms(db, method_nid, lines)


def _append_class_injections(db: "GraphDB", class_nid: str, lines: list[str]) -> None:
    if not class_nid:
        return
    try:
        rows = db.execute(
            "MATCH (c:Class_)-[:INJECTS]->(dep) WHERE c.node_id = $nid "
            "RETURN dep.name AS name LIMIT 8",
            {"nid": class_nid},
        )
        if rows:
            names = [r.get("name") or "?" for r in rows]
            lines.append(f"**Injects:** {', '.join(f'`{n}`' for n in names)}")
    except Exception:
        pass


def _append_validates(db: "GraphDB", method_nid: str, lines: list[str]) -> None:
    if not method_nid:
        return
    try:
        rows = db.execute(
            "MATCH (m:Method)-[:VALIDATES_WITH]->(fr:FormRequest) WHERE m.node_id = $nid "
            "RETURN fr.name AS name, fr.rules_summary AS rules",
            {"nid": method_nid},
        )
        for r in rows:
            name = r.get("name") or "?"
            rules_str = ""
            raw = r.get("rules") or ""
            if raw:
                try:
                    keys = json.loads(raw)
                    if isinstance(keys, list) and keys:
                        rules_str = f" (fields: {', '.join(str(k) for k in keys[:6])})"
                except Exception:
                    pass
            lines.append(f"**Validates with:** `{name}`{rules_str}")
    except Exception:
        pass


def _append_method_injections(db: "GraphDB", method_nid: str, lines: list[str]) -> None:
    if not method_nid:
        return
    try:
        rows = db.execute(
            "MATCH (m:Method)-[:INJECTS]->(dep) WHERE m.node_id = $nid "
            "RETURN dep.name AS name LIMIT 5",
            {"nid": method_nid},
        )
        if rows:
            names = [r.get("name") or "?" for r in rows]
            lines.append(f"**Method params:** {', '.join(f'`{n}`' for n in names)}")
    except Exception:
        pass


def _append_calls(db: "GraphDB", method_nid: str, lines: list[str]) -> None:
    if not method_nid:
        return
    try:
        rows = db.execute(
            "MATCH (m:Method)-[c:CALLS]->(target) WHERE m.node_id = $nid "
            "RETURN target.name AS name, target.fqn AS fqn, c.confidence AS conf "
            "ORDER BY c.confidence DESC LIMIT 8",
            {"nid": method_nid},
        )
        if rows:
            call_strs = [f"`{_short_fqn(r.get('fqn') or r.get('name') or '?')}`" for r in rows]
            lines.append(f"**Calls:** {', '.join(call_strs)}")
    except Exception:
        pass


def _append_dispatches(
    db: "GraphDB",
    method_nid: str,
    lines: list[str],
    project_root: Path | None = None,
) -> None:
    if not method_nid:
        return
    try:
        rows = db.execute(
            "MATCH (m:Method)-[d:DISPATCHES]->(t) WHERE m.node_id = $nid "
            "RETURN t.name AS name, t.fqn AS fqn, d.dispatch_type AS dtype, d.is_queued AS queued",
            {"nid": method_nid},
        )
        for r in rows:
            dtype  = r.get("dtype") or "event"
            queued = r.get("queued") or False
            name   = r.get("name") or "?"
            fqn    = r.get("fqn") or ""
            q_str  = " *(queued)*" if queued else ""
            lines.append(f"**Dispatches {dtype}:** `{name}`{q_str}")
            if dtype == "event" and fqn:
                _append_listeners(db, fqn, lines, project_root=project_root)
    except Exception:
        pass


def _append_listeners(
    db: "GraphDB",
    event_fqn: str,
    lines: list[str],
    project_root: Path | None = None,
) -> None:
    if not event_fqn:
        return
    try:
        rows = db.execute(
            "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.fqn = $fqn "
            "RETURN l.name AS name, l.fqn AS lfqn, l.is_queued AS queued, l.queue AS queue",
            {"fqn": event_fqn},
        )
        for r in rows:
            name   = r.get("name") or "?"
            lfqn   = r.get("lfqn") or ""
            queued = r.get("queued") or False
            queue  = r.get("queue") or ""
            suffix = f" *(queue: {queue})*" if queued and queue else " *(queued)*" if queued else ""
            lines.append(f"  → Listener: `{name}`{suffix}")

            # Option 2: docblock for the handle() method
            # Option 1: source of the handle() method
            if lfqn:
                _append_listener_handle(db, lfqn, lines, project_root=project_root)
    except Exception:
        pass


def _append_listener_handle(
    db: "GraphDB",
    listener_fqn: str,
    lines: list[str],
    project_root: Path | None = None,
) -> None:
    """Show the listener's handle() method docblock + source."""
    if not listener_fqn:
        return
    try:
        rows = db.execute(
            "MATCH (c:Class_)-[:DEFINES]->(m:Method) "
            "WHERE c.fqn = $fqn AND m.name = 'handle' "
            "RETURN m.docblock AS doc, m.file_path AS fp, "
            "m.line_start AS ls, m.line_end AS le",
            {"fqn": listener_fqn},
        )
    except Exception:
        return

    if not rows:
        return

    row        = rows[0]
    raw_doc    = row.get("doc") or ""
    file_path  = row.get("fp") or ""
    line_start = row.get("ls") or 0
    line_end   = row.get("le") or 0

    # Option 2: docblock description
    description = clean_docblock(raw_doc)
    if description:
        lines.append(f"    **What it does:** {description}")

    # Option 1: source snippet
    if file_path and line_start:
        snippet = read_source_snippet(file_path, line_start, line_end, project_root)
        if snippet:
            rel = Path(file_path).name
            lines.append(f"    **`handle()` source** (`{rel}:{line_start}`):")
            lines.append("    ```php")
            for src_line in snippet.splitlines():
                lines.append(f"    {src_line}")
            lines.append("    ```")


def _append_renders(db: "GraphDB", method_nid: str, lines: list[str]) -> None:
    if not method_nid:
        return
    try:
        rows = db.execute(
            "MATCH (m:Method)-[:RENDERS_TEMPLATE]->(t:BladeTemplate) WHERE m.node_id = $nid "
            "RETURN t.name AS name",
            {"nid": method_nid},
        )
        if rows:
            names = [r.get("name") or "?" for r in rows]
            lines.append(f"**Renders view:** {', '.join(f'`{n}`' for n in names)}")
    except Exception:
        pass


def _append_transforms(db: "GraphDB", method_nid: str, lines: list[str]) -> None:
    if not method_nid:
        return
    try:
        rows = db.execute(
            "MATCH (m:Method)-[:TRANSFORMS_WITH]->(r:Resource) WHERE m.node_id = $nid "
            "RETURN r.name AS name",
            {"nid": method_nid},
        )
        if rows:
            names = [r.get("name") or "?" for r in rows]
            lines.append(f"**Returns resource:** {', '.join(f'`{n}`' for n in names)}")
    except Exception:
        pass


# ── Event chain ───────────────────────────────────────────────────────────────

def trace_event_chain(
    db: "GraphDB",
    event_nid: str,
    event_name: str,
    lines: list[str],
    project_root: Path | None = None,
) -> None:
    """Append: dispatchers → event → listeners (with handle() source)."""
    lines.append(f"\n#### Event: `{event_name}`")

    try:
        dispatchers = db.execute(
            "MATCH (m)-[:DISPATCHES]->(e) WHERE e.node_id = $nid RETURN m.fqn AS fqn LIMIT 5",
            {"nid": event_nid},
        )
        if dispatchers:
            names = [f"`{_short_fqn(r.get('fqn') or '?')}`" for r in dispatchers]
            lines.append(f"**Dispatched by:** {', '.join(names)}")
    except Exception:
        pass

    # Get event's own docblock / file info
    try:
        event_rows = db.execute(
            "MATCH (e:Event) WHERE e.node_id = $nid "
            "RETURN e.fqn AS fqn",
            {"nid": event_nid},
        )
        event_fqn = (event_rows[0].get("fqn") or "") if event_rows else ""
    except Exception:
        event_fqn = ""

    try:
        listeners = db.execute(
            "MATCH (l:Listener)-[:LISTENS_TO]->(e) WHERE e.node_id = $nid "
            "RETURN l.name AS name, l.fqn AS lfqn, l.is_queued AS queued, l.queue AS queue",
            {"nid": event_nid},
        )
        for r in listeners:
            name   = r.get("name") or "?"
            lfqn   = r.get("lfqn") or ""
            queued = r.get("queued") or False
            queue  = r.get("queue") or ""
            suffix = f" *(queue: {queue})*" if queued and queue else " *(queued)*" if queued else ""
            lines.append(f"→ Listener: `{name}`{suffix}")
            if lfqn:
                _append_listener_handle(db, lfqn, lines, project_root=project_root)
    except Exception:
        pass


# ── Model summary ─────────────────────────────────────────────────────────────

def trace_model_summary(db: "GraphDB", model_nid: str, model_name: str, lines: list[str]) -> None:
    """Append model table + Eloquent relationship map."""
    lines.append(f"\n#### Model: `{model_name}`")

    try:
        rows = db.execute(
            "MATCH (m:EloquentModel) WHERE m.node_id = $nid "
            "RETURN m.db_table AS tbl, m.soft_deletes AS soft, m.fillable AS fillable",
            {"nid": model_nid},
        )
        if rows:
            tbl      = rows[0].get("tbl") or ""
            soft     = rows[0].get("soft") or False
            fillable = rows[0].get("fillable") or ""
            if tbl:
                lines.append(f"**Table:** `{tbl}`{'  *(soft deletes)*' if soft else ''}")
            if fillable:
                try:
                    fields = json.loads(fillable)
                    if isinstance(fields, list) and fields:
                        lines.append(f"**Fillable fields:** {', '.join(f'`{f}`' for f in fields[:10])}")
                except Exception:
                    pass
    except Exception:
        pass

    try:
        rels = db.execute(
            "MATCH (m:EloquentModel)-[r:HAS_RELATIONSHIP]->(rel:EloquentModel) WHERE m.node_id = $nid "
            "RETURN r.relationship_type AS rtype, r.method_name AS method, rel.name AS rname LIMIT 10",
            {"nid": model_nid},
        )
        for r in rels:
            lines.append(
                f"→ `{r.get('method') or '?'}()` {r.get('rtype') or '?'} → `{r.get('rname') or '?'}`"
            )
    except Exception:
        pass


# ── Utility ───────────────────────────────────────────────────────────────────

def _short_fqn(fqn: str) -> str:
    """ClassName::method or ClassName from a fully-qualified name."""
    if not fqn:
        return "?"
    if "::" in fqn:
        cls, meth = fqn.rsplit("::", 1)
        return f"{cls.split(chr(92))[-1]}::{meth}"
    return fqn.split("\\")[-1]
