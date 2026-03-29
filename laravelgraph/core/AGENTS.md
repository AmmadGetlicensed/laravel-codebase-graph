# AGENTS.md — laravelgraph/core/

## OVERVIEW

Graph database foundation. `schema.py` defines all DDL; `graph.py` executes it against KuzuDB and provides the write API; `registry.py` tracks indexed projects globally.

## STRUCTURE

```
core/
├── graph.py      # GraphDB class — KuzuDB wrapper, upsert_node/upsert_edge, Cypher exec
├── schema.py     # NODE_TYPES, REL_TYPES, node_id() helper
└── registry.py   # Registry — ~/.laravelgraph/repos.json, register/list/remove
```

## GraphDB API

```python
db.upsert_node(label: str, props: dict) -> None
db.upsert_edge(label: str, src_id: str, dst_id: str, props: dict = {}) -> None
db.execute(query: str, params: dict = {}) -> list[dict]
db.close() -> None
```

- `upsert_node` merges on the primary key (first property in `NODE_TYPES` entry = `node_id`).
- `upsert_edge` uses `MERGE` semantics — safe to call multiple times.
- `execute` returns list of dicts; used by MCP server tools for Cypher queries.

## Schema Convention

`NODE_TYPES`: list of `(label, [(prop_name, kuzu_type), ...])`. First prop is always `node_id STRING`.

`REL_TYPES`: list of `(label, [(from_label, to_label), ...], [(prop_name, kuzu_type), ...])`. Multi-FROM/TO supported (KuzuDB 0.11.3+).

`node_id(type: str, fqn: str) -> str` — deterministic ID, e.g. `node_id("class", "App\\Models\\User")`.

**KuzuDB table names:** raw label strings — `Class_` (note underscore, Python keyword collision), `CALLS`, `ROUTES_TO`, etc.

## ANTI-PATTERNS

- **Do not add node/edge types without updating `NODE_TYPES`/`REL_TYPES`** — DDL runs from these lists at startup.
- **Do not write raw Cypher `CREATE` statements in phases** — always use `upsert_node`/`upsert_edge`.
- **Do not assume `force_reinit=True` fully removes the `.kuzu` dir** — it drops tables via Cypher as a fallback when `shutil.rmtree` fails (KuzuDB lock files).
- **Do not open `GraphDB` in read/write mode while MCP server has it open** — KuzuDB allows one writer; use `read_only=True` for secondary connections.
