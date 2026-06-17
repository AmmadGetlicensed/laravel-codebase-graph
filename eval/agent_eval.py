"""Agent A/B evaluation — the headline "does LaravelGraph help" number.

For each question we run a real LLM agent twice:
  - WITH    : the LaravelGraph MCP tools are available (bridged to the in-memory
              FastMCP server).
  - WITHOUT : only generic file access (read/list/grep) over the project — the
              baseline an agent has today without LaravelGraph.

Both answers are scored by an LLM judge against the ground-truth facts. The
output is accuracy_with vs accuracy_without.

Opt-in: requires ANTHROPIC_API_KEY. Model via LARAVELGRAPH_EVAL_MODEL
(default claude-sonnet-4-6), judge via LARAVELGRAPH_EVAL_JUDGE_MODEL.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

from eval.client import index_app, resolve_app_paths

AGENT_MODEL = os.environ.get("LARAVELGRAPH_EVAL_MODEL", "claude-sonnet-4-6")
JUDGE_MODEL = os.environ.get("LARAVELGRAPH_EVAL_JUDGE_MODEL", "claude-sonnet-4-6")
MAX_TURNS = 8

SYSTEM = (
    "You are a senior Laravel engineer answering a question about a specific "
    "codebase. Use the available tools to gather facts before answering. "
    "Be concise and concrete: name the exact routes, models, methods, events, "
    "and columns involved. Do not guess — if a tool gives you the fact, state it."
)


# ── file-access tools (the WITHOUT-LaravelGraph baseline) ──────────────────────

def _file_tools_schema() -> list[dict]:
    return [
        {
            "name": "list_files",
            "description": "List files under a directory (relative to project root), recursively.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file relative to project root.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "grep",
            "description": "Search the project for a regex pattern; returns matching file:line snippets.",
            "input_schema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    ]


def _exec_file_tool(root: Path, name: str, args: dict) -> str:
    try:
        if name == "list_files":
            base = (root / args.get("path", ".")).resolve()
            if not str(base).startswith(str(root)):
                return "error: path escapes project root"
            return "\n".join(
                str(p.relative_to(root))
                for p in sorted(base.rglob("*"))
                if p.is_file() and ".laravelgraph" not in p.parts and "vendor" not in p.parts
            )[:8000]
        if name == "read_file":
            target = (root / args["path"]).resolve()
            if not str(target).startswith(str(root)) or not target.is_file():
                return "error: not a readable file in project"
            return target.read_text(errors="replace")[:12000]
        if name == "grep":
            res = subprocess.run(
                ["grep", "-rniE", args["pattern"], str(root),
                 "--include=*.php", "--exclude-dir=vendor", "--exclude-dir=.laravelgraph"],
                capture_output=True, text=True, timeout=20,
            )
            return (res.stdout or "(no matches)")[:8000]
    except Exception as e:
        return f"error: {e}"
    return f"error: unknown tool {name}"


# ── LaravelGraph tools (the WITH condition), bridged to the in-memory server ───

async def _laravelgraph_tools_schema(server) -> list[dict]:
    from fastmcp import Client

    async with Client(server) as client:
        tools = await client.list_tools()
        schema = []
        for t in tools:
            schema.append({
                "name": t.name,
                "description": (t.description or "")[:1024],
                "input_schema": t.inputSchema or {"type": "object", "properties": {}},
            })
        return schema


# ── agent loop ─────────────────────────────────────────────────────────────────

async def _run_agent(anthropic_client, question: str, tools: list[dict], exec_tool) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(MAX_TURNS):
        resp = anthropic_client.messages.create(
            model=AGENT_MODEL, max_tokens=1500, system=SYSTEM,
            tools=tools, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                out = await exec_tool(block.name, block.input or {})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": out[:12000],
                })
        messages.append({"role": "user", "content": results})
    return "(agent did not converge within turn budget)"


def _judge(anthropic_client, question: str, expect_all: list[str], answer: str) -> bool:
    rubric = (
        "You are grading whether an answer correctly states the required facts.\n"
        f"Question: {question}\n"
        f"Required facts (all must be reflected, allowing for synonyms/paths): {expect_all}\n"
        f"Answer:\n{answer}\n\n"
        'Reply with ONLY a JSON object: {"correct": true|false}. '
        "correct=true only if the answer conveys every required fact and is not a guess/refusal."
    )
    resp = anthropic_client.messages.create(
        model=JUDGE_MODEL, max_tokens=100,
        messages=[{"role": "user", "content": rubric}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return False
    try:
        return bool(json.loads(m.group(0)).get("correct"))
    except Exception:
        return False


def run_agent_ab(app: str, dataset: Path) -> dict:
    import anthropic
    import yaml

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("agent mode requires ANTHROPIC_API_KEY")

    from fastmcp import Client

    from laravelgraph.mcp.server import create_server

    with open(dataset) as f:
        questions = yaml.safe_load(f)

    source, into, cleanup = resolve_app_paths(app)
    indexed = index_app(source, into=into)
    root = indexed.root

    anthropic_client = anthropic.Anthropic()
    server = create_server(root)
    lg_tools = asyncio.run(_laravelgraph_tools_schema(server))
    file_tools = _file_tools_schema()

    async def run_all() -> list[dict]:
        rows = []
        async with Client(server) as lg_client:
            async def exec_lg(name, args):
                try:
                    r = await lg_client.call_tool(name, args)
                    from eval.client import _extract_text
                    return _extract_text(r)
                except Exception as e:
                    return f"tool error: {e}"

            async def exec_file(name, args):
                return _exec_file_tool(root, name, args)

            for q in questions:
                ans_with = await _run_agent(anthropic_client, q["question"], lg_tools, exec_lg)
                ans_without = await _run_agent(anthropic_client, q["question"], file_tools, exec_file)
                ok_with = _judge(anthropic_client, q["question"], q.get("expect_all", []), ans_with)
                ok_without = _judge(anthropic_client, q["question"], q.get("expect_all", []), ans_without)
                rows.append({
                    "id": q["id"], "category": q.get("category", ""),
                    "tool": "agent", "with": ok_with, "without": ok_without,
                    "passed": ok_with, "missing": [], "errored": False,
                })
        return rows

    rows = asyncio.run(run_all())
    if cleanup:
        indexed.cleanup()

    total = len(rows)
    acc_with = round(100.0 * sum(r["with"] for r in rows) / total, 1) if total else 0.0
    acc_without = round(100.0 * sum(r["without"] for r in rows) / total, 1) if total else 0.0
    return {
        "mode": "agent", "app": app, "total": total,
        "passed": sum(r["with"] for r in rows),
        "structural_correctness": acc_with,
        "accuracy_with": acc_with, "accuracy_without": acc_without,
        "lift": round(acc_with - acc_without, 1),
        "model": AGENT_MODEL, "results": rows,
    }
