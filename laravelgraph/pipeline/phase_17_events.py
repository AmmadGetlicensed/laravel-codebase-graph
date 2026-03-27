"""Phase 17 — Event/Listener/Job Dispatch Graph.

Parse EventServiceProvider and build the Event → Listener → Job dispatch graph.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Regex to extract the $listen array from EventServiceProvider
_LISTEN_ARRAY_RE = re.compile(
    r'\$listen\s*=\s*\[(.+?)\];',
    re.DOTALL,
)

# Match one event => [listeners] block inside $listen
_EVENT_BLOCK_RE = re.compile(
    r'([\w\\]+)::class\s*=>\s*\[(.*?)\]',
    re.DOTALL,
)

# Match individual listener class references
_CLASS_REF_RE = re.compile(r'([\w\\]+)::class')

# Dispatch patterns inside handle() methods
_DISPATCH_NEW_RE = re.compile(r'dispatch\s*\(\s*new\s+([\w\\]+)\s*\(')
_DISPATCH_STATIC_RE = re.compile(r'([\w\\]+)::dispatch\s*\(')
_NOTIFICATION_SEND_RE = re.compile(r'Notification::send\s*\(.*?,\s*new\s+([\w\\]+)\s*\(')
_SHOULD_QUEUE_RE = re.compile(r'implements\s+.*?ShouldQueue')


def _short_name(fqn: str) -> str:
    """Return the unqualified class name from a FQN."""
    return fqn.split("\\")[-1]


def _resolve_class(class_name: str, fqn_index: dict[str, str], use_stmts: list[str]) -> str:
    """Try to resolve a short or partial class name to a fully-qualified name."""
    if "\\" in class_name:
        return class_name
    # Check use statements
    for use in use_stmts:
        if use.endswith(f"\\{class_name}") or use == class_name:
            return use
    # Fall back to whatever was written
    return class_name


def _parse_listen_array(source: str) -> dict[str, list[str]]:
    """Parse the $listen array and return {event_class: [listener_classes]}."""
    result: dict[str, list[str]] = {}
    m = _LISTEN_ARRAY_RE.search(source)
    if not m:
        return result

    body = m.group(1)
    for block in _EVENT_BLOCK_RE.finditer(body):
        event_class = block.group(1)
        listeners_body = block.group(2)
        listeners = _CLASS_REF_RE.findall(listeners_body)
        result[event_class] = listeners

    return result


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _find_listener_file(listener_name: str, ctx: PipelineContext) -> Path | None:
    """Locate the file for a listener class by short name."""
    short = _short_name(listener_name)
    for path in ctx.php_files:
        if path.name == f"{short}.php":
            return path
    return None


def _parse_handle_method(source: str) -> dict[str, Any]:
    """Extract dispatch and notification calls from a listener's handle() method."""
    dispatched_jobs: list[str] = []
    notified: list[str] = []

    for m in _DISPATCH_NEW_RE.finditer(source):
        dispatched_jobs.append(m.group(1))
    for m in _DISPATCH_STATIC_RE.finditer(source):
        dispatched_jobs.append(m.group(1))
    for m in _NOTIFICATION_SEND_RE.finditer(source):
        notified.append(m.group(1))

    is_queued = bool(_SHOULD_QUEUE_RE.search(source))

    return {
        "dispatched_jobs": dispatched_jobs,
        "notified": notified,
        "is_queued": is_queued,
    }


