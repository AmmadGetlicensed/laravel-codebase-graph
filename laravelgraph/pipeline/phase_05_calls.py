"""Phase 05 — Call Graph Tracing.

Build CALLS edges between Method nodes (and Method → Class_ when the
specific target method is unknown). Resolves Laravel Facades, static calls,
and instance calls with confidence scoring.

Also detects Laravel event/job dispatch patterns and writes DISPATCHES edges
(Method → Event, Method → Job). This is the only place these edges are created
for controller and service methods.
"""

from __future__ import annotations

import re
from pathlib import Path

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger, phase_timer
from laravelgraph.parsers.php import CALL_BLOCKLIST, FACADE_MAP, ParsedCall
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# Pattern for magic __call / __get style methods
_MAGIC_PATTERN = re.compile(r"^__")

# ── Dispatch detection patterns ───────────────────────────────────────────────
# Matches: dispatch(new ClassName(  OR  dispatchIf(cond, new ClassName(
# Negative lookbehind (?<!:) prevents matching Event::dispatch / Bus::dispatch
_DISPATCH_NEW_RE   = re.compile(r'(?<!:)dispatch(?:If|Now|AfterResponse|Sync)?\s*\(\s*(?:[^,]+,\s*)?new\s+([\w\\]+)\s*\(')
# Matches: ClassName::dispatch(  or  ClassName::dispatchIf(
_STATIC_DISPATCH_RE = re.compile(r'([\w\\]+)::dispatch(?:If|Now|AfterResponse|Sync)?\s*\(')
# Matches: event(new ClassName(
_EVENT_HELPER_RE   = re.compile(r'\bevent\s*\(\s*new\s+([\w\\]+)\s*\(')
# Matches: Event::dispatch(new ClassName(
_EVENT_FACADE_RE   = re.compile(r'Event::dispatch\s*\(\s*new\s+([\w\\]+)\s*\(')


def run(ctx: PipelineContext) -> None:
    """Trace all call expressions and write CALLS edges into the graph."""
    with phase_timer("Call Graph Tracing"):
        db = ctx.db
        project_root = ctx.project_root

        calls_traced = 0
        calls_unresolved = 0
        facades_resolved = 0

        use_aliases: dict[str, dict[str, str]] = getattr(ctx, "_use_aliases", {})

        for path_str, parsed in ctx.parsed_php.items():
            filepath = Path(path_str)
            file_aliases = use_aliases.get(path_str, {})

            for cls in parsed.classes:
                class_fqn = cls.fqn
                class_nid = ctx.fqn_index.get(class_fqn)

                for method in cls.methods:
                    caller_fqn = f"{class_fqn}::{method.name}"
                    caller_nid = ctx.fqn_index.get(caller_fqn)
                    if not caller_nid:
                        continue

                    for call in method.calls:
                        if _should_skip(call):
                            continue

                        to_nid, call_type, confidence = _resolve_call(
                            call=call,
                            caller_class_fqn=class_fqn,
                            file_aliases=file_aliases,
                            ctx=ctx,
                        )

                        if to_nid is None:
                            calls_unresolved += 1
                            continue

                        if call_type == "facade":
                            facades_resolved += 1

                        try:
                            db.upsert_rel(
                                "CALLS",
                                "Method", caller_nid,
                                _infer_label(to_nid), to_nid,
                                props={
                                    "confidence": confidence,
                                    "call_type": call_type,
                                    "line": call.line,
                                },
                            )
                            calls_traced += 1
                        except Exception as e:
                            logger.debug(
                                "CALLS edge failed",
                                caller=caller_fqn,
                                to=to_nid,
                                error=str(e),
                            )

            # Also trace calls in free functions
            for fn in parsed.functions:
                fn_nid = ctx.fqn_index.get(fn.fqn)
                if not fn_nid:
                    continue
                for call in fn.calls:
                    if _should_skip(call):
                        continue
                    to_nid, call_type, confidence = _resolve_call(
                        call=call,
                        caller_class_fqn="",
                        file_aliases=file_aliases,
                        ctx=ctx,
                    )
                    if to_nid is None:
                        calls_unresolved += 1
                        continue
                    if call_type == "facade":
                        facades_resolved += 1
                    try:
                        db.upsert_rel(
                            "CALLS",
                            "Function_", fn_nid,
                            _infer_label(to_nid), to_nid,
                            props={
                                "confidence": confidence,
                                "call_type": call_type,
                                "line": call.line,
                            },
                        )
                        calls_traced += 1
                    except Exception as e:
                        logger.debug("Function CALLS edge failed", fn=fn.fqn, error=str(e))

        ctx.stats["calls_traced"] = calls_traced
        ctx.stats["calls_unresolved"] = calls_unresolved
        ctx.stats["facades_resolved"] = facades_resolved

        logger.info(
            "Call graph complete",
            traced=calls_traced,
            unresolved=calls_unresolved,
            facades=facades_resolved,
        )

        # NOTE: dispatch detection (DISPATCHES edges) runs in run_dispatch_pass(),
        # called from phase_17 after Event/Job nodes have been created.


