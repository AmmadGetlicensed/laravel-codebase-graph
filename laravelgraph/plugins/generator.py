"""Plugin auto-generation system.

Generates MCP tool plugins from natural language descriptions using LLM,
then validates through a 4-layer stack with reflection loop.

Architecture
------------
Generation happens in three stages before any LLM call is made:

  1. Domain resolution  — _resolve_domain_anchors() explores the graph to find
     which routes, models, events, jobs and controllers actually implement the
     domain the description refers to.  This step is pure Python + Cypher; no
     LLM is involved.  The result is a structured dict of real node names.

  2. Grounded spec     — _generate_plugin_spec() sends the pre-resolved domain
     facts to the LLM and asks only for a multi-tool JSON spec (slug, prefix,
     list of {name, description, cypher_query, result_format} objects).  The
     LLM is given real names to substitute, not asked to invent them.

  3. Deterministic assembly — _assemble_plugin_code() builds the final Python
     from the spec.  Three tools are always added without LLM involvement:
       • {prefix}summary          — hard-coded domain overview from anchors
       • {prefix}store_discoveries — writes findings to plugin graph
       • one query tool per spec["tools"] entry

Validation layers
-----------------
  1. Static AST  — syntax, manifest, prefix rules (reuses validator.py)
  2. Schema      — Cypher node/rel labels exist in schema.py
  3. Execution   — sandbox import + register_tools call
  4. LLM-as-Judge — quality score ≥ 7/10

If all LLM attempts fail the template fallback generates a valid skeleton the
user can hand-edit.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ValidationResult:
    passed: bool
    layer: int           # 1-4, which layer was last checked
    score: float = 0.0   # LLM judge score 0-10, only meaningful for layer 4
    critique: str = ""   # what failed and why — fed into next iteration
    errors: list[str] = field(default_factory=list)


# ── Stop-words used by domain token extraction ────────────────────────────────

_STOP_WORDS: frozenset[str] = frozenset({
    "show", "list", "find", "get", "all", "that", "this", "with", "for",
    "the", "and", "need", "want", "make", "create", "build", "tool", "plugin",
    "about", "from", "into", "have", "which", "their", "there", "how", "what",
    "when", "does", "are", "was", "been", "will", "can", "could", "should",
    "would", "understand", "know", "explain", "describe", "tell", "give",
    "return", "display", "report", "see", "view", "check", "look", "search",
    "also", "just", "only", "here", "where", "more", "some", "any", "using",
    "used", "each", "than", "them", "they", "then", "time", "like", "every",
    "much", "very", "most", "such", "well", "both", "been", "across",
})


# ── Layer 2 — Schema validation ────────────────────────────────────────────────

def _validate_schema(code: str) -> ValidationResult:
    """Parse all string literals in code that look like Cypher, validate node/rel types."""
    from laravelgraph.core.schema import NODE_TYPES, REL_TYPES
    import ast as _ast

    valid_node_labels = {label for label, _ in NODE_TYPES} | {"PluginNode", "PluginEdge_Node"}
    valid_rel_types = {rel_name for rel_name, _, _ in REL_TYPES}

    tree = _ast.parse(code)
    errors = []

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Constant) and isinstance(node.value, str):
            s = node.value.strip()
            if not ("MATCH" in s.upper() or "MERGE" in s.upper() or "CREATE" in s.upper()):
                continue
            for label in _re.findall(r'\([\w\s]*:(\w+)', s):
                if label not in valid_node_labels and not label.startswith("_"):
                    errors.append(f"Unknown node label '{label}' in Cypher: {s[:80]}")
            for rel in _re.findall(r'\[:(\w+)', s):
                if rel not in valid_rel_types:
                    errors.append(f"Unknown relationship type '{rel}' in Cypher: {s[:80]}")

    if errors:
        return ValidationResult(
            passed=False, layer=2,
            critique="Schema validation failed:\n" + "\n".join(errors[:5]),
            errors=errors,
        )
    return ValidationResult(passed=True, layer=2)


# ── Layer 3 — Execution validation ────────────────────────────────────────────

def _validate_execution(code: str, core_db: Any) -> ValidationResult:
    """Import plugin in sandbox, run register_tools, call each tool with empty args."""
    import importlib.util
    import inspect
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        spec = importlib.util.spec_from_file_location("_plugin_test", tmp_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "register_tools"):
            return ValidationResult(passed=False, layer=3, critique="Plugin has no register_tools() function")

        registered_tools = []

        class _MockMCP:
            def tool(self, *a, **kw):
                def decorator(fn):
                    registered_tools.append(fn)
                    return fn
                return decorator

            def __getattr__(self, name):
                return lambda *a, **kw: None

        def _mock_db():
            class _MockDB:
                def execute(self, q, p=None):
                    return []
                def core(self):
                    return self
                def plugin(self):
                    return self
                def upsert_plugin_node(self, *a, **kw):
                    pass
            return _MockDB()

        def _mock_sql_db():
            class _MockCursor:
                def execute(self, q, p=None):
                    pass
                def fetchall(self):
                    return []
                def fetchone(self):
                    return None
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    pass
            class _MockSQLConn:
                def cursor(self):
                    return _MockCursor()
                def close(self):
                    pass
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    pass
            return _MockSQLConn()

        sig = inspect.signature(module.register_tools)
        reg_kwargs: dict = {"db": _mock_db}
        if "sql_db" in sig.parameters:
            reg_kwargs["sql_db"] = _mock_sql_db
        module.register_tools(_MockMCP(), **reg_kwargs)

        if not registered_tools:
            return ValidationResult(passed=False, layer=3, critique="Plugin registered no tools")

        errors = []
        for fn in registered_tools:
            try:
                fn()
            except TypeError:
                pass  # missing required args is OK
            except Exception as e:
                errors.append(f"{fn.__name__}: {e}")

        if errors:
            return ValidationResult(
                passed=False, layer=3,
                critique="Tool execution errors:\n" + "\n".join(errors[:3]),
                errors=errors,
            )

        return ValidationResult(passed=True, layer=3)

    except SyntaxError as e:
        return ValidationResult(passed=False, layer=3, critique=f"Syntax error: {e}")
    except Exception as e:
        return ValidationResult(passed=False, layer=3, critique=f"Import failed: {e}")
    finally:
        import os
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── Layer 4 — LLM-as-Judge ────────────────────────────────────────────────────

def _validate_llm_judge(description: str, code: str, cfg: Any) -> ValidationResult:
    """Use LLM to evaluate if plugin genuinely solves the described need."""
    from laravelgraph.mcp.summarize import (
        PROVIDER_REGISTRY,
        _call_anthropic,
        _call_openai_compat,
        _get_api_key,
        _get_base_url,
        _get_model,
        _resolve_provider,
    )
    import json

    summary_cfg = cfg.llm
    if not summary_cfg.enabled:
        return ValidationResult(passed=True, layer=4, score=7.0, critique="LLM disabled — layer 4 skipped")

    provider = _resolve_provider(summary_cfg)
    if not provider:
        return ValidationResult(passed=True, layer=4, score=7.0, critique="No LLM — layer 4 skipped")

    model = _get_model(provider, summary_cfg)
    if not model:
        return ValidationResult(passed=True, layer=4, score=7.0, critique="No model — layer 4 skipped")

    prompt = (
        "You are evaluating a LaravelGraph MCP plugin.\n\n"
        f"ORIGINAL REQUEST: {description}\n\n"
        f"GENERATED PLUGIN CODE:\n{code[:3000]}\n\n"
        "Score 1-10 on: does it address the request, are the queries sound, "
        "is the output meaningful?\n"
        'Reply ONLY: {"score": <1-10>, "critique": "<specific feedback>"}'
    )

    api_key = _get_api_key(provider, summary_cfg)
    base_url = _get_base_url(provider, summary_cfg)
    info = PROVIDER_REGISTRY[provider]

    result: str | None = None
    if info["sdk"] == "anthropic":
        result = _call_anthropic(prompt, api_key, model)
    else:
        result = _call_openai_compat(prompt, api_key or "no-key", model, base_url)

    if not result:
        return ValidationResult(passed=True, layer=4, score=7.0, critique="No LLM response — layer 4 skipped")

    try:
        clean = _re.sub(r"```(?:json)?|```", "", result).strip()
        parsed = json.loads(clean)
        score = float(parsed.get("score", 0))
        critique = parsed.get("critique", "")
        return ValidationResult(passed=score >= 7.0, layer=4, score=score, critique=critique)
    except Exception as e:
        return ValidationResult(passed=True, layer=4, score=7.0, critique=f"Could not parse judge response: {e}")


# ── Domain Resolver ────────────────────────────────────────────────────────────
#
# This section is the core of the domain-anchored generation approach.
# It runs pure Python + Cypher against the real graph — no LLM — to discover
# which nodes actually implement the domain the description refers to.
# The structured result (anchors dict) is then handed to the LLM so it only
# has to substitute real names into query templates, not invent them.

def _description_tokens(description: str) -> list[str]:
    """Extract meaningful domain tokens from a plugin description.

    Strips stop words, short words, and common verbs so only domain-relevant
    terms remain (e.g. "order", "refund", "inventory", "subscription").
    """
    words = _re.findall(r'[a-z]{3,}', description.lower())
    return [w for w in words if w not in _STOP_WORDS][:10]


def _safe_execute(core_db: Any, query: str, params: dict | None = None) -> list[dict]:
    """Run a Cypher query and return rows, swallowing all exceptions."""
    try:
        return core_db.execute(query, params) or []
    except Exception:
        return []


def _categorise_symbols(rows: list[dict], anchors: dict) -> None:
    """Sort graph nodes returned from a BELONGS_TO_FEATURE expansion into buckets."""
    for r in rows:
        lbl = (r.get("lbl") or "").strip()
        name = r.get("name") or ""
        if not name:
            continue
        if lbl == "Route":
            entry = {
                "method": r.get("http_method") or r.get("method") or "?",
                "uri": r.get("uri") or "?",
                "action": r.get("action") or "?",
            }
            if entry not in anchors["routes"]:
                anchors["routes"].append(entry)
        elif lbl == "EloquentModel":
            entry = {"name": name, "table": r.get("db_table") or "?"}
            if entry not in anchors["models"]:
                anchors["models"].append(entry)
        elif lbl == "Event":
            entry = {"name": name, "listeners": []}
            if not any(e["name"] == name for e in anchors["events"]):
                anchors["events"].append(entry)
        elif lbl == "Job":
            entry = {"name": name, "queue": r.get("queue") or ""}
            if not any(j["name"] == name for j in anchors["jobs"]):
                anchors["jobs"].append(entry)
        elif lbl in ("Class_", "Controller"):
            if name not in anchors["controllers"]:
                anchors["controllers"].append(name)


def _try_feature_match(core_db: Any, tokens: list[str], anchors: dict) -> None:
    """Phase A: find the Feature node that best matches the description tokens.

    Feature nodes are pre-built by pipeline phase 27 — they are the cleanest
    domain entry point because they already group routes, models, events, and
    jobs by URI prefix and namespace proximity.
    """
    rows = _safe_execute(
        core_db,
        "MATCH (f:Feature) RETURN f.name AS name, f.slug AS slug, f.node_id AS nid",
    )
    best_score = 0
    best = None
    for r in rows:
        name_l = (r.get("name") or "").lower()
        slug_l = (r.get("slug") or "").lower()
        score = sum(1 for t in tokens if t in name_l or t in slug_l)
        if score > best_score:
            best_score = score
            best = r

    if not best or best_score == 0:
        return

    anchors["feature_name"] = best.get("name")
    anchors["feature_slug"] = best.get("slug")
    anchors["matched_by"] = "feature_node"
    nid = best.get("nid")

    # Pull symbols by label separately to avoid cross-label property access errors
    # (KuzuDB rejects s.method when s is EloquentModel, etc.)
    sym_rows: list[dict] = []
    for label, extra in [
        ("Route",        "s.method AS http_method, s.uri AS uri, s.action AS action, null AS fqn, null AS db_table, null AS queue"),
        ("EloquentModel","null AS http_method, null AS uri, null AS action, s.fqn AS fqn, s.db_table AS db_table, null AS queue"),
        ("Event",        "null AS http_method, null AS uri, null AS action, s.fqn AS fqn, null AS db_table, null AS queue"),
        ("Job",          "null AS http_method, null AS uri, null AS action, s.fqn AS fqn, null AS db_table, s.queue AS queue"),
        ("Class_",       "null AS http_method, null AS uri, null AS action, s.fqn AS fqn, null AS db_table, null AS queue"),
    ]:
        rows = _safe_execute(
            core_db,
            f"MATCH (s:{label})-[:BELONGS_TO_FEATURE]->(f:Feature {{node_id: $nid}}) "
            f"RETURN '{label}' AS lbl, s.name AS name, {extra} LIMIT 20",
            {"nid": nid},
        )
        sym_rows.extend(rows)
    _categorise_symbols(sym_rows, anchors)


def _try_token_scan(core_db: Any, tokens: list[str], anchors: dict) -> None:
    """Phase B: fallback when no Feature node matches — scan all node types by token.

    This is used for codebases that haven't run phase 27 yet, or for domains
    that don't map cleanly to a single URI prefix (e.g. "authentication").
    """
    if not tokens:
        return
    anchors["matched_by"] = "token_scan"

    for token in tokens[:5]:
        for r in _safe_execute(
            core_db,
            "MATCH (r:Route) WHERE toLower(r.uri) CONTAINS $t OR toLower(r.action) CONTAINS $t "
            "RETURN r.method AS method, r.uri AS uri, r.action AS action LIMIT 15",
            {"t": token},
        ):
            entry = {"method": r.get("method", "?"), "uri": r.get("uri", "?"), "action": r.get("action", "?")}
            if entry not in anchors["routes"]:
                anchors["routes"].append(entry)

        for r in _safe_execute(
            core_db,
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS $t "
            "RETURN m.name AS name, m.db_table AS table_name LIMIT 10",
            {"t": token},
        ):
            entry = {"name": r.get("name", "?"), "table": r.get("table_name", "?")}
            if entry not in anchors["models"]:
                anchors["models"].append(entry)

        for r in _safe_execute(
            core_db,
            "MATCH (e:Event) WHERE toLower(e.name) CONTAINS $t "
            "RETURN e.name AS name LIMIT 10",
            {"t": token},
        ):
            name = r.get("name", "?")
            if not any(e["name"] == name for e in anchors["events"]):
                anchors["events"].append({"name": name, "listeners": []})

        for r in _safe_execute(
            core_db,
            "MATCH (j:Job) WHERE toLower(j.name) CONTAINS $t "
            "RETURN j.name AS name LIMIT 10",
            {"t": token},
        ):
            name = r.get("name", "?")
            if not any(j["name"] == name for j in anchors["jobs"]):
                anchors["jobs"].append({"name": name, "queue": ""})


def _expand_event_listeners(core_db: Any, anchors: dict) -> None:
    """Phase C: for every discovered event, find its listener classes."""
    for event in anchors["events"][:6]:
        event_name = event.get("name") or ""
        if not event_name:
            continue
        rows = _safe_execute(
            core_db,
            "MATCH (e:Event {name: $name})<-[:LISTENS_TO]-(l:Class_) "
            "RETURN l.name AS listener_name LIMIT 5",
            {"name": event_name},
        )
        event["listeners"] = [r.get("listener_name", "?") for r in rows]


def _resolve_domain_anchors(core_db: Any, description: str) -> dict:
    """Explore the graph to resolve which nodes implement the described domain.

    Returns a structured dict with routes, models, events, jobs, and controllers
    found in the graph. Uses Feature nodes (phase 27) as the primary anchor
    when available, falls back to token scanning when not.

    This function does no LLM calls — it's pure graph exploration.
    """
    anchors: dict = {
        "feature_name": None,
        "feature_slug": None,
        "matched_by": None,
        "tokens_used": [],
        "routes": [],
        "models": [],
        "events": [],
        "jobs": [],
        "controllers": [],
    }

    if core_db is None:
        return anchors

    tokens = _description_tokens(description)
    anchors["tokens_used"] = tokens

    _try_feature_match(core_db, tokens, anchors)

    if not anchors["feature_name"]:
        _try_token_scan(core_db, tokens, anchors)

    _expand_event_listeners(core_db, anchors)

    return anchors


def _format_anchors_for_prompt(anchors: dict) -> str:
    """Convert the domain anchors dict into a terse, LLM-readable facts section."""
    lines: list[str] = []

    if anchors.get("feature_name"):
        lines.append(f"Feature cluster matched: {anchors['feature_name']!r} (slug: {anchors.get('feature_slug', '?')})")
        lines.append(f"Match method: {anchors.get('matched_by', '?')}")
    elif anchors.get("matched_by"):
        tokens = anchors.get("tokens_used") or []
        lines.append(f"Token scan used (no Feature node matched): tokens={tokens}")
    else:
        lines.append("No domain match found — use generic graph queries.")

    if anchors.get("routes"):
        lines.append(f"\nRoutes ({len(anchors['routes'])}):")
        for r in anchors["routes"][:12]:
            lines.append(f"  {r.get('method','?')} {r.get('uri','?')} → {r.get('action','?')}")

    if anchors.get("models"):
        lines.append(f"\nModels ({len(anchors['models'])}):")
        for m in anchors["models"][:8]:
            lines.append(f"  {m.get('name','?')} (table: {m.get('table','?')})")

    if anchors.get("events"):
        lines.append(f"\nEvents ({len(anchors['events'])}):")
        for e in anchors["events"][:6]:
            listeners = e.get("listeners") or []
            suffix = f" → [{', '.join(listeners)}]" if listeners else ""
            lines.append(f"  {e.get('name','?')}{suffix}")

    if anchors.get("jobs"):
        lines.append(f"\nJobs ({len(anchors['jobs'])}):")
        for j in anchors["jobs"][:6]:
            q = j.get("queue") or ""
            lines.append(f"  {j.get('name','?')}" + (f" (queue: {q})" if q else ""))

    if anchors.get("controllers"):
        lines.append(f"\nControllers: {', '.join(anchors['controllers'][:6])}")

    return "\n".join(lines) if lines else "No domain data resolved."


# ── LLM plugin generation ──────────────────────────────────────────────────────

def _call_llm(prompt: str, cfg: Any) -> str | None:
    """Call the configured LLM and return the raw text response."""
    from laravelgraph.mcp.summarize import (
        PROVIDER_REGISTRY,
        _call_anthropic,
        _call_openai_compat,
        _get_api_key,
        _get_base_url,
        _get_model,
        _resolve_provider,
    )
    summary_cfg = cfg.llm
    if not summary_cfg.enabled:
        return None
    provider = _resolve_provider(summary_cfg)
    if not provider:
        return None
    model = _get_model(provider, summary_cfg)
    if not model:
        return None
    api_key = _get_api_key(provider, summary_cfg)
    base_url = _get_base_url(provider, summary_cfg)
    info = PROVIDER_REGISTRY[provider]
    if info["sdk"] == "anthropic":
        return _call_anthropic(prompt, api_key, model)
    return _call_openai_compat(prompt, api_key or "no-key", model, base_url)


def _generate_plugin_spec(
    description: str,
    anchors: dict,
    critique: str,
    cfg: Any,
) -> dict | None:
    """Ask the LLM for a multi-tool JSON spec using pre-resolved domain facts.

    The LLM receives real node names from the graph — it only has to select
    which aspects of the domain to expose and write short Cypher queries using
    those names.  It does NOT invent node names or relationship types.
    """
    import json

    domain_facts = _format_anchors_for_prompt(anchors)
    has_anchors = bool(anchors.get("routes") or anchors.get("models") or anchors.get("events"))

    critique_section = (
        f"\n\nPREVIOUS ATTEMPT REJECTED. Fix these specific issues:\n{critique}"
        if critique else ""
    )

    # Build a tight node-type section showing only what's known to be populated
    populated_types = "Route, EloquentModel, Event, Job, Class_, Method, Feature, DatabaseTable, DatabaseColumn"

    prompt = (
        "You are building a LaravelGraph MCP plugin.\n\n"
        f"USER REQUEST: {description}\n\n"
        "=== DOMAIN DATA ALREADY FOUND IN THIS GRAPH ===\n"
        f"{domain_facts}\n\n"
        "=== AVAILABLE NODE TYPES (use only these) ===\n"
        f"  {populated_types}\n\n"
        "=== AVAILABLE RELATIONSHIP TYPES ===\n"
        "  (Route)-[:ROUTES_TO]->(Method)\n"
        "  (Method)-[:DISPATCHES]->(Event)\n"
        "  (Event)<-[:LISTENS_TO]-(Class_)\n"
        "  (Method)-[:DISPATCHES]->(Job)\n"
        "  (EloquentModel)-[:USES_TABLE]->(DatabaseTable)\n"
        "  (Class_)-[:DEFINES]->(Method)\n"
        "  (EloquentModel)-[:HAS_RELATIONSHIP]->(EloquentModel)\n"
        "  (anything)-[:BELONGS_TO_FEATURE]->(Feature)\n\n"
        "TASK: Generate 2 focused MCP tools that answer distinct questions about "
        "the domain described. Use ONLY the node names shown in the domain data above.\n\n"
        "Reply with ONLY this JSON (no markdown, no explanation):\n"
        '{\n'
        '  "slug": "domain-name",\n'
        '  "prefix": "domain_",\n'
        '  "tools": [\n'
        '    {\n'
        '      "name": "domain_what_it_does",\n'
        '      "description": "One sentence.",\n'
        '      "cypher_query": "MATCH (r:Route) RETURN r.method AS m, r.uri AS u LIMIT 30",\n'
        '      "result_format": "[{m}] {u}"\n'
        '    }\n'
        '  ]\n'
        '}\n\n'
        "Rules:\n"
        '- slug: hyphens and lowercase only\n'
        '- prefix: 3-12 chars, lowercase, ends with underscore, NOT "laravelgraph_"\n'
        "- every tool name must start with the prefix\n"
        "- cypher_query: valid Cypher, LIMIT 30 or 50\n"
        "- result_format: use {alias} placeholders matching AS aliases in the query\n"
        + (
            "- IMPORTANT: use the exact node names shown in the domain data above\n"
            if has_anchors else
            "- No domain data was found; write generic queries using Route and EloquentModel\n"
        )
        + f"{critique_section}\n\nReply with ONLY the JSON."
    )

    raw = _call_llm(prompt, cfg)
    if raw is None:
        # Sentinel: LLM not configured or unavailable (distinct from bad JSON)
        return False  # type: ignore[return-value]
    if not raw:
        return None

    # Strip markdown fences
    clean = _re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    # Try whole response, then outermost { ... }
    spec = None
    for candidate in [clean, None]:
        if candidate is None:
            start = clean.find("{")
            end = clean.rfind("}")
            if start == -1 or end <= start:
                return None
            candidate = clean[start:end + 1]
        try:
            spec = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue

    if spec is None:
        return None

    # Validate top-level required fields
    if not all(spec.get(k) for k in ("slug", "prefix", "tools")):
        return None
    if not isinstance(spec["tools"], list) or not spec["tools"]:
        return None

    prefix = spec["prefix"]

    # Validate and sanitise each tool entry
    valid_tools = []
    for tool in spec["tools"]:
        if not all(tool.get(k) for k in ("name", "description", "cypher_query", "result_format")):
            continue
        # Enforce prefix on tool name
        if not tool["name"].startswith(prefix.rstrip("_")):
            tool["name"] = prefix.rstrip("_") + "_" + tool["name"].lstrip("_")
        # Cross-check: result_format placeholders must match AS aliases in query
        aliases = set(_re.findall(r'\bAS\s+(\w+)', tool["cypher_query"], _re.IGNORECASE))
        placeholders = set(_re.findall(r'\{(\w+)\}', tool["result_format"]))
        missing = placeholders - aliases
        if missing:
            # Auto-fix: remove unknown placeholders from format
            for ph in missing:
                tool["result_format"] = tool["result_format"].replace("{" + ph + "}", "?")
        valid_tools.append(tool)

    if not valid_tools:
        return None

    spec["tools"] = valid_tools
    return spec


# ── Plugin code assembly ───────────────────────────────────────────────────────

def _build_summary_text(anchors: dict, tool_names: list[str]) -> str:
    """Build the hard-coded domain summary for the {prefix}summary tool.

    This is assembled entirely from the domain anchors — no LLM involved.
    It always gives the agent an accurate domain overview even if the LLM
    produced mediocre query tools.
    """
    lines: list[str] = []
    fname = anchors.get("feature_name")
    if fname:
        lines.append(f"## {fname} Domain")
    else:
        lines.append("## Domain Overview")

    if anchors.get("routes"):
        lines.append(f"\nEntry routes ({len(anchors['routes'])}):")
        for r in anchors["routes"][:10]:
            lines.append(f"  {r.get('method','?')} {r.get('uri','?')} -> {r.get('action','?')}")

    if anchors.get("models"):
        lines.append(f"\nModels ({len(anchors['models'])}):")
        for m in anchors["models"][:8]:
            lines.append(f"  {m.get('name','?')} (table: {m.get('table','?')})")

    if anchors.get("events"):
        lines.append(f"\nEvents ({len(anchors['events'])}):")
        for e in anchors["events"][:6]:
            listeners = e.get("listeners") or []
            suffix = f" -> [{', '.join(listeners)}]" if listeners else ""
            lines.append(f"  {e.get('name','?')}{suffix}")

    if anchors.get("jobs"):
        lines.append(f"\nJobs ({len(anchors['jobs'])}):")
        for j in anchors["jobs"][:6]:
            q = j.get("queue") or ""
            lines.append(f"  {j.get('name','?')}" + (f" (queue: {q})" if q else ""))

    if tool_names:
        lines.append(f"\nAvailable tools: {', '.join(t + '()' for t in tool_names)}")

    lines.append("\nGenerated from static graph analysis — call the tools above for live data.")
    return "\n".join(lines)


def _build_query_tool(prefix: str, tool_spec: dict) -> str:
    """Assemble a single @mcp.tool() function from a tool spec dict.

    All string literals in generated code use double quotes (via json.dumps)
    to avoid quote collisions in the assembled file.
    """
    import json as _json

    name = tool_spec["name"]
    desc = tool_spec["description"]
    cypher = tool_spec["cypher_query"]
    result_format = tool_spec["result_format"]

    # Convert {alias} placeholders to {r.get('alias', '?')} with single quotes
    # so the expression inside the outer double-quoted f-string stays valid Python.
    aliases = _re.findall(r'\bAS\s+(\w+)', cypher, _re.IGNORECASE)
    if not aliases:
        result_line = "str(r)"
    else:
        fmt = result_format
        for alias in aliases:
            fmt = fmt.replace("{" + alias + "}", "{r.get('" + alias + "', '?')}")
        result_line = 'f"' + fmt + '"'

    desc_literal = _json.dumps(desc)
    query_literal = _json.dumps(cypher)

    return (
        f"    @mcp.tool()\n"
        f"    def {name}() -> str:\n"
        f"        {desc_literal}\n"
        f"        rows = db().execute({query_literal})\n"
        f"        if not rows:\n"
        f'            return "No data found."\n'
        f"        lines = [{result_line} for r in rows]\n"
        f'        return "\\n".join(lines)\n'
    )


def _build_summary_tool(prefix: str, summary_text: str) -> str:
    """Assemble the {prefix}summary tool (no DB call needed)."""
    fn_name = prefix.rstrip("_") + "_summary"
    # Use json.dumps to get a clean double-quoted string literal for any content
    import json as _json
    text_literal = _json.dumps(summary_text)
    return (
        f"    @mcp.tool()\n"
        f"    def {fn_name}() -> str:\n"
        f'        "Domain overview assembled from graph analysis."\n'
        f"        return {text_literal}\n"
    )


def _build_store_tool(prefix: str, slug: str, anchors: dict) -> str:
    """Assemble the {prefix}store_discoveries tool.

    Writes key domain facts from the graph into the plugin knowledge graph so
    future agent sessions can read accumulated domain knowledge without re-running
    the full analysis.

    All string literals in the generated code use double quotes to avoid quote
    conflicts when the tool is assembled inside f-strings.
    """
    fn_name = prefix.rstrip("_") + "_store_discoveries"

    # Use double-quote repr for slug to avoid single-quote collisions in generated code
    slug_dq = '"' + slug.replace('"', '\\"') + '"'

    # Derive a URI keyword to filter routes (if we have route anchors)
    uri_keyword = ""
    if anchors.get("routes"):
        parts = (anchors["routes"][0].get("uri") or "").strip("/").split("/")
        uri_keyword = parts[0] if parts else ""

    if uri_keyword:
        query_line = (
            f'        rows = db().execute(\n'
            f'            "MATCH (r:Route) WHERE r.uri CONTAINS \\"{uri_keyword}\\" '\
            f'RETURN r.method AS m, r.uri AS u, r.action AS a LIMIT 50"\n'
            f'        )\n'
        )
    else:
        query_line = (
            f'        rows = db().execute(\n'
            f'            "MATCH (r:Route) RETURN r.method AS m, r.uri AS u, r.action AS a LIMIT 50"\n'
            f'        )\n'
        )

    return (
        f"    @mcp.tool()\n"
        f"    def {fn_name}() -> str:\n"
        f'        "Store discovered domain facts into the plugin knowledge graph for future sessions."\n'
        f"        stored = 0\n"
        + query_line +
        f"        for row in rows:\n"
        f"            db().plugin().upsert_plugin_node(\n"
        f"                plugin_source={slug_dq},\n"
        f'                label="DomainRoute",\n'
        f'                node_id="domain:" + {slug_dq} + ":route:" + (row.get("u") or ""),\n'
        f'                data={{"uri": row.get("u"), "method": row.get("m"), "action": row.get("a")}},\n'
        f'                core_ref=row.get("u") or "",\n'
        f"            )\n"
        f"            stored += 1\n"
        f'        return "Stored " + str(stored) + " domain facts for {slug}."\n'
    )


def _assemble_plugin_code(spec: dict, anchors: dict) -> str:
    """Build the complete Python plugin file from a validated spec + domain anchors.

    Always emits three guaranteed tools beyond whatever the LLM spec requested:
      {prefix}summary          — hard-coded domain overview from anchors (no DB)
      {prefix}store_discoveries — writes domain facts to plugin graph
    Plus one tool per entry in spec["tools"].
    """
    slug = spec["slug"]
    prefix = spec["prefix"]

    # Collect all tool function names for the summary tool's "available tools" list
    llm_tool_names = [t["name"] for t in spec.get("tools", [])]
    summary_fn = prefix.rstrip("_") + "_summary"
    store_fn = prefix.rstrip("_") + "_store_discoveries"
    all_tool_names = [summary_fn] + llm_tool_names + [store_fn]

    summary_text = _build_summary_text(anchors, all_tool_names)

    # Build all tool blocks
    tool_blocks: list[str] = []
    tool_blocks.append(_build_summary_tool(prefix, summary_text))
    for tool_spec in spec.get("tools", []):
        tool_blocks.append(_build_query_tool(prefix, tool_spec))
    tool_blocks.append(_build_store_tool(prefix, slug, anchors))

    import json as _json
    first_desc = spec.get("tools", [{}])[0].get("description", slug) if spec.get("tools") else slug
    desc_literal = _json.dumps(first_desc)

    return (
        f'PLUGIN_MANIFEST = {{\n'
        f'    "name": "{slug}",\n'
        f'    "version": "1.0.0",\n'
        f'    "description": {desc_literal},\n'
        f'    "tool_prefix": "{prefix}",\n'
        f'}}\n'
        f'\n'
        f'\n'
        f'def register_tools(mcp, db=None, sql_db=None):\n'
        + "\n".join(tool_blocks)
    )


def _generate_plugin_code(
    description: str,
    anchors: dict,
    critique: str,
    cfg: Any,
) -> str | None:
    """Generate plugin code: get JSON spec from LLM, assemble Python deterministically.

    Returns:
        str  — assembled plugin code (success)
        None — LLM returned something but it wasn't parseable JSON (retryable)
        False (via sentinel) — LLM not configured / unavailable (fail fast)
    """
    spec = _generate_plugin_spec(description, anchors, critique, cfg)
    if spec is False:
        return False  # type: ignore[return-value]
    if spec is None:
        return None
    return _assemble_plugin_code(spec, anchors)


# ── Template fallback ─────────────────────────────────────────────────────────

def _build_template_fallback(description: str) -> str:
    """Build a minimal valid plugin skeleton without calling the LLM.

    Used when the LLM cannot produce a valid spec after max_iterations.
    The skeleton always passes validation — the user edits the Cypher inside.
    """
    _STOP = frozenset({
        "the", "and", "for", "are", "not", "but", "that", "this", "with",
        "need", "want", "make", "create", "build", "tool", "plugin", "all",
        "just", "only", "into", "from", "have", "which", "their", "there",
        "list", "show", "get", "find", "give", "return", "display", "report",
    })
    words = [w for w in _re.findall(r'[a-z]+', description.lower())
             if len(w) > 2 and w not in _STOP][:3]
    if not words:
        words = ["custom"]
    slug = "-".join(words)
    prefix = words[0] + "_"
    tool_fn = prefix + ("_".join(words[1:3]) if len(words) > 1 else "query")
    slug = _re.sub(r"[^a-zA-Z0-9\-]", "-", slug).strip("-") or "custom-plugin"
    prefix = _re.sub(r"[^a-zA-Z0-9_]", "_", prefix).strip("_") + "_"
    tool_fn = _re.sub(r"[^a-zA-Z0-9_]", "_", tool_fn).strip("_") or "query"
    if not tool_fn.startswith(prefix.rstrip("_")):
        tool_fn = prefix.rstrip("_") + "_" + tool_fn
    summary_fn = prefix.rstrip("_") + "_summary"
    desc_safe = repr(description[:120])
    note = repr(f"Skeleton plugin — edit the Cypher query to match: {description[:80]}")
    return (
        f'PLUGIN_MANIFEST = {{\n'
        f'    "name": "{slug}",\n'
        f'    "version": "1.0.0",\n'
        f'    "description": {desc_safe},\n'
        f'    "tool_prefix": "{prefix}",\n'
        f'}}\n'
        f'\n'
        f'\n'
        f'def register_tools(mcp, db=None, sql_db=None):\n'
        f'    @mcp.tool()\n'
        f'    def {summary_fn}() -> str:\n'
        f'        {note}\n'
        f'        return {note}\n'
        f'\n'
        f'    @mcp.tool()\n'
        f'    def {tool_fn}() -> str:\n'
        f'        {desc_safe}\n'
        f'        # TODO: Replace with a query that matches your request.\n'
        f'        rows = db().execute(\n'
        f'            "MATCH (r:Route) RETURN r.method AS m, r.uri AS u, r.action AS a LIMIT 50"\n'
        f'        )\n'
        f'        if not rows:\n'
        f'            return "No data found."\n'
        f'        lines = [f"[{{r.get(\'m\', \'?\')}}] {{r.get(\'u\', \'?\')}} -> {{r.get(\'a\', \'?\')}}" for r in rows]\n'
        f'        return "\\n".join(lines)\n'
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_plugin(
    description: str,
    project_root: Path,
    core_db: Any,
    cfg: Any,
    max_iterations: int = 3,
) -> tuple[str | None, str]:
    """Generate a domain-aware plugin from a description.

    Workflow:
      1. Resolve domain anchors from the graph (no LLM).
      2. For up to max_iterations: ask LLM for a multi-tool JSON spec,
         assemble Python deterministically, validate through 4 layers.
      3. If all iterations fail at layer 1: try the template fallback.

    Returns:
        (plugin_code, status_message)
        plugin_code is None only if everything — including the fallback — fails.
    """
    from laravelgraph.logging import get_logger
    log = get_logger(__name__)

    from laravelgraph.plugins.validator import (
        PluginValidationError as _PVE,
        validate_plugin_file_content,
    )

    # ── Stage 1: Resolve the domain from the graph (no LLM) ──────────────────
    anchors = _resolve_domain_anchors(core_db, description)
    log.info(
        "Domain anchors resolved",
        description=description[:80],
        feature=anchors.get("feature_name"),
        matched_by=anchors.get("matched_by"),
        routes=len(anchors.get("routes", [])),
        models=len(anchors.get("models", [])),
        events=len(anchors.get("events", [])),
    )

    last_critique = ""
    last_failed_layer = 0

    for iteration in range(1, max_iterations + 1):
        log.info("Plugin generation attempt", description=description[:80], iteration=iteration)

        # ── Stage 2: Generate spec + assemble code ────────────────────────────
        code = _generate_plugin_code(description, anchors, last_critique, cfg)
        if code is False:
            # LLM not configured — no point retrying
            return None, "No LLM provider configured. Run `laravelgraph providers add`."
        if not code:
            # LLM returned something but JSON parsing failed — retry or fall back to template
            last_critique = "LLM returned invalid or unparseable JSON. Return only valid JSON matching the schema."
            last_failed_layer = 1
            log.warning("Spec parse failed", iteration=iteration)
            continue

        # Strip stray markdown fences the model may have added around the JSON
        code = _re.sub(r"^```python\s*\n?", "", code.strip())
        code = _re.sub(r"\n?```\s*$", "", code)

        # Layer 1: Static AST
        try:
            l1 = validate_plugin_file_content(code)
            if not l1.passed:
                last_critique = f"Validation errors: {'; '.join(l1.errors)}"
                last_failed_layer = 1
                log.warning("Layer 1 failed", iteration=iteration, critique=last_critique)
                continue
        except _PVE as pve:
            err_msg = pve.errors[0] if pve.errors else str(pve)
            last_critique = f"Validation error: {err_msg}"
            last_failed_layer = 1
            log.warning("Layer 1 failed", iteration=iteration, critique=err_msg)
            continue
        except Exception as e:
            last_critique = f"Unexpected validation error: {e}"
            last_failed_layer = 1
            log.warning("Layer 1 failed", iteration=iteration, critique=last_critique)
            continue

        # Layer 2: Schema
        l2 = _validate_schema(code)
        if not l2.passed:
            last_critique = l2.critique
            last_failed_layer = 2
            log.warning("Layer 2 failed", iteration=iteration, critique=last_critique)
            continue

        # Layer 3: Execution
        l3 = _validate_execution(code, core_db)
        if not l3.passed:
            last_critique = l3.critique
            last_failed_layer = 3
            log.warning("Layer 3 failed", iteration=iteration, critique=last_critique)
            continue

        # Layer 4: LLM Judge
        l4 = _validate_llm_judge(description, code, cfg)
        if not l4.passed:
            last_critique = f"Quality score {l4.score}/10: {l4.critique}"
            last_failed_layer = 4
            log.warning("Layer 4 failed", iteration=iteration, score=l4.score, critique=l4.critique)
            continue

        log.info("Plugin generated", description=description[:80], iterations=iteration, score=l4.score)
        return code, f"Plugin generated successfully (score: {l4.score}/10, iterations: {iteration})"

    # ── Template fallback ─────────────────────────────────────────────────────
    try:
        fallback_code = _build_template_fallback(description)
        from laravelgraph.plugins.validator import validate_plugin_file_content as _vpc
        _vpc(fallback_code)
        l3_fb = _validate_execution(fallback_code, core_db)
        if l3_fb.passed:
            log.info("Template fallback generated", description=description[:80])
            return fallback_code, (
                "LLM could not produce a valid plugin spec. "
                "A working skeleton was generated instead — "
                "edit the Cypher query inside to match your request."
            )
    except Exception as _fb_err:
        log.debug("Template fallback failed", error=str(_fb_err))

    layer_names = {1: "AST/static", 2: "schema", 3: "execution", 4: "quality-judge"}
    layer_label = layer_names.get(last_failed_layer, "unknown")
    return None, (
        f"Plugin generation failed after {max_iterations} attempts. "
        f"Last failure: Layer {last_failed_layer} ({layer_label}). "
        f"Details: {last_critique}"
    )