def run(ctx: PipelineContext) -> None:
    """Parse EventServiceProvider and build Event → Listener → Job dispatch graph."""
    db = ctx.db
    events_mapped = 0
    listeners_mapped = 0

    # Locate EventServiceProvider
    esp_path = ctx.project_root / "app" / "Providers" / "EventServiceProvider.php"
    if not esp_path.exists():
        # Try to find it among php_files
        for f in ctx.php_files:
            if f.name == "EventServiceProvider.php":
                esp_path = f
                break

    if not esp_path.exists():
        logger.info("EventServiceProvider.php not found; skipping event/listener graph")
        ctx.stats["events_mapped"] = 0
        ctx.stats["listeners_mapped"] = 0
        # Still run dispatch detection — it scans ALL method bodies for dispatch patterns
        # and does not depend on EventServiceProvider.
        from laravelgraph.pipeline.phase_05_calls import run_dispatch_pass
        run_dispatch_pass(ctx)
        return

    source = _read_source(esp_path)
    listen_map = _parse_listen_array(source)
    logger.info("Parsed EventServiceProvider", path=str(esp_path), events=len(listen_map))
    if not listen_map:
        logger.warning(
            "EventServiceProvider found but $listen array is empty or could not be parsed. "
            "The project may use Event::listen() in boot(), auto-discovery, or a non-standard pattern.",
            path=str(esp_path),
        )

    for event_class, listener_classes in listen_map.items():
        # Create or update Event node
        event_nid = make_node_id("event", event_class)
        event_short = _short_name(event_class)

        # Find event file path if available
        event_file = ""
        for p in ctx.php_files:
            if p.name == f"{event_short}.php":
                event_file = str(p)
                break

        try:
            db._insert_node("Event", {
                "node_id": event_nid,
                "name": event_short,
                "fqn": event_class,
                "file_path": event_file,
                "broadcastable": False,
                "broadcast_channel": "",
            })
            events_mapped += 1
        except Exception as exc:
            logger.debug("Event node insert failed (may exist)", nid=event_nid, error=str(exc))

        for listener_class in listener_classes:
            listener_nid = make_node_id("listener", listener_class)
            listener_short = _short_name(listener_class)

            # Find listener file
            listener_path = _find_listener_file(listener_class, ctx)
            listener_file = str(listener_path) if listener_path else ""
            listener_source = _read_source(listener_path) if listener_path else ""
            handle_info = _parse_handle_method(listener_source) if listener_source else {
                "dispatched_jobs": [], "notified": [], "is_queued": False
            }

            # Determine queue name from listener source
            queue_match = re.search(r'public\s+\$queue\s*=\s*[\'"]([^\'"]+)[\'"]', listener_source)
            queue_name = queue_match.group(1) if queue_match else ""

            try:
                db._insert_node("Listener", {
                    "node_id": listener_nid,
                    "name": listener_short,
                    "fqn": listener_class,
                    "file_path": listener_file,
                    "is_queued": handle_info["is_queued"],
                    "queue": queue_name,
                })
                listeners_mapped += 1
            except Exception as exc:
                logger.debug("Listener node insert failed (may exist)", nid=listener_nid, error=str(exc))

            # LISTENS_TO: Listener → Event
            try:
                db.upsert_rel("LISTENS_TO", "Listener", listener_nid, "Event", event_nid)
            except Exception as exc:
                logger.debug("LISTENS_TO rel failed", listener=listener_nid, event=event_nid, error=str(exc))

            # DISPATCHES: Listener → Job
            for job_class in handle_info["dispatched_jobs"]:
                job_short = _short_name(job_class)
                job_nid = make_node_id("job", job_class)

                # Check if a Job node already exists; if not create a stub
                job_file = ""
                for p in ctx.php_files:
                    if p.name == f"{job_short}.php":
                        job_file = str(p)
                        break

                try:
                    db._insert_node("Job", {
                        "node_id": job_nid,
                        "name": job_short,
                        "fqn": job_class,
                        "file_path": job_file,
                        "queue": "",
                        "connection": "",
                        "tries": 0,
                        "timeout": 0,
                        "is_queued": True,
                    })
                except Exception:
                    pass  # May already exist from a prior phase

                try:
                    db.upsert_rel(
                        "DISPATCHES",
                        "Listener",
                        listener_nid,
                        "Job",
                        job_nid,
                        {"dispatch_type": "job", "is_queued": True, "line": 0},
                    )
                except Exception as exc:
                    logger.debug("DISPATCHES rel failed", listener=listener_nid, job=job_nid, error=str(exc))

            # NOTIFIES: Listener → Notification
            for notif_class in handle_info["notified"]:
                notif_short = _short_name(notif_class)
                notif_nid = make_node_id("notification", notif_class)

                notif_file = ""
                for p in ctx.php_files:
                    if p.name == f"{notif_short}.php":
                        notif_file = str(p)
                        break

                try:
                    db._insert_node("Notification", {
                        "node_id": notif_nid,
                        "name": notif_short,
                        "fqn": notif_class,
                        "file_path": notif_file,
                        "channels": "[]",
                    })
                except Exception:
                    pass

                try:
                    db.upsert_rel(
                        "NOTIFIES",
                        "Listener",
                        listener_nid,
                        "Notification",
                        notif_nid,
                        {"channels": ""},
                    )
                except Exception as exc:
                    logger.debug("NOTIFIES rel failed", listener=listener_nid, notif=notif_nid, error=str(exc))

    ctx.stats["events_mapped"] = events_mapped
    ctx.stats["listeners_mapped"] = listeners_mapped
    logger.info(
        "Event/Listener graph built",
        events=events_mapped,
        listeners=listeners_mapped,
    )

    # Run the dispatch detection pass now that Event/Job nodes exist.
    # This must happen AFTER Event/Job nodes are created (hence here, not in phase_05).
    from laravelgraph.pipeline.phase_05_calls import run_dispatch_pass
    run_dispatch_pass(ctx)