def _ensure_dispatch_node(
    short_name: str,
    dispatch_type: str,
    class_nid: str,
    event_nid_by_name: dict,
    job_nid_by_name: dict,
    ctx: "PipelineContext",  # noqa: F821
) -> str | None:
    """Create an Event or Job node from a detected dispatch pattern if one doesn't exist.

    Projects using Laravel auto-discovery or Event::listen() in boot() won't have
    Event nodes from phase_17.  This creates minimal Event/Job nodes from the class
    info already indexed by phase_03, so DISPATCHES edges can be written.

    Updates event_nid_by_name / job_nid_by_name in-place so subsequent dispatches
    to the same class don't trigger duplicate inserts.
    """
    from laravelgraph.core.schema import node_id as make_node_id

    # Derive FQN from fqn_index (class_nid → reverse lookup)
    # class_nid looks like "class:App\Events\UserRegistered"
    fqn = class_nid.replace("class:", "", 1) if class_nid.startswith("class:") else short_name

    node_table = "Event" if dispatch_type == "event" else "Job"
    node_nid = make_node_id(dispatch_type, short_name)

    # Find the file path for this class
    file_path = ""
    for p in ctx.php_files:
        if p.name == f"{short_name}.php":
            file_path = str(p)
            break

    try:
        if node_table == "Event":
            ctx.db._insert_node("Event", {
                "node_id": node_nid,
                "name": short_name,
                "fqn": fqn,
                "file_path": file_path,
                "broadcastable": False,
                "broadcast_channel": "",
            })
            event_nid_by_name[short_name] = node_nid
        else:
            ctx.db._insert_node("Job", {
                "node_id": node_nid,
                "name": short_name,
                "fqn": fqn,
                "file_path": file_path,
                "is_queued": True,
                "queue": "",
            })
            job_nid_by_name[short_name] = node_nid
        logger.debug("Auto-created dispatch node", type=node_table, name=short_name, fqn=fqn)
        return node_nid
    except Exception:
        # Node may already exist (race or duplicate dispatch detection) — look it up
        try:
            rows = ctx.db.execute(
                f"MATCH (n:{node_table}) WHERE n.name = $name RETURN n.node_id AS nid LIMIT 1",
                {"name": short_name},
            )
            if rows and rows[0].get("nid"):
                nid = rows[0]["nid"]
                if node_table == "Event":
                    event_nid_by_name[short_name] = nid
                else:
                    job_nid_by_name[short_name] = nid
                return nid
        except Exception:
            pass
    return None


