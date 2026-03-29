"""Phase 27 — Feature Cluster Detection.

Auto-clusters routes, controllers, models, events, and jobs into high-level
Feature nodes using URI prefix grouping and namespace proximity heuristics.
No LLM required — purely structural analysis.

Algorithm
---------
1. Fetch all routes and group by first meaningful URI segment (skipping noise
   segments like "api", "v1", "{id}", etc.).
2. Create a Feature node per slug with name, slug, route_prefix, symbol_count,
   and entry_routes (JSON list).
3. Fetch EloquentModel, Class_ (controllers/services), Event, and Job nodes.
4. For each symbol, find the best matching Feature by:
     exact name match → contains match → prefix match (4+ chars)
5. Create BELONGS_TO_FEATURE edges with confidence:
     route=1.0, model=0.8, class=0.7, event/job=0.6
"""

from __future__ import annotations

import json
import re
from typing import Any

from laravelgraph.core.schema import node_id as make_node_id
from laravelgraph.logging import get_logger
from laravelgraph.pipeline.orchestrator import PipelineContext

logger = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# URI segments that don't identify a feature domain
_NOISE_SEGMENTS: frozenset[str] = frozenset({
    "api", "v1", "v2", "v3", "v4", "web", "admin", "dashboard",
    "app", "index", "list", "detail", "show", "create", "store",
    "edit", "update", "delete", "destroy",
})

# PHP namespace parts that don't identify a feature domain
_NOISE_NAMESPACES: frozenset[str] = frozenset({
    "app", "http", "controllers", "models", "services", "repositories",
    "providers", "observers", "policies", "events", "listeners",
    "jobs", "mail", "notifications", "console", "commands",
    "resources", "requests", "traits", "interfaces", "contracts",
    "database", "seeders", "factories",
})

# Brace-enclosed route parameters like {id}, {uuid}, {slug}
_BRACE_PARAM_RE = re.compile(r"^\{[^}]+\}$")

# Max routes in a single feature bucket before we stop grouping into "general"
_MAX_GENERAL_ROUTES = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uri_to_slug(uri: str) -> str:
    """Extract the primary slug from a URI path.

    Skips leading slashes, noise segments, and brace parameters.
    Returns an empty string if no meaningful segment is found.

    Examples:
        "/api/v1/orders/{id}" → "orders"
        "/admin/users"        → "users"
        "/health"             → "health"
    """
    parts = [p for p in uri.strip("/").split("/") if p]
    for part in parts:
        lower = part.lower()
        if _BRACE_PARAM_RE.match(part):
            continue
        if lower in _NOISE_SEGMENTS:
            continue
        # Normalise hyphens to underscores for the slug
        return lower.replace("-", "_")
    return ""


def _slug_to_name(slug: str) -> str:
    """Convert a slug to a human-readable feature name.

    Examples:
        "order_course" → "Order Course"
        "users"        → "Users"
    """
    return " ".join(word.capitalize() for word in slug.replace("-", "_").split("_"))


def _symbol_tokens(name: str) -> list[str]:
    """Split a PascalCase / snake_case / FQN symbol name into lowercase tokens.

    Used when matching a symbol's short name against feature slugs.

    Examples:
        "OrderCourse"            → ["order", "course"]
        "App\\Models\\UserOrder" → ["user", "order"]
        "order_courses"          → ["order", "courses"]
    """
    # Take the last segment of a namespace-qualified name
    short = name.split("\\")[-1].split("::")[-1]
    # Split on underscores first, then CamelCase
    parts: list[str] = []
    for chunk in short.split("_"):
        # CamelCase split
        words = re.sub(r"([A-Z][a-z]+)", r"_\1", chunk).strip("_").split("_")
        parts.extend(w.lower() for w in words if w)
    return [p for p in parts if p not in _NOISE_NAMESPACES]


def _best_slug_match(
    name: str,
    slugs: dict[str, str],
) -> tuple[str, str] | None:
    """Find the best feature slug match for a symbol name.

    Returns (slug, match_type) or None if no match found.

    Match priority:
      1. exact   — symbol tokens joined equals a slug exactly
      2. contains — slug is a substring of the joined tokens (or vice-versa)
      3. prefix   — 4+ character common prefix between the slug and any token
    """
    tokens = _symbol_tokens(name)
    if not tokens:
        return None

    tokens_joined = "_".join(tokens)

    # 1. Exact match
    if tokens_joined in slugs:
        return tokens_joined, "exact"
    for slug in slugs:
        if slug == tokens_joined:
            return slug, "exact"

    # 2. Contains match — slug fully contained in tokens string or vice-versa
    best_contains: str | None = None
    best_contains_len = 0
    for slug in slugs:
        if slug in tokens_joined or tokens_joined in slug:
            if len(slug) > best_contains_len:
                best_contains = slug
                best_contains_len = len(slug)
    if best_contains is not None:
        return best_contains, "contains"

    # 3. Prefix match (4+ chars) — check each token against each slug
    best_prefix: str | None = None
    best_prefix_len = 0
    for slug in slugs:
        for token in tokens:
            common_len = 0
            for c1, c2 in zip(slug, token):
                if c1 == c2:
                    common_len += 1
                else:
                    break
            if common_len >= 4 and common_len > best_prefix_len:
                best_prefix = slug
                best_prefix_len = common_len
    if best_prefix is not None:
        return best_prefix, "prefix"

    return None


