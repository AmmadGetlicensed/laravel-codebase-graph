"""Phase 12 — Vector Embedding Generation.

Generate 384-dimensional vector embeddings for Class_, Method, and Function_
nodes using the BAAI/bge-small-en-v1.5 model via fastembed. These embeddings
power semantic search across the codebase.
"""

from __future__ import annotations

import json
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)


def _build_class_text(row: dict[str, Any]) -> str:
    """Build a text representation for a Class_ node."""
    parts = []
    name = row.get("name") or ""
    fqn = row.get("fqn") or ""
    role = row.get("laravel_role") or ""

    if name:
        parts.append(name)
    if fqn and fqn != name:
        parts.append(fqn)
    if role:
        parts.append(role)

    return " ".join(p for p in parts if p)


def _build_method_text(row: dict[str, Any]) -> str:
    """Build a text representation for a Method node."""
    parts = []
    name = row.get("name") or ""
    fqn = row.get("fqn") or ""
    docblock = row.get("docblock") or ""
    param_types = row.get("param_types") or ""
    return_type = row.get("return_type") or ""
    role = row.get("laravel_role") or ""

    if name:
        parts.append(name)

    # Extract class name from FQN (ClassName::methodName)
    if fqn and "::" in fqn:
        class_part = fqn.split("::")[0].split("\\")[-1]
        parts.append(class_part)

    if role:
        parts.append(role)

    if return_type:
        parts.append(return_type)

    # Parse param types from JSON
    if param_types:
        try:
            parsed = json.loads(param_types)
            if isinstance(parsed, list):
                parts.extend(str(p) for p in parsed if p)
        except (json.JSONDecodeError, TypeError):
            parts.append(param_types)

    if docblock:
        # Truncate docblock to avoid overwhelming the embedding
        clean_doc = docblock.strip().replace("\n", " ")[:200]
        parts.append(clean_doc)

    return " ".join(p for p in parts if p)


def _build_function_text(row: dict[str, Any]) -> str:
    """Build a text representation for a Function_ node."""
    parts = []
    name = row.get("name") or ""
    fqn = row.get("fqn") or ""
    return_type = row.get("return_type") or ""

    if name:
        parts.append(name)
    if fqn and fqn != name:
        parts.append(fqn)
    if return_type:
        parts.append(return_type)

    return " ".join(p for p in parts if p)


def _update_embedding(db: Any, label: str, node_id_val: str, embedding: list[float]) -> None:
    """Write embedding vector back to the node."""
    try:
        inner = ", ".join(str(x) for x in embedding)
        db._conn.execute(
            f"MATCH (n:{label} {{node_id: $nid}}) SET n.embedding = [{inner}]",
            parameters={"nid": node_id_val},
        )
    except Exception as exc:
        logger.debug("Failed to update embedding", label=label, nid=node_id_val, error=str(exc))


def run(ctx: PipelineContext) -> None:
    """Generate vector embeddings for Class_, Method, and Function_ nodes."""
    model_name = ctx.config.embedding.model
    batch_size = ctx.config.embedding.batch_size

    try:
        from fastembed import TextEmbedding
        model = TextEmbedding(model_name=model_name)
    except ImportError:
        logger.warning("fastembed not available; skipping embeddings")
        return
    except Exception as exc:
        logger.warning("Failed to load embedding model", model=model_name, error=str(exc))
        return

    db = ctx.db
    embeddings_generated = 0

    # Collect all items to embed: (label, node_id, text)
    items: list[tuple[str, str, str]] = []

    # Class_ nodes
    try:
        rows = db.execute(
            "MATCH (c:Class_) RETURN c.node_id AS nid, c.name AS name, "
            "c.fqn AS fqn, c.laravel_role AS laravel_role"
        )
        for row in rows:
            nid = row.get("nid") or ""
            if not nid:
                continue
            text = _build_class_text(row)
            if text:
                items.append(("Class_", nid, text))
    except Exception as exc:
        logger.warning("Failed to fetch Class_ nodes for embedding", error=str(exc))

    # Method nodes
    try:
        rows = db.execute(
            "MATCH (m:Method) RETURN m.node_id AS nid, m.name AS name, m.fqn AS fqn, "
            "m.docblock AS docblock, m.param_types AS param_types, "
            "m.return_type AS return_type, m.laravel_role AS laravel_role"
        )
        for row in rows:
            nid = row.get("nid") or ""
            if not nid:
                continue
            text = _build_method_text(row)
            if text:
                items.append(("Method", nid, text))
    except Exception as exc:
        logger.warning("Failed to fetch Method nodes for embedding", error=str(exc))

    # Function_ nodes
    try:
        rows = db.execute(
            "MATCH (f:Function_) RETURN f.node_id AS nid, f.name AS name, "
            "f.fqn AS fqn, f.return_type AS return_type"
        )
        for row in rows:
            nid = row.get("nid") or ""
            if not nid:
                continue
            text = _build_function_text(row)
            if text:
                items.append(("Function_", nid, text))
    except Exception as exc:
        logger.warning("Failed to fetch Function_ nodes for embedding", error=str(exc))

    total_items = len(items)
    logger.info("Generating embeddings", total_items=total_items, batch_size=batch_size)

    # Process in batches
    for batch_start in range(0, total_items, batch_size):
        batch = items[batch_start : batch_start + batch_size]
        labels = [item[0] for item in batch]
        nids = [item[1] for item in batch]
        texts = [item[2] for item in batch]

        try:
            embeddings = list(model.embed(texts))
        except Exception as exc:
            logger.warning(
                "Embedding generation failed for batch",
                batch_start=batch_start,
                error=str(exc),
            )
            continue

        for label, nid, embedding in zip(labels, nids, embeddings):
            try:
                vec = list(embedding.tolist() if hasattr(embedding, "tolist") else embedding)
                _update_embedding(db, label, nid, vec)
                embeddings_generated += 1
            except Exception as exc:
                logger.debug("Failed to store embedding", nid=nid, error=str(exc))

        logger.debug(
            "Embedding batch complete",
            batch_start=batch_start,
            batch_size=len(batch),
            total_so_far=embeddings_generated,
        )

    # Explicitly release the ONNX runtime and node list to free 500MB+ of RAM
    # before the remaining pipeline phases run.
    del items
    del model
    try:
        import gc
        gc.collect()
    except Exception:
        pass

    ctx.stats["embeddings_generated"] = embeddings_generated
    logger.info("Embedding generation complete", embeddings_generated=embeddings_generated)