def run_dispatch_pass(ctx: "PipelineContext") -> None:  # noqa: F821
    """Scan every method body for dispatch patterns and write DISPATCHES edges.

    Must be called AFTER phase_17 (Event/Listener/Job Graph) so that Event and
    Job nodes already exist in the graph.  Phase_05's main run() only handles
    CALLS edges; this separate pass handles Method → Event/Job DISPATCHES edges.
    """
    db = ctx.db
    use_aliases: dict[str, dict[str, str]] = getattr(ctx, "_use_aliases", {})

    # Build a short-name → FQN reverse index for fast lookup
    short_to_fqns: dict[str, list[str]] = {}
    for fqn in ctx.fqn_index:
        short = fqn.split("\\")[-1].split("::")[-1]
        short_to_fqns.setdefault(short, []).append(fqn)

    # Build direct short-name → node_id maps from the Event and Job tables.
    # ctx.fqn_index only maps class FQNs to Class_ node_ids; Event/Job nodes have
    # separate node_ids (e.g. "event:UserRegistered") created by phase_17.
    event_nid_by_name: dict[str, str] = {}
    job_nid_by_name: dict[str, str] = {}
    try:
        for row in (db.execute("MATCH (e:Event) RETURN e.name AS name, e.node_id AS nid") or []):
            if row.get("name") and row.get("nid"):
                event_nid_by_name[row["name"]] = row["nid"]
    except Exception:
        pass
    try:
        for row in (db.execute("MATCH (j:Job) RETURN j.name AS name, j.node_id AS nid") or []):
            if row.get("name") and row.get("nid"):
                job_nid_by_name[row["name"]] = row["nid"]
    except Exception:
        pass

    dispatches_traced = 0

    for path_str, parsed in ctx.parsed_php.items():
        filepath = Path(path_str)
        try:
            source_lines = filepath.read_text(errors="replace").splitlines()
        except OSError:
            continue

        file_aliases = use_aliases.get(path_str, {})

        for cls in parsed.classes:
            class_fqn = cls.fqn
            for method in cls.methods:
                caller_fqn = f"{class_fqn}::{method.name}"
                caller_nid = ctx.fqn_index.get(caller_fqn)
                if not caller_nid:
                    continue

                ls = (method.line_start or 1) - 1
                le = method.line_end or (ls + 200)
                method_src = "\n".join(source_lines[ls:le])

                for target_short, dispatch_type, condition_hint in _find_dispatches(method_src, file_aliases):
                    # Prefer Event/Job node lookup over fqn_index (which maps to Class_ nodes)
                    if dispatch_type == "event":
                        target_nid = event_nid_by_name.get(target_short)
                    else:
                        target_nid = job_nid_by_name.get(target_short)

                    if not target_nid:
                        # Try fqn_index as fallback (gives us the Class_ FQN)
                        class_nid = _resolve_dispatch_target(
                            target_short, dispatch_type, file_aliases, short_to_fqns, ctx
                        )
                        if class_nid:
                            # Auto-create Event/Job node from the detected class.
                            # Projects using auto-discovery or Event::listen() won't have
                            # Event nodes from phase_17 — create them here so DISPATCHES
                            # edges can be written.
                            target_nid = _ensure_dispatch_node(
                                target_short, dispatch_type, class_nid,
                                event_nid_by_name, job_nid_by_name, ctx
                            )

                    if not target_nid:
                        continue
                    target_label = "Event" if dispatch_type == "event" else "Job"
                    try:
                        db.upsert_rel(
                            "DISPATCHES",
                            "Method", caller_nid,
                            target_label, target_nid,
                            props={
                                "dispatch_type": dispatch_type,
                                "is_queued": dispatch_type == "job",
                                "line": 0,
                                "condition": condition_hint,
                            },
                        )
                        dispatches_traced += 1
                    except Exception as e:
                        logger.debug("DISPATCHES edge failed", caller=caller_fqn, target=target_short, error=str(e))

    ctx.stats["dispatches_traced"] = dispatches_traced
    logger.info("Dispatch edges traced", count=dispatches_traced)


# ── Resolution helpers ────────────────────────────────────────────────────────