# ── Main phase ────────────────────────────────────────────────────────────────

def run(ctx: PipelineContext) -> None:
    """Auto-cluster routes and symbols into Feature nodes."""
    db = ctx.db
    features_created = 0
    feature_links = 0

    # ── Step 1: Fetch all routes ───────────────────────────────────────────
    try:
        route_rows: list[dict[str, Any]] = db.execute(
            "MATCH (r:Route) RETURN r.node_id AS nid, r.uri AS uri, r.name AS rname"
        )
    except Exception as exc:
        logger.error("Failed to fetch Route nodes", error=str(exc))
        route_rows = []

    # ── Step 2: Group routes by primary slug ──────────────────────────────
    # slug → list of (route_node_id, uri)
    slug_routes: dict[str, list[tuple[str, str]]] = {}
    for row in route_rows:
        uri = row.get("uri") or ""
        nid = row.get("nid") or ""
        if not uri or not nid:
            continue
        slug = _uri_to_slug(uri)
        if not slug:
            slug = "general"
        slug_routes.setdefault(slug, []).append((nid, uri))

    # Drop "general" if it would balloon — better to leave ungrouped
    if "general" in slug_routes and len(slug_routes["general"]) > _MAX_GENERAL_ROUTES:
        logger.debug(
            "Dropping oversized 'general' feature bucket",
            route_count=len(slug_routes["general"]),
        )
        del slug_routes["general"]

    if not slug_routes:
        logger.info("No route slugs found — skipping feature clustering")
        ctx.stats["features_created"] = 0
        ctx.stats["feature_links"] = 0
        return

    # ── Step 3: Create Feature nodes ──────────────────────────────────────
    # slug → feature node_id
    slug_to_nid: dict[str, str] = {}

    for slug, route_list in slug_routes.items():
        feature_nid = make_node_id("feature", slug)
        slug_to_nid[slug] = feature_nid
        uris = [uri for _, uri in route_list]
        name = _slug_to_name(slug)

        try:
            db.upsert_node("Feature", {
                "node_id": feature_nid,
                "name": name,
                "slug": slug,
                "route_prefix": slug,
                "symbol_count": len(route_list),  # updated later
                "entry_routes": json.dumps(uris[:20]),  # cap stored list at 20
            })
            features_created += 1
        except Exception as exc:
            logger.warning("Failed to create Feature node", slug=slug, error=str(exc))
            continue

    logger.info("Feature nodes created", count=features_created)

    # ── Step 4a: Link routes to features ─────────────────────────────────
    for slug, route_list in slug_routes.items():
        feature_nid = slug_to_nid.get(slug)
        if not feature_nid:
            continue
        for route_nid, _uri in route_list:
            try:
                db.upsert_rel(
                    "BELONGS_TO_FEATURE",
                    from_label="Route",
                    from_id=route_nid,
                    to_label="Feature",
                    to_id=feature_nid,
                    props={"confidence": 1.0, "match_type": "exact"},
                )
                feature_links += 1
            except Exception as exc:
                logger.debug(
                    "Failed to link route to feature",
                    route_nid=route_nid,
                    feature_nid=feature_nid,
                    error=str(exc),
                )

    # ── Step 4b: Link EloquentModels ──────────────────────────────────────
    try:
        model_rows: list[dict[str, Any]] = db.execute(
            "MATCH (m:EloquentModel) RETURN m.node_id AS nid, m.name AS name, m.fqn AS fqn"
        )
    except Exception as exc:
        logger.warning("Failed to fetch EloquentModel nodes", error=str(exc))
        model_rows = []

    for row in model_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or row.get("fqn") or ""
        if not nid or not name:
            continue
        match = _best_slug_match(name, slug_to_nid)
        if match is None:
            continue
        slug, match_type = match
        feature_nid = slug_to_nid[slug]
        try:
            db.upsert_rel(
                "BELONGS_TO_FEATURE",
                from_label="EloquentModel",
                from_id=nid,
                to_label="Feature",
                to_id=feature_nid,
                props={"confidence": 0.8, "match_type": match_type},
            )
            feature_links += 1
        except Exception as exc:
            logger.debug(
                "Failed to link EloquentModel to feature",
                nid=nid,
                error=str(exc),
            )

    # ── Step 4c: Link Class_ nodes (controllers, services, etc.) ─────────
    try:
        class_rows: list[dict[str, Any]] = db.execute(
            "MATCH (c:Class_) RETURN c.node_id AS nid, c.name AS name, c.fqn AS fqn, "
            "c.laravel_role AS role"
        )
    except Exception as exc:
        logger.warning("Failed to fetch Class_ nodes", error=str(exc))
        class_rows = []

    for row in class_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or row.get("fqn") or ""
        if not nid or not name:
            continue
        match = _best_slug_match(name, slug_to_nid)
        if match is None:
            continue
        slug, match_type = match
        feature_nid = slug_to_nid[slug]
        try:
            db.upsert_rel(
                "BELONGS_TO_FEATURE",
                from_label="Class_",
                from_id=nid,
                to_label="Feature",
                to_id=feature_nid,
                props={"confidence": 0.7, "match_type": match_type},
            )
            feature_links += 1
        except Exception as exc:
            logger.debug(
                "Failed to link Class_ to feature",
                nid=nid,
                error=str(exc),
            )

    # ── Step 4d: Link Event nodes ─────────────────────────────────────────
    try:
        event_rows: list[dict[str, Any]] = db.execute(
            "MATCH (e:Event) RETURN e.node_id AS nid, e.name AS name, e.fqn AS fqn"
        )
    except Exception as exc:
        logger.warning("Failed to fetch Event nodes", error=str(exc))
        event_rows = []

    for row in event_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or row.get("fqn") or ""
        if not nid or not name:
            continue
        match = _best_slug_match(name, slug_to_nid)
        if match is None:
            continue
        slug, match_type = match
        feature_nid = slug_to_nid[slug]
        try:
            db.upsert_rel(
                "BELONGS_TO_FEATURE",
                from_label="Event",
                from_id=nid,
                to_label="Feature",
                to_id=feature_nid,
                props={"confidence": 0.6, "match_type": match_type},
            )
            feature_links += 1
        except Exception as exc:
            logger.debug(
                "Failed to link Event to feature",
                nid=nid,
                error=str(exc),
            )

    # ── Step 4e: Link Job nodes ───────────────────────────────────────────
    try:
        job_rows: list[dict[str, Any]] = db.execute(
            "MATCH (j:Job) RETURN j.node_id AS nid, j.name AS name, j.fqn AS fqn"
        )
    except Exception as exc:
        logger.warning("Failed to fetch Job nodes", error=str(exc))
        job_rows = []

    for row in job_rows:
        nid = row.get("nid") or ""
        name = row.get("name") or row.get("fqn") or ""
        if not nid or not name:
            continue
        match = _best_slug_match(name, slug_to_nid)
        if match is None:
            continue
        slug, match_type = match
        feature_nid = slug_to_nid[slug]
        try:
            db.upsert_rel(
                "BELONGS_TO_FEATURE",
                from_label="Job",
                from_id=nid,
                to_label="Feature",
                to_id=feature_nid,
                props={"confidence": 0.6, "match_type": match_type},
            )
            feature_links += 1
        except Exception as exc:
            logger.debug(
                "Failed to link Job to feature",
                nid=nid,
                error=str(exc),
            )

    # ── Step 5: Update symbol_count on Feature nodes ──────────────────────
    # Recompute symbol_count as total linked symbols (routes already counted
    # above; now add model/class/event/job edges).
    for slug, feature_nid in slug_to_nid.items():
        try:
            count_rows = db.execute(
                "MATCH (s)-[:BELONGS_TO_FEATURE]->(f:Feature {node_id: $nid}) "
                "RETURN count(*) AS cnt",
                {"nid": feature_nid},
            )
            total = count_rows[0].get("cnt", 0) if count_rows else 0
            db._conn.execute(
                "MATCH (f:Feature {node_id: $nid}) SET f.symbol_count = $cnt",
                parameters={"nid": feature_nid, "cnt": total},
            )
        except Exception as exc:
            logger.debug(
                "Failed to update symbol_count on Feature",
                slug=slug,
                error=str(exc),
            )

    ctx.stats["features_created"] = features_created
    ctx.stats["feature_links"] = feature_links

    logger.info(
        "Feature clustering complete",
        features_created=features_created,
        feature_links=feature_links,
    )
