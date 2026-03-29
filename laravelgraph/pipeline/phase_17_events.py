"""Phase 17 — Event/Listener/Job Dispatch Graph.

Parse EventServiceProvider and build the Event → Listener → Job dispatch graph.

Detection strategies (in order of reliability):
  1. EventServiceProvider $listen array           — classic Laravel ≤10
  2. Event::listen() calls in ServiceProvider boot() — Laravel 11+ / any provider
  3. PHP #[Listen] attribute on listener class    — Laravel 11+ attributes
  4. Type-hint auto-discovery on handle() method  — Laravel 11+ auto-discovery
  5. Wildcard / closure listeners in boot()       — partial (logs warning)
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

# ── Regex patterns ─────────────────────────────────────────────────────────────

# $listen array in EventServiceProvider
_LISTEN_ARRAY_RE = re.compile(r'\$listen\s*=\s*\[(.+?)\];', re.DOTALL)
_EVENT_BLOCK_RE = re.compile(r'([\w\\]+)::class\s*=>\s*\[(.*?)\]', re.DOTALL)
_CLASS_REF_RE = re.compile(r'([\w\\]+)::class')

# Event::listen(EventClass::class, ListenerClass::class) — any ServiceProvider boot()
_EVENT_LISTEN_RE = re.compile(
    r'Event::listen\s*\(\s*([\w\\]+)::class\s*,\s*([\w\\]+)::class',
    re.DOTALL,
)
# Event::listen(EventClass::class, [ListenerClass::class, 'method'])
_EVENT_LISTEN_ARRAY_RE = re.compile(
    r'Event::listen\s*\(\s*([\w\\]+)::class\s*,\s*\[\s*([\w\\]+)::class',
    re.DOTALL,
)
# $events->listen(...) variant inside subscriber subscribe() methods
_EVENTS_LISTEN_RE = re.compile(
    r'\$events\s*->\s*listen\s*\(\s*([\w\\]+)::class\s*,\s*\[?\s*(?:static::class|self::class|[\w\\]+::class)',
    re.DOTALL,
)

# PHP #[Listen(EventClass::class)] attribute
_LISTEN_ATTR_RE = re.compile(r'#\[Listen\s*\(\s*([\w\\]+)::class\s*\)\]')

# Dispatch patterns inside handle() methods
_DISPATCH_NEW_RE = re.compile(r'dispatch\s*\(\s*new\s+([\w\\]+)\s*\(')
_DISPATCH_STATIC_RE = re.compile(r'([\w\\]+)::dispatch\s*\(')
_NOTIFICATION_SEND_RE = re.compile(r'Notification::send\s*\(.*?,\s*new\s+([\w\\]+)\s*\(')
_SHOULD_QUEUE_RE = re.compile(r'implements\s+.*?ShouldQueue')


# ── Helpers ────────────────────────────────────────────────────────────────────

def _short_name(fqn: str) -> str:
    return fqn.split("\\")[-1]


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _build_use_map(file_path: Path, ctx: PipelineContext) -> dict[str, str]:
    """Return {short_name: fqn} from the file's `use` statements."""
    key = str(file_path)
    php_file = ctx.parsed_php.get(key)
    if php_file is None:
        return {}
    result: dict[str, str] = {}
    for u in php_file.uses:
        alias = u.alias or _short_name(u.fqn)
        result[alias] = u.fqn
    return result


def _resolve_class(name: str, use_map: dict[str, str], namespace: str = "") -> str:
    """Resolve a short/partial class name to a fully-qualified name."""
    if name.startswith("\\"):
        return name.lstrip("\\")
    if "\\" in name:
        return name
    if name in use_map:
        return use_map[name]
    if namespace:
        return f"{namespace}\\{name}"
    return name


def _find_listener_file(listener_name: str, ctx: PipelineContext) -> Path | None:
    short = _short_name(listener_name)
    for path in ctx.php_files:
        if path.name == f"{short}.php":
            return path
    return None