def _should_skip(call: ParsedCall) -> bool:
    """Return True if this call should not produce a CALLS edge."""
    if call.method in CALL_BLOCKLIST:
        return True
    # Skip magic PHP methods
    if _MAGIC_PATTERN.match(call.method):
        return True
    return False


def _resolve_call(
    call: ParsedCall,
    caller_class_fqn: str,
    file_aliases: dict[str, str],
    ctx: PipelineContext,
) -> tuple[str | None, str, float]:
    """Attempt to resolve a ParsedCall to a graph node_id.

    Returns (node_id_or_None, call_type, confidence).
    """
    receiver = call.receiver
    method_name = call.method

    # ── 1. Facade call ────────────────────────────────────────────────────
    if call.is_static and receiver and receiver in FACADE_MAP:
        concrete_fqn = FACADE_MAP[receiver]
        # Try to find a Method node for the concrete class
        method_nid = ctx.fqn_index.get(f"{concrete_fqn}::{method_name}")
        if method_nid:
            return method_nid, "facade", 0.85
        # Fall back to Class_ node
        class_nid = ctx.fqn_index.get(concrete_fqn)
        if class_nid:
            return class_nid, "facade", 0.75
        return None, "facade", 0.0

    # ── 2. Self / this call (instance method on same class) ───────────────
    if receiver in (None, "this", "self", "static") and caller_class_fqn:
        method_nid = ctx.fqn_index.get(f"{caller_class_fqn}::{method_name}")
        if method_nid:
            return method_nid, "direct", 0.9
        # Could be inherited — return class node as approximate target
        class_nid = ctx.fqn_index.get(caller_class_fqn)
        if class_nid:
            return class_nid, "direct", 0.6

    # ── 3. Static call to a named class ──────────────────────────────────
    if call.is_static and receiver:
        resolved_fqn = _resolve_class_name(receiver, file_aliases, ctx)
        if resolved_fqn:
            method_nid = ctx.fqn_index.get(f"{resolved_fqn}::{method_name}")
            if method_nid:
                return method_nid, "direct", 0.85
            class_nid = ctx.fqn_index.get(resolved_fqn)
            if class_nid:
                return class_nid, "direct", 0.7

    # ── 4. Instance call on a typed variable ─────────────────────────────
    if not call.is_static and receiver and receiver not in ("this", "self", "static"):
        # receiver is a variable name — we can't easily type-resolve without DI,
        # but we try if the variable name matches a short class name.
        resolved_fqn = _resolve_class_name(receiver, file_aliases, ctx)
        if resolved_fqn:
            method_nid = ctx.fqn_index.get(f"{resolved_fqn}::{method_name}")
            if method_nid:
                return method_nid, "direct", 0.5
            class_nid = ctx.fqn_index.get(resolved_fqn)
            if class_nid:
                return class_nid, "direct", 0.4

    # ── 5. Free function call ─────────────────────────────────────────────
    if receiver is None:
        # Try namespace-qualified function
        fn_nid = ctx.fqn_index.get(method_name)
        if fn_nid:
            return fn_nid, "direct", 0.8

    return None, "unknown", 0.0


def _resolve_class_name(
    name: str,
    file_aliases: dict[str, str],
    ctx: PipelineContext,
) -> str | None:
    """Resolve a short class name to a fully-qualified name using use-statement aliases."""
    # Direct alias match
    if name in file_aliases:
        return file_aliases[name]
    # Already in fqn_index as-is
    if name in ctx.fqn_index:
        return name
    # Already a FQN (contains backslash)
    if "\\" in name and name in ctx.fqn_index:
        return name
    return None


def _infer_label(node_id: str) -> str:
    """Determine the node label from a node_id prefix for upsert_rel."""
    if node_id.startswith("method:"):
        return "Method"
    if node_id.startswith("class:"):
        return "Class_"
    if node_id.startswith("function:"):
        return "Function_"
    if node_id.startswith("trait:"):
        return "Trait_"
    return "Class_"


