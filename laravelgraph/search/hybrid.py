"""Hybrid search combining BM25 + vector + fuzzy with Reciprocal Rank Fusion."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from laravelgraph.logging import get_logger

if TYPE_CHECKING:
    from laravelgraph.config import SearchConfig
    from laravelgraph.core.graph import GraphDB

logger = get_logger(__name__)

try:
    from rank_bm25 import BM25Okapi  # type: ignore[import]
    _BM25_AVAILABLE = True
except ImportError:
    _BM25_AVAILABLE = False
    logger.warning("rank_bm25 not installed; BM25 search disabled. Install with: pip install rank-bm25")

try:
    from rapidfuzz import process as fuzz_process, fuzz  # type: ignore[import]
    _FUZZY_AVAILABLE = True
except ImportError:
    _FUZZY_AVAILABLE = False
    logger.warning("rapidfuzz not installed; fuzzy search disabled. Install with: pip install rapidfuzz")

try:
    from fastembed import TextEmbedding  # type: ignore[import]
    import numpy as np  # type: ignore[import]
    _VECTOR_AVAILABLE = True
except ImportError:
    _VECTOR_AVAILABLE = False
    logger.warning("fastembed/numpy not installed; vector search disabled. Install with: pip install fastembed")

# Node labels that are indexed for search
_SEARCHABLE_LABELS = [
    "Class_", "Method", "Function_", "Route",
    "EloquentModel", "Command", "Job", "Event",
    "Listener", "Middleware", "ServiceProvider",
    "BladeTemplate", "FormRequest", "Resource",
]

# RRF constant
_RRF_K = 60


@dataclass
class SearchResult:
    """A single search result with multi-strategy scores."""

    node_id: str
    label: str           # node type (Class_, Method, etc.)
    name: str
    fqn: str
    file_path: str
    line: int
    laravel_role: str
    score: float         # final RRF-merged score
    bm25_score: float = 0.0
    vector_score: float = 0.0
    fuzzy_score: float = 0.0
    snippet: str = ""    # short human-readable description
    community_id: int = -1


class HybridSearch:
    """Hybrid search over the LaravelGraph knowledge graph.

    Combines:
    - BM25 full-text search on symbol names and docblocks
    - Vector similarity search on stored embeddings
    - Fuzzy name matching via rapidfuzz
    - Reciprocal Rank Fusion (RRF) to merge result lists
    """

    def __init__(self, db: "GraphDB", config: "SearchConfig") -> None:
        self._db = db
        self._config = config

        # In-memory indexes (built lazily or on demand)
        self._bm25_index: Any = None
        self._bm25_corpus: list[str] = []
        self._bm25_node_ids: list[str] = []

        self._fuzzy_names: list[str] = []
        self._fuzzy_node_ids: list[str] = []

        self._node_meta: dict[str, dict[str, Any]] = {}  # node_id → metadata

        self._embedder: Any = None
        self._embedding_node_ids: list[str] = []
        self._embeddings: Any = None  # np.ndarray if available

        self._index_built = False

    # ── Public API ────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        limit: int = 20,
        file_filter: str | None = None,
        role_filter: str | None = None,
    ) -> list[SearchResult]:
        """Run hybrid search and return RRF-merged results.

        Args:
            query: Free-text search query.
            limit: Maximum number of results to return.
            file_filter: Optional glob-style filter on file_path (substring match).
            role_filter: Optional laravel_role value to restrict results.
        """
        if not self._index_built:
            self.build_index()

        bm25_results = self._bm25_search(query) if _BM25_AVAILABLE and self._bm25_index else []
        vector_results = self._vector_search(query) if _VECTOR_AVAILABLE and self._embeddings is not None else []
        fuzzy_results = self._fuzzy_search(query) if _FUZZY_AVAILABLE and self._fuzzy_names else []

        weights = [
            self._config.bm25_weight,
            self._config.vector_weight,
            self._config.fuzzy_weight,
        ]

        merged = self._rrf_merge(bm25_results, vector_results, fuzzy_results, weights=weights)

        # Apply boosts
        boosted: list[tuple[str, float]] = []
        for node_id, score in merged:
            meta = self._node_meta.get(node_id, {})
            file_path = meta.get("file_path", "")
            adjusted = score

            if "vendor" in file_path:
                adjusted *= 0.1
            elif "Test" in file_path or "test" in file_path or "spec" in file_path.lower():
                adjusted *= self._config.test_file_penalty
            elif file_path:
                adjusted *= self._config.source_boost

            boosted.append((node_id, adjusted))

        boosted.sort(key=lambda x: x[1], reverse=True)

        results: list[SearchResult] = []
        for node_id, final_score in boosted[:limit]:
            meta = self._node_meta.get(node_id, {})

            if file_filter and file_filter not in meta.get("file_path", ""):
                continue
            if role_filter and meta.get("laravel_role", "") != role_filter:
                continue

            # Fetch individual strategy scores
            b_score = next((s for nid, s in bm25_results if nid == node_id), 0.0)
            v_score = next((s for nid, s in vector_results if nid == node_id), 0.0)
            f_score = next((s for nid, s in fuzzy_results if nid == node_id), 0.0)

            results.append(SearchResult(
                node_id=node_id,
                label=meta.get("label", ""),
                name=meta.get("name", ""),
                fqn=meta.get("fqn", ""),
                file_path=meta.get("file_path", ""),
                line=meta.get("line_start", 0),
                laravel_role=meta.get("laravel_role", ""),
                score=final_score,
                bm25_score=b_score,
                vector_score=v_score,
                fuzzy_score=f_score,
                snippet=meta.get("snippet", ""),
                community_id=meta.get("community_id", -1),
            ))

        return results

    # ── Search strategies ─────────────────────────────────────────────────────

    def _bm25_search(self, query: str) -> list[tuple[str, float]]:
        """Return list of (node_id, score) from BM25."""
        if not _BM25_AVAILABLE or self._bm25_index is None:
            return []
        if not self._bm25_corpus:
            return []

        try:
            tokenized_query = query.lower().split()
            scores = self._bm25_index.get_scores(tokenized_query)

            results: list[tuple[str, float]] = []
            for idx, score in enumerate(scores):
                if score > 0 and idx < len(self._bm25_node_ids):
                    results.append((self._bm25_node_ids[idx], float(score)))

            # Normalize to 0–1
            if results:
                max_score = max(s for _, s in results)
                if max_score > 0:
                    results = [(nid, s / max_score) for nid, s in results]

            results.sort(key=lambda x: x[1], reverse=True)
            return results
        except Exception as exc:
            logger.warning("BM25 search failed", error=str(exc))
            return []

    def _vector_search(self, query: str) -> list[tuple[str, float]]:
        """Return list of (node_id, score) from vector similarity."""
        if not _VECTOR_AVAILABLE or self._embedder is None or self._embeddings is None:
            return []

        try:
            query_embedding = list(self._embedder.embed([query]))[0]
            # Cosine similarity
            norms = np.linalg.norm(self._embeddings, axis=1)
            q_norm = np.linalg.norm(query_embedding)
            if q_norm == 0:
                return []
            similarities = self._embeddings.dot(query_embedding) / (norms * q_norm + 1e-10)

            results: list[tuple[str, float]] = []
            for idx, sim in enumerate(similarities):
                if idx < len(self._embedding_node_ids) and sim > 0:
                    results.append((self._embedding_node_ids[idx], float(sim)))

            results.sort(key=lambda x: x[1], reverse=True)
            return results
        except Exception as exc:
            logger.warning("Vector search failed", error=str(exc))
            return []

    def _fuzzy_search(self, query: str) -> list[tuple[str, float]]:
        """Return list of (node_id, score) from fuzzy name matching."""
        if not _FUZZY_AVAILABLE or not self._fuzzy_names:
            return []

        try:
            threshold = int(self._config.fuzzy_threshold * 100)
            matches = fuzz_process.extract(
                query,
                self._fuzzy_names,
                scorer=fuzz.WRatio,
                limit=50,
                score_cutoff=threshold,
            )

            results: list[tuple[str, float]] = []
            for match_name, score, idx in matches:
                if idx < len(self._fuzzy_node_ids):
                    node_id = self._fuzzy_node_ids[idx]
                    normalized = score / 100.0
                    results.append((node_id, normalized))

            results.sort(key=lambda x: x[1], reverse=True)
            return results
        except Exception as exc:
            logger.warning("Fuzzy search failed", error=str(exc))
            return []

    # ── RRF Merging ───────────────────────────────────────────────────────────

    def _rrf_merge(
        self,
        *result_lists: list[tuple[str, float]],
        weights: list[float],
    ) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion with per-strategy weights.

        score(d) = sum_i( weight_i / (k + rank_i(d)) )
        """
        rrf_scores: dict[str, float] = {}

        for strategy_idx, result_list in enumerate(result_lists):
            w = weights[strategy_idx] if strategy_idx < len(weights) else 1.0
            for rank, (node_id, _score) in enumerate(result_list, start=1):
                rrf_scores[node_id] = rrf_scores.get(node_id, 0.0) + w / (_RRF_K + rank)

        merged = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        return merged

    # ── Index Building ────────────────────────────────────────────────────────

    def build_index(self) -> None:
        """Build in-memory BM25 and fuzzy indexes from the graph."""
        logger.info("Building search index...")

        self._bm25_corpus = []
        self._bm25_node_ids = []
        self._fuzzy_names = []
        self._fuzzy_node_ids = []
        self._node_meta = {}
        embedding_rows: list[tuple[str, list[float]]] = []

        # Per-label field lists — only query fields that exist in the schema
        _LABEL_FIELDS: dict[str, list[str]] = {
            "Class_":          ["node_id", "name", "fqn", "file_path", "line_start", "laravel_role", "community_id", "embedding"],
            "Method":          ["node_id", "name", "fqn", "file_path", "line_start", "laravel_role", "community_id", "docblock", "embedding"],
            "Function_":       ["node_id", "name", "fqn", "file_path", "line_start", "embedding"],
            "Route":           ["node_id", "name", "uri", "http_method", "controller_fqn", "route_file"],
            "EloquentModel":   ["node_id", "name", "fqn", "file_path"],
            "Command":         ["node_id", "name", "fqn", "file_path", "signature", "description"],
            "Job":             ["node_id", "name", "fqn", "file_path"],
            "Event":           ["node_id", "name", "fqn", "file_path"],
            "Listener":        ["node_id", "name", "fqn", "file_path"],
            "Middleware":      ["node_id", "name", "fqn", "file_path"],
            "ServiceProvider": ["node_id", "name", "fqn", "file_path"],
            "BladeTemplate":   ["node_id", "name", "file_path", "relative_path"],
            "FormRequest":     ["node_id", "name", "fqn", "file_path"],
            "Resource":        ["node_id", "name", "fqn", "file_path"],
        }

        for label in _SEARCHABLE_LABELS:
            fields = _LABEL_FIELDS.get(label, ["node_id", "name", "file_path"])
            select_clause = ", ".join(f"n.{f} AS {f}" for f in fields)
            try:
                rows = self._db.execute(f"MATCH (n:{label}) RETURN {select_clause}")
            except Exception as exc:
                logger.debug("Index query failed", label=label, error=str(exc))
                continue

            for row in rows:
                node_id = row.get("node_id") or ""
                if not node_id:
                    continue

                name = row.get("name") or ""
                # Route uses controller_fqn; others use fqn
                fqn = row.get("fqn") or row.get("controller_fqn") or ""
                # Route uses route_file; BladeTemplate has no file_path
                file_path = row.get("file_path") or row.get("route_file") or ""
                docblock = row.get("docblock") or row.get("description") or row.get("signature") or ""
                embedding = row.get("embedding")
                laravel_role = row.get("laravel_role") or row.get("http_method") or ""
                community_id = row.get("community_id") or -1
                line_start = row.get("line_start") or 0
                # Add URI to Route corpus
                extra_text = row.get("uri") or row.get("relative_path") or ""

                # Build corpus text for BM25
                corpus_text = " ".join(filter(None, [
                    name,
                    fqn.replace("\\", " ").replace("::", " "),
                    docblock[:200] if docblock else "",
                    laravel_role,
                    extra_text,
                ]))
                self._bm25_corpus.append(corpus_text.lower())
                self._bm25_node_ids.append(node_id)

                # Fuzzy name index
                display_name = name or fqn.split("\\")[-1] if fqn else node_id
                self._fuzzy_names.append(display_name)
                self._fuzzy_node_ids.append(node_id)

                # Collect embeddings for vector search
                if embedding and isinstance(embedding, list) and len(embedding) > 0:
                    embedding_rows.append((node_id, embedding))
                    self._embedding_node_ids.append(node_id)

                # Snippet: human-readable description
                if laravel_role:
                    snippet = f"[{laravel_role}] {fqn or name}"
                else:
                    snippet = fqn or name

                self._node_meta[node_id] = {
                    "label": label,
                    "name": name,
                    "fqn": fqn,
                    "file_path": file_path,
                    "line_start": line_start,
                    "laravel_role": laravel_role,
                    "community_id": community_id,
                    "snippet": snippet,
                }

        # Build BM25 index
        if _BM25_AVAILABLE and self._bm25_corpus:
            try:
                tokenized = [doc.split() for doc in self._bm25_corpus]
                self._bm25_index = BM25Okapi(tokenized)
                logger.info("BM25 index built", documents=len(tokenized))
            except Exception as exc:
                logger.warning("BM25 index build failed", error=str(exc))

        # Build vector matrix
        if _VECTOR_AVAILABLE and embedding_rows:
            try:
                import numpy as np
                vectors = [v for _, v in embedding_rows]
                self._embedding_node_ids = [nid for nid, _ in embedding_rows]
                self._embeddings = np.array(vectors, dtype=np.float32)
                # Load embedder for query-time embedding
                model_name = getattr(self._config, "embedding_model", "BAAI/bge-small-en-v1.5")
                self._embedder = TextEmbedding(model_name_or_path=model_name)
                logger.info("Vector index built", vectors=len(vectors))
            except Exception as exc:
                logger.warning("Vector index build failed", error=str(exc))

        self._index_built = True
        logger.info(
            "Search index ready",
            bm25_docs=len(self._bm25_corpus),
            fuzzy_names=len(self._fuzzy_names),
            embeddings=len(self._embedding_node_ids),
        )

    # ── Grouping ──────────────────────────────────────────────────────────────

    def group_by_flow(self, results: list[SearchResult]) -> dict[str, list[SearchResult]]:
        """Group results by their Process (execution flow).

        Returns a dict mapping process node_id/name → list of SearchResults
        that appear as steps in that process.
        """
        groups: dict[str, list[SearchResult]] = {}
        default_group = "__ungrouped__"

        node_ids = [r.node_id for r in results]
        if not node_ids:
            return {default_group: results}

        # Query which processes each node belongs to
        node_process_map: dict[str, str] = {}
        for node_id in node_ids:
            try:
                rows = self._db.execute(
                    "MATCH (n)-[:STEP_IN_PROCESS]->(p:Process) WHERE n.node_id = $nid "
                    "RETURN p.name AS process_name, p.node_id AS process_nid LIMIT 1",
                    {"nid": node_id},
                )
                if rows:
                    process_label = rows[0].get("process_name") or rows[0].get("process_nid") or default_group
                    node_process_map[node_id] = process_label
            except Exception:
                pass

        for result in results:
            process_label = node_process_map.get(result.node_id, default_group)
            groups.setdefault(process_label, []).append(result)

        return groups


# ── Module-level convenience function ─────────────────────────────────────────

def search(
    query: str,
    db: "GraphDB",
    config: "SearchConfig",
    limit: int = 20,
) -> list[SearchResult]:
    """Module-level convenience wrapper around HybridSearch.

    Args:
        query: Free-text search query.
        db: GraphDB instance.
        config: SearchConfig instance.
        limit: Maximum number of results.

    Returns:
        List of SearchResult sorted by descending relevance score.
    """
    searcher = HybridSearch(db, config)
    searcher.build_index()
    return searcher.search(query, limit=limit)
