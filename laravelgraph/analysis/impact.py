"""Impact / blast radius analysis.

Given a symbol node_id, traces all downstream affected symbols through:
- CALLS edges (direct callers)
- USES_TYPE edges (type consumers)
- HAS_RELATIONSHIP edges (Eloquent relationships)
- APPLIES_MIDDLEWARE edges
- LISTENS_TO edges
- COUPLED_WITH edges (git co-change)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from laravelgraph.core.graph import GraphDB
from laravelgraph.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ImpactResult:
    root_node_id: str
    total: int
    by_depth: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    route_impacts: list[dict[str, Any]] = field(default_factory=list)
    model_impacts: list[dict[str, Any]] = field(default_factory=list)
    event_impacts: list[dict[str, Any]] = field(default_factory=list)
    test_suggestions: list[str] = field(default_factory=list)


class ImpactAnalyzer:
    """BFS traversal of the graph to find all impacted symbols."""

    # Edge types to traverse (incoming to current node = affected by changes)
    IMPACT_EDGE_TYPES = [
        ("CALLS", "source"),           # things that call this
        ("USES_TYPE", "source"),        # things that use this type
        ("IMPLEMENTS_INTERFACE", "source"),  # implementors
        ("EXTENDS_CLASS", "source"),    # subclasses
        ("INJECTS", "source"),          # things that inject this
        ("VALIDATES_WITH", "source"),   # controllers that validate with this FormRequest
        ("TRANSFORMS_WITH", "source"),  # controllers that use this Resource
        ("RENDERS_TEMPLATE", "source"), # controllers that render this blade
    ]

    def __init__(self, db: GraphDB) -> None:
        self._db = db

    def analyze(self, node_id: str, depth: int = 3) -> ImpactResult:
        """BFS from node_id, returning all impacted symbols by depth."""
        visited: set[str] = {node_id}
        by_depth: dict[int, list[dict[str, Any]]] = {}
        queue: deque[tuple[str, int]] = deque([(node_id, 0)])

        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            # Find all nodes that depend on the current node
            dependents = self._find_dependents(current_id)

            for dep in dependents:
                dep_id = dep.get("node_id", "")
                if not dep_id or dep_id in visited:
                    continue
                visited.add(dep_id)
                d = current_depth + 1
                by_depth.setdefault(d, []).append(dep)
                queue.append((dep_id, d))

        total = sum(len(v) for v in by_depth.values())

        # Laravel-specific impact analysis
        route_impacts = self._find_route_impacts(node_id)
        model_impacts = self._find_model_impacts(node_id)
        event_impacts = self._find_event_impacts(node_id)

        return ImpactResult(
            root_node_id=node_id,
            total=total,
            by_depth=by_depth,
            route_impacts=route_impacts,
            model_impacts=model_impacts,
            event_impacts=event_impacts,
        )

    def _find_dependents(self, node_id: str) -> list[dict[str, Any]]:
        """Find all nodes with an edge pointing TO node_id."""
        results = []

        # CALLS: who calls this method?
        for query in [
            ("MATCH (caller)-[r:CALLS]->(target) WHERE target.node_id = $id "
             "RETURN caller.node_id AS node_id, caller.fqn AS fqn, caller.file_path AS file_path, "
             "labels(caller)[0] AS label, r.confidence AS confidence"),
            ("MATCH (user)-[r:USES_TYPE]->(target) WHERE target.node_id = $id "
             "RETURN user.node_id AS node_id, user.fqn AS fqn, user.file_path AS file_path, "
             "labels(user)[0] AS label, 0.9 AS confidence"),
            ("MATCH (sub)-[:EXTENDS_CLASS]->(parent) WHERE parent.node_id = $id "
             "RETURN sub.node_id AS node_id, sub.fqn AS fqn, sub.file_path AS file_path, "
             "labels(sub)[0] AS label, 1.0 AS confidence"),
            ("MATCH (impl)-[:IMPLEMENTS_INTERFACE]->(iface) WHERE iface.node_id = $id "
             "RETURN impl.node_id AS node_id, impl.fqn AS fqn, impl.file_path AS file_path, "
             "labels(impl)[0] AS label, 1.0 AS confidence"),
        ]:
            try:
                rows = self._db.execute(query, {"id": node_id})
                results.extend(rows)
            except Exception as e:
                logger.debug("Impact query failed", query=query[:60], error=str(e))

        return results

    def _find_route_impacts(self, node_id: str) -> list[dict[str, Any]]:
        """Find routes that use this symbol (directly or via middleware)."""
        routes = []
        try:
            rows = self._db.execute(
                "MATCH (r:Route)-[:ROUTES_TO]->(target) WHERE target.node_id = $id "
                "RETURN r.http_method AS method, r.uri AS uri, r.name AS name",
                {"id": node_id},
            )
            routes.extend(rows)
        except Exception:
            pass
        return routes

    def _find_model_impacts(self, node_id: str) -> list[dict[str, Any]]:
        """Find Eloquent models related to this symbol."""
        impacts = []
        try:
            rows = self._db.execute(
                "MATCH (m:EloquentModel)-[r:HAS_RELATIONSHIP]->(related) WHERE m.node_id = $id OR related.node_id = $id "
                "RETURN m.fqn AS fqn, r.relationship_type AS relationship",
                {"id": node_id},
            )
            impacts.extend(rows)
        except Exception:
            pass
        return impacts

    def _find_event_impacts(self, node_id: str) -> list[dict[str, Any]]:
        """Find events/listeners affected by this symbol."""
        impacts = []
        try:
            rows = self._db.execute(
                "MATCH (l:Listener)-[:LISTENS_TO]->(e:Event) WHERE e.node_id = $id OR l.node_id = $id "
                "RETURN l.name AS listener, e.name AS event",
                {"id": node_id},
            )
            impacts.extend(rows)
        except Exception:
            pass
        return impacts