_CONDITION_RE = re.compile(
    r'\b(if\s*\(|elseif\s*\(|case\s+|switch\s*\()',
    re.IGNORECASE,
)


def _extract_condition_hint(method_src: str, match_start: int, context_lines: int = 4) -> str:
    """Return the nearest preceding if/case/switch line that guards this dispatch.

    Walks backwards up to `context_lines` non-empty lines from the dispatch
    position looking for a conditional.  Returns the raw condition text
    truncated to 120 chars, or "" if none found.
    """
    preceding_text = method_src[:match_start]
    lines = preceding_text.splitlines()
    checked = 0
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        checked += 1
        if checked > context_lines:
            break
        if _CONDITION_RE.search(stripped):
            return stripped[:120]
    return ""


def _find_dispatches(method_src: str, file_aliases: dict[str, str]) -> list[tuple[str, str, str]]:
    """Scan method source text and return (class_short_name, dispatch_type, condition_hint) triples.

    dispatch_type is "event" or "job".  We can't always know for certain — we
    use naming conventions (jobs typically end in Job/Command; events in Event/ed).
    condition_hint is the nearest preceding if/case text, or "" if unconditional.
    The caller resolves the type against the actual graph node label.
    """
    found: list[tuple[str, str, str]] = []

    # event(new X()) — always an event
    for m in _EVENT_HELPER_RE.finditer(method_src):
        hint = _extract_condition_hint(method_src, m.start())
        found.append((m.group(1).split("\\")[-1], "event", hint))

    # Event::dispatch(new X()) — event
    for m in _EVENT_FACADE_RE.finditer(method_src):
        hint = _extract_condition_hint(method_src, m.start())
        found.append((m.group(1).split("\\")[-1], "event", hint))

    # dispatch(new X()) or dispatchIf(cond, new X()) — usually a job
    for m in _DISPATCH_NEW_RE.finditer(method_src):
        cls = m.group(1).split("\\")[-1]
        dtype = "event" if cls.endswith("Event") else "job"
        hint = _extract_condition_hint(method_src, m.start())
        found.append((cls, dtype, hint))

    # ClassName::dispatch() — static dispatch on the class itself
    for m in _STATIC_DISPATCH_RE.finditer(method_src):
        cls = m.group(1).split("\\")[-1]
        if cls.lower() in ("event", "bus", "queue", "job", "mail", "notification", "dispatch"):
            continue
        if cls.endswith("Event") or cls.endswith("Notification"):
            dtype = "event"
        else:
            dtype = "job"
        hint = _extract_condition_hint(method_src, m.start())
        found.append((cls, dtype, hint))

    return found


def _resolve_dispatch_target(
    short_name: str,
    dispatch_type: str,
    file_aliases: dict[str, str],
    short_to_fqns: dict[str, list[str]],
    ctx: PipelineContext,
) -> str | None:
    """Resolve a dispatched class short name to a graph node_id.

    Checks: use-statement alias → fqn_index → short name reverse index.
    Returns the node_id of an Event or Job node, or None if not found.
    """
    # 1. Direct alias resolution
    fqn = file_aliases.get(short_name)
    if fqn:
        nid = ctx.fqn_index.get(fqn)
        if nid:
            return nid

    # 2. Already in fqn_index as short name (unlikely but possible)
    nid = ctx.fqn_index.get(short_name)
    if nid:
        return nid

    # 3. Short-name reverse lookup — pick the best match
    candidates = short_to_fqns.get(short_name, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        return ctx.fqn_index.get(candidates[0])

    # Multiple candidates — prefer ones in Events/ or Jobs/ directories
    preferred_dirs = ("Events\\", "Jobs\\", "Events/", "Jobs/")
    for fqn in candidates:
        if any(d in fqn for d in preferred_dirs):
            return ctx.fqn_index.get(fqn)

    # Fall back to first
    return ctx.fqn_index.get(candidates[0])