def _find_event_file(event_name: str, ctx: PipelineContext) -> str:
    short = _short_name(event_name)
    for p in ctx.php_files:
        if p.name == f"{short}.php":
            return str(p)
    return ""


def _parse_handle_method(source: str) -> dict[str, Any]:
    dispatched_jobs: list[str] = []
    notified: list[str] = []
    for m in _DISPATCH_NEW_RE.finditer(source):
        dispatched_jobs.append(m.group(1))
    for m in _DISPATCH_STATIC_RE.finditer(source):
        dispatched_jobs.append(m.group(1))
    for m in _NOTIFICATION_SEND_RE.finditer(source):
        notified.append(m.group(1))
    is_queued = bool(_SHOULD_QUEUE_RE.search(source))
    return {"dispatched_jobs": dispatched_jobs, "notified": notified, "is_queued": is_queued}


# ── Core upsert helpers ────────────────────────────────────────────────────────

def _upsert_event(db: Any, fqn: str, file_path: str) -> str:
    nid = make_node_id("event", fqn)
    try:
        db._insert_node("Event", {
            "node_id": nid,
            "name": _short_name(fqn),
            "fqn": fqn,
            "file_path": file_path,
            "broadcastable": False,
            "broadcast_channel": "",
        })
    except Exception:
        pass  # node already exists from a prior strategy
    return nid


def _upsert_listener(db: Any, fqn: str, file_path: str, is_queued: bool, queue: str) -> str:
    nid = make_node_id("listener", fqn)
    try:
        db._insert_node("Listener", {
            "node_id": nid,
            "name": _short_name(fqn),
            "fqn": fqn,
            "file_path": file_path,
            "is_queued": is_queued,
            "queue": queue,
        })
    except Exception:
        pass
    return nid


def _link_listener_to_event(
    db: Any,
    listener_nid: str,
    event_nid: str,
    source: str,
) -> None:
    try:
        db.upsert_rel("LISTENS_TO", "Listener", listener_nid, "Event", event_nid)
        logger.debug(
            "LISTENS_TO edge created",
            listener=listener_nid,
            event_nid=event_nid,
            detection_source=source,
        )
    except Exception as exc:
        logger.debug("LISTENS_TO rel failed", listener=listener_nid, event_nid=event_nid, error=str(exc))


# ── Strategy 1: EventServiceProvider $listen array ────────────────────────────

