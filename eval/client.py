"""Indexing + in-memory MCP tool invocation for the eval harness.

The MCP tools are defined inline inside ``create_server()`` and are not
importable as plain functions. We drive them exactly the way a real agent
would — through the MCP protocol — using FastMCP's in-memory client, which
talks to the server object directly with no socket or subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
TINY_FIXTURE = _REPO / "tests" / "fixtures" / "tiny-laravel-app"


def resolve_app_paths(app: str) -> tuple[Path, Path | None, bool]:
    """Map an app key to (source_root, copy_into_or_None, should_cleanup).

    ``tiny`` is copied into a temp dir so the fixture is never mutated; ``real``
    indexes in place at ``$LARAVELGRAPH_EVAL_REAL_APP``.
    """
    if app == "tiny":
        tmp = Path(tempfile.mkdtemp(prefix="lg_eval_")) / "tiny"
        return TINY_FIXTURE, tmp, True
    if app == "real":
        real = os.environ.get("LARAVELGRAPH_EVAL_REAL_APP")
        if not real:
            print(
                "ERROR: --app real requires LARAVELGRAPH_EVAL_REAL_APP=/path/to/laravel-app",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return Path(real).expanduser().resolve(), None, False
    raise ValueError(f"Unknown app: {app}")


@contextlib.contextmanager
def _no_llm_summaries():
    """Patch out LLM summary generation so structural eval is hermetic and fast.

    ``laravelgraph_context``/``_explain`` call ``generate_summary`` which, if a
    provider is configured globally, makes a network call (and times out in CI).
    Structural assertions never depend on the prose summary, so stub it.
    """
    import laravelgraph.mcp.server as server_mod

    original = getattr(server_mod, "generate_summary", None)
    if original is None:
        yield
        return
    server_mod.generate_summary = lambda *a, **k: (None, "disabled-for-eval")
    try:
        yield
    finally:
        server_mod.generate_summary = original


@dataclass
class IndexedApp:
    """A Laravel project that has been indexed and is ready to query."""

    root: Path
    is_temp: bool = False

    def cleanup(self) -> None:
        if self.is_temp and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


def index_app(source: Path, *, into: Path | None = None, full: bool = True) -> IndexedApp:
    """Index a Laravel app. If ``into`` is given, copy ``source`` there first
    (so the original fixture is never mutated); otherwise index in place.

    Embeddings are disabled — the eval measures structural facts, not vector
    search, and skipping embeddings keeps the harness fast and deterministic.
    """
    from laravelgraph.config import Config
    from laravelgraph.pipeline.orchestrator import Pipeline

    if into is not None:
        if into.exists():
            shutil.rmtree(into, ignore_errors=True)
        shutil.copytree(str(source), str(into))
        root = into
        is_temp = True
    else:
        root = source
        is_temp = False

    cfg = Config.load(root)
    cfg.embedding.enabled = False
    Pipeline(root, config=cfg).run(full=full, skip_embeddings=True)
    return IndexedApp(root=root, is_temp=is_temp)


def _extract_text(result: Any) -> str:
    """Normalise a FastMCP call_tool result to a plain string."""
    # FastMCP returns an object with .content (list of content blocks) and,
    # for structured returns, .data. Our tools return markdown strings.
    content = getattr(result, "content", None)
    if content:
        parts = []
        for block in content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    data = getattr(result, "data", None)
    if isinstance(data, str):
        return data
    if data is not None:
        return str(data)
    return str(result)


async def _call_tool_async(server: Any, name: str, arguments: dict) -> str:
    from fastmcp import Client

    async with Client(server) as client:
        result = await client.call_tool(name, arguments)
        return _extract_text(result)


def call_tool(
    root: Path, name: str, arguments: dict | None = None, *, disable_llm: bool = True
) -> str:
    """Synchronously create a server for ``root`` and call one tool by name."""
    from laravelgraph.mcp.server import create_server

    ctx = _no_llm_summaries() if disable_llm else contextlib.nullcontext()
    with ctx:
        server = create_server(root)
        return asyncio.run(_call_tool_async(server, name, arguments or {}))


async def _run_calls_async(server: Any, calls: list[tuple[str, dict]]) -> list[str]:
    from fastmcp import Client

    out: list[str] = []
    async with Client(server) as client:
        for name, args in calls:
            try:
                result = await client.call_tool(name, args)
                out.append(_extract_text(result))
            except Exception as e:  # a tool erroring shouldn't abort the run
                out.append(f"__TOOL_ERROR__: {e}")
    return out


def run_calls(
    root: Path, calls: list[tuple[str, dict]], *, disable_llm: bool = True
) -> list[str]:
    """Create the server once and run a batch of (tool_name, args) calls.

    Far cheaper than ``call_tool`` per question — one server, one client.
    """
    from laravelgraph.mcp.server import create_server

    ctx = _no_llm_summaries() if disable_llm else contextlib.nullcontext()
    with ctx:
        server = create_server(root)
        return asyncio.run(_run_calls_async(server, calls))


async def list_tools_async(server: Any) -> list[str]:
    from fastmcp import Client

    async with Client(server) as client:
        tools = await client.list_tools()
        return [t.name for t in tools]


def list_tool_names(root: Path) -> list[str]:
    """Return the names of every tool the server registers (incl. plugins)."""
    from laravelgraph.mcp.server import create_server

    server = create_server(root)
    return asyncio.run(list_tools_async(server))
