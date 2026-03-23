"""Phase 05 — Call Graph Tracing.

Build CALLS edges between Method nodes (and Method → Class_ when the
specific target method is unknown). Resolves Laravel Facades, static calls,
and instance calls with confidence scoring.
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