def _parse_listen_array(source: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    m = _LISTEN_ARRAY_RE.search(source)
    if not m:
        return result
    body = m.group(1)
    for block in _EVENT_BLOCK_RE.finditer(body):
        event_class = block.group(1)
        listeners = _CLASS_REF_RE.findall(block.group(2))
        result[event_class] = listeners
    return result


def _strategy_esp_listen_array(ctx: PipelineContext, counts: dict[str, int]) -> None:
    """Parse the classic $listen array from EventServiceProvider."""
    db = ctx.db

    esp_path = ctx.project_root / "app" / "Providers" / "EventServiceProvider.php"
    if not esp_path.exists():
        for f in ctx.php_files:
            if f.name == "EventServiceProvider.php":
                esp_path = f
                break

    if not esp_path.exists():
        logger.debug("EventServiceProvider.php not found — skipping $listen array strategy")
        return

    source = _read_source(esp_path)
    use_map = _build_use_map(esp_path, ctx)
    namespace_m = re.search(r'^namespace\s+([\w\\]+)\s*;', source, re.MULTILINE)
    namespace = namespace_m.group(1) if namespace_m else ""

    listen_map = _parse_listen_array(source)
    if not listen_map:
        logger.debug(
            "EventServiceProvider found but $listen array empty or unparseable",
            path=str(esp_path),
        )
        return

    logger.info("Parsing EventServiceProvider $listen array", events=len(listen_map))
    for event_raw, listener_raws in listen_map.items():
        event_fqn = _resolve_class(event_raw, use_map, namespace)
        event_nid = _upsert_event(db, event_fqn, _find_event_file(event_fqn, ctx))
        counts["events"] += 1

        for listener_raw in listener_raws:
            listener_fqn = _resolve_class(listener_raw, use_map, namespace)
            listener_path = _find_listener_file(listener_fqn, ctx)
            listener_file = str(listener_path) if listener_path else ""
            listener_source = _read_source(listener_path) if listener_path else ""
            handle_info = _parse_handle_method(listener_source)
            queue_m = re.search(r'public\s+\$queue\s*=\s*[\'"]([^\'"]+)[\'"]', listener_source)

            listener_nid = _upsert_listener(
                db, listener_fqn, listener_file,
                handle_info["is_queued"],
                queue_m.group(1) if queue_m else "",
            )
            counts["listeners"] += 1
            _link_listener_to_event(db, listener_nid, event_nid, "$listen array")
            _process_listener_dispatches(db, listener_nid, handle_info, ctx)


# ── Strategy 2: Event::listen() in ServiceProvider boot() ─────────────────────

def _strategy_event_listen_calls(ctx: PipelineContext, counts: dict[str, int]) -> None:
    """Detect Event::listen(EventClass::class, ListenerClass::class) calls."""
    db = ctx.db
    provider_paths: list[Path] = []

    providers_dir = ctx.project_root / "app" / "Providers"
    if providers_dir.exists():
        provider_paths.extend(providers_dir.rglob("*.php"))

    # Also check bootstrap/app.php for withEvents() (Laravel 11+)
    bootstrap_app = ctx.project_root / "bootstrap" / "app.php"
    if bootstrap_app.exists():
        provider_paths.append(bootstrap_app)

    seen_pairs: set[tuple[str, str]] = set()

    for provider_path in provider_paths:
        source = _read_source(provider_path)
        if "Event::listen" not in source and "->listen" not in source:
            continue

        use_map = _build_use_map(provider_path, ctx)
        namespace_m = re.search(r'^namespace\s+([\w\\]+)\s*;', source, re.MULTILINE)
        namespace = namespace_m.group(1) if namespace_m else ""

        # Event::listen(EventClass::class, ListenerClass::class)
        for m in _EVENT_LISTEN_RE.finditer(source):
            event_raw, listener_raw = m.group(1), m.group(2)
            event_fqn = _resolve_class(event_raw, use_map, namespace)
            listener_fqn = _resolve_class(listener_raw, use_map, namespace)
            pair = (event_fqn, listener_fqn)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            _create_event_listener_pair(db, event_fqn, listener_fqn, ctx, counts, "Event::listen()")

        # Event::listen(EventClass::class, [ListenerClass::class, 'method'])
        for m in _EVENT_LISTEN_ARRAY_RE.finditer(source):
            event_raw, listener_raw = m.group(1), m.group(2)
            event_fqn = _resolve_class(event_raw, use_map, namespace)
            listener_fqn = _resolve_class(listener_raw, use_map, namespace)
            pair = (event_fqn, listener_fqn)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            _create_event_listener_pair(db, event_fqn, listener_fqn, ctx, counts, "Event::listen(array)")

    if seen_pairs:
        logger.info("Event::listen() strategy found pairs", count=len(seen_pairs))


def _create_event_listener_pair(
    db: Any,
    event_fqn: str,
    listener_fqn: str,
    ctx: PipelineContext,
    counts: dict[str, int],
    source: str,
) -> None:
    event_nid = _upsert_event(db, event_fqn, _find_event_file(event_fqn, ctx))
    counts["events"] += 1
    listener_path = _find_listener_file(listener_fqn, ctx)
    listener_file = str(listener_path) if listener_path else ""
    listener_source = _read_source(listener_path) if listener_path else ""
    handle_info = _parse_handle_method(listener_source)
    queue_m = re.search(r'public\s+\$queue\s*=\s*[\'"]([^\'"]+)[\'"]', listener_source)
    listener_nid = _upsert_listener(
        db, listener_fqn, listener_file,
        handle_info["is_queued"],
        queue_m.group(1) if queue_m else "",
    )
    counts["listeners"] += 1
    _link_listener_to_event(db, listener_nid, event_nid, source)
    _process_listener_dispatches(db, listener_nid, handle_info, ctx)


# ── Strategy 3: PHP #[Listen] attribute ───────────────────────────────────────

def _strategy_listen_attribute(ctx: PipelineContext, counts: dict[str, int]) -> None:
    """Detect #[Listen(EventClass::class)] PHP attribute on listener classes."""
    db = ctx.db

    for php_file in ctx.parsed_php.values():
        for cls in php_file.classes:
            # Check raw attributes list from parser
            for attr in (cls.attributes or []):
                # attr may be "Listen(SomeEvent::class)" or similar
                m = re.search(r'Listen\s*\(\s*([\w\\]+)::class\s*\)', attr)
                if not m:
                    continue
                event_raw = m.group(1)
                use_map = {u.alias or _short_name(u.fqn): u.fqn for u in php_file.uses}
                namespace = php_file.namespace if hasattr(php_file, 'namespace') else ""
                event_fqn = _resolve_class(event_raw, use_map, namespace)
                listener_fqn = cls.fqn
                listener_path = Path(php_file.path) if hasattr(php_file, 'path') else None
                listener_file = php_file.path if hasattr(php_file, 'path') else ""
                listener_source = _read_source(Path(listener_file)) if listener_file else ""
                handle_info = _parse_handle_method(listener_source)
                queue_m = re.search(r'public\s+\$queue\s*=\s*[\'"]([^\'"]+)[\'"]', listener_source)
                event_nid = _upsert_event(db, event_fqn, _find_event_file(event_fqn, ctx))
                counts["events"] += 1
                listener_nid = _upsert_listener(
                    db, listener_fqn, listener_file,
                    handle_info["is_queued"],
                    queue_m.group(1) if queue_m else "",
                )
                counts["listeners"] += 1
                _link_listener_to_event(db, listener_nid, event_nid, "#[Listen] attribute")
                _process_listener_dispatches(db, listener_nid, handle_info, ctx)
                logger.debug("Found #[Listen] attribute", listener=listener_fqn, event=event_fqn)

    # Also scan raw source for projects where attributes aren't fully parsed
    for php_path in ctx.php_files:
        if "Listener" not in php_path.stem and "Listeners" not in str(php_path):
            continue
        source = _read_source(php_path)
        for m in _LISTEN_ATTR_RE.finditer(source):
            event_raw = m.group(1)
            use_map = _build_use_map(php_path, ctx)
            namespace_m = re.search(r'^namespace\s+([\w\\]+)\s*;', source, re.MULTILINE)
            namespace = namespace_m.group(1) if namespace_m else ""
            event_fqn = _resolve_class(event_raw, use_map, namespace)
            # Extract the class name from the file
            class_m = re.search(r'class\s+(\w+)', source)
            if not class_m:
                continue
            class_short = class_m.group(1)
            listener_fqn = f"{namespace}\\{class_short}" if namespace else class_short
            handle_info = _parse_handle_method(source)
            queue_m = re.search(r'public\s+\$queue\s*=\s*[\'"]([^\'"]+)[\'"]', source)
            event_nid = _upsert_event(db, event_fqn, _find_event_file(event_fqn, ctx))
            counts["events"] += 1
            listener_nid = _upsert_listener(
                db, listener_fqn, str(php_path),
                handle_info["is_queued"],
                queue_m.group(1) if queue_m else "",
            )
            counts["listeners"] += 1
            _link_listener_to_event(db, listener_nid, event_nid, "#[Listen] raw scan")


# ── Strategy 4: Type-hint auto-discovery ──────────────────────────────────────

def _strategy_type_hint_autodiscovery(ctx: PipelineContext, counts: dict[str, int]) -> None:
    """For each Listener class, infer the event from the handle() method's first typed param.

    This mirrors Laravel's own auto-discovery logic and works for:
    - Laravel 11+ (auto-discovery is the default)
    - Any project where listeners have properly typed handle() methods
    """
    db = ctx.db
    linked = 0

    for php_file in ctx.parsed_php.values():
        # Check if file is in an app/Listeners/ path
        path_str = getattr(php_file, 'path', '') or ''
        is_listener_file = (
            "/Listeners/" in path_str
            or "\\Listeners\\" in path_str
        )

        for cls in php_file.classes:
            # Skip classes not in Listeners/ unless they implement ShouldHandleEvents or similar
            if not is_listener_file:
                continue

            # Find handle() method
            handle_method = None
            for m in cls.methods:
                if m.name == "handle":
                    handle_method = m
                    break
            if handle_method is None:
                continue

            # Get first parameter's type hint — that's the event class
            if not handle_method.params:
                continue
            first_param = handle_method.params[0]
            event_type = first_param.type_hint.strip()
            if not event_type or event_type in ("", "void", "mixed"):
                continue
            # Skip primitive types
            if event_type.lower() in ("string", "int", "float", "bool", "array", "object", "null"):
                continue

            # Resolve the event FQN
            use_map = {u.alias or _short_name(u.fqn): u.fqn for u in php_file.uses}
            namespace = getattr(php_file, 'namespace', '')
            event_fqn = _resolve_class(event_type, use_map, namespace)
            listener_fqn = cls.fqn

            event_nid = _upsert_event(db, event_fqn, _find_event_file(event_fqn, ctx))
            listener_source = _read_source(Path(path_str)) if path_str else ""
            handle_info = _parse_handle_method(listener_source)
            queue_m = re.search(r'public\s+\$queue\s*=\s*[\'"]([^\'"]+)[\'"]', listener_source)
            listener_nid = _upsert_listener(
                db, listener_fqn, path_str,
                handle_info["is_queued"],
                queue_m.group(1) if queue_m else "",
            )
            # Check if LISTENS_TO already exists (from earlier strategies)
            existing = db.execute(
                "MATCH (l:Listener {node_id: $lid})-[:LISTENS_TO]->(e:Event {node_id: $eid}) RETURN l.node_id",
                {"lid": listener_nid, "eid": event_nid},
            )
            if existing:
                continue  # already linked by a more authoritative strategy

            _link_listener_to_event(db, listener_nid, event_nid, "type-hint auto-discovery")
            counts["events"] += 1
            counts["listeners"] += 1
            linked += 1

    if linked:
        logger.info("Type-hint auto-discovery found listener-event pairs", count=linked)


# ── Listener dispatch processing ──────────────────────────────────────────────

def _process_listener_dispatches(
    db: Any,
    listener_nid: str,
    handle_info: dict[str, Any],
    ctx: PipelineContext,
) -> None:
    for job_class in handle_info["dispatched_jobs"]:
        job_short = _short_name(job_class)
        job_nid = make_node_id("job", job_class)
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
            pass
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


# ── Main entry point ───────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> None:
    """Build Event → Listener → Job dispatch graph using all available strategies."""
    counts: dict[str, int] = {"events": 0, "listeners": 0}

    # Run all four strategies; each is idempotent (upsert-safe)
    _strategy_esp_listen_array(ctx, counts)
    _strategy_event_listen_calls(ctx, counts)
    _strategy_listen_attribute(ctx, counts)
    _strategy_type_hint_autodiscovery(ctx, counts)

    ctx.stats["events_mapped"] = counts["events"]
    ctx.stats["listeners_mapped"] = counts["listeners"]
    logger.info(
        "Event/Listener graph built",
        events=counts["events"],
        listeners=counts["listeners"],
    )

    # Run the dispatch detection pass now that Event/Job nodes exist.
    from laravelgraph.pipeline.phase_05_calls import run_dispatch_pass
    run_dispatch_pass(ctx)
