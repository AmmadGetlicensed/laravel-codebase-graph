"""Plugin suggestion engine — detects domain patterns in the graph and recommends plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PluginRecipe:
    name: str           # e.g. "payment-lifecycle"
    slug: str           # e.g. "payment_lifecycle"
    title: str          # human-readable title
    description: str    # what the plugin would add
    signals: list[str]  # Cypher queries that return count > 0 = signal present
    min_signals: int    # minimum number of signals required to recommend
    tool_prefix: str    # suggested tool_prefix for the plugin


RECIPES: list[PluginRecipe] = [
    PluginRecipe(
        name="payment-lifecycle",
        slug="payment_lifecycle",
        title="Payment Lifecycle Auditor",
        description="Tracks payment state transitions, detects missing webhook handlers, flags unrecovered failed charges, maps gateway integration patterns.",
        signals=[
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'payment' OR toLower(m.name) CONTAINS 'invoice' OR toLower(m.name) CONTAINS 'charge' RETURN count(m) AS cnt",
            "MATCH (c:DatabaseColumn) WHERE c.name IN ['payment_gateway','payment_through','stripe_id','gateway','payment_method'] RETURN count(c) AS cnt",
            "MATCH (r:Route) WHERE toLower(r.uri) CONTAINS 'pay' OR toLower(r.uri) CONTAINS 'checkout' OR toLower(r.uri) CONTAINS 'webhook' RETURN count(r) AS cnt",
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'order' RETURN count(m) AS cnt",
        ],
        min_signals=2,
        tool_prefix="payment_",
    ),
    PluginRecipe(
        name="tenant-isolation",
        slug="tenant_isolation",
        title="Multi-Tenant Isolation Scanner",
        description="Verifies every model query is scoped by tenant, detects global queries that could leak cross-tenant data, maps tenant middleware coverage per route.",
        signals=[
            "MATCH (c:DatabaseColumn) WHERE c.name IN ['tenant_id','organization_id','company_id','account_id'] RETURN count(c) AS cnt",
            "MATCH (m:Middleware) WHERE toLower(m.name) CONTAINS 'tenant' RETURN count(m) AS cnt",
            "MATCH (t:DatabaseTable) WHERE EXISTS { MATCH (t)-[:HAS_COLUMN]->(c:DatabaseColumn) WHERE c.name IN ['tenant_id','organization_id'] } RETURN count(t) AS cnt",
        ],
        min_signals=1,
        tool_prefix="tenant_",
    ),
    PluginRecipe(
        name="booking-state-machine",
        slug="booking_state_machine",
        title="Booking State Machine Validator",
        description="Maps valid booking status transitions, detects routes that skip required states, flags race conditions on concurrent booking mutations.",
        signals=[
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'booking' OR toLower(m.name) CONTAINS 'appointment' OR toLower(m.name) CONTAINS 'reservation' RETURN count(m) AS cnt",
            "MATCH (c:DatabaseColumn) WHERE c.name = 'status' AND c.table_name CONTAINS 'book' RETURN count(c) AS cnt",
            "MATCH (t:DatabaseTable) WHERE t.name CONTAINS 'booking' OR t.name CONTAINS 'appointment' OR t.name CONTAINS 'slot' RETURN count(t) AS cnt",
        ],
        min_signals=1,
        tool_prefix="booking_",
    ),
    PluginRecipe(
        name="subscription-lifecycle",
        slug="subscription_lifecycle",
        title="Subscription & Billing Lifecycle",
        description="Tracks subscription state transitions, maps trial/grace period logic, detects missing cancellation and renewal handlers, surfaces churn risk signals.",
        signals=[
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'subscription' OR toLower(m.name) CONTAINS 'plan' RETURN count(m) AS cnt",
            "MATCH (c:DatabaseColumn) WHERE c.name IN ['trial_ends_at','subscribed_at','billing_cycle','plan_id','cancelled_at'] RETURN count(c) AS cnt",
            "MATCH (t:DatabaseTable) WHERE t.name CONTAINS 'subscription' OR t.name CONTAINS 'plan' RETURN count(t) AS cnt",
        ],
        min_signals=2,
        tool_prefix="subscription_",
    ),
    PluginRecipe(
        name="rbac-coverage",
        slug="rbac_coverage",
        title="Role & Permission Coverage Auditor",
        description="Maps which routes lack policy authorization, detects privilege escalation paths, verifies permission gates are consistent with role definitions.",
        signals=[
            "MATCH (m:EloquentModel) WHERE toLower(m.name) IN ['role','permission','userrole','rolepermission'] RETURN count(m) AS cnt",
            "MATCH (t:DatabaseTable) WHERE t.name IN ['roles','permissions','role_user','model_has_roles','model_has_permissions'] RETURN count(t) AS cnt",
            "MATCH (p:Policy) RETURN count(p) AS cnt",
        ],
        min_signals=2,
        tool_prefix="rbac_",
    ),
    PluginRecipe(
        name="audit-trail",
        slug="audit_trail",
        title="Audit Trail Coverage",
        description="Identifies models that mutate without audit logging, maps activitylog coverage gaps, surfaces write operations that bypass the audit trail.",
        signals=[
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'audit' OR toLower(m.name) CONTAINS 'activity' OR toLower(m.name) CONTAINS 'log' RETURN count(m) AS cnt",
            "MATCH (c:DatabaseColumn) WHERE c.name IN ['auditable_type','auditable_id','causer_type','subject_type'] RETURN count(c) AS cnt",
        ],
        min_signals=1,
        tool_prefix="audit_",
    ),
    PluginRecipe(
        name="feature-flags",
        slug="feature_flags",
        title="Feature Flag & Toggle Mapper",
        description="Maps all feature flag checks across the codebase, identifies dead flags (always on/off), detects flag usage without cleanup.",
        signals=[
            "MATCH (m:EloquentModel) WHERE toLower(m.name) CONTAINS 'feature' OR toLower(m.name) CONTAINS 'flag' OR toLower(m.name) CONTAINS 'toggle' RETURN count(m) AS cnt",
            "MATCH (c:DatabaseColumn) WHERE c.name CONTAINS 'enabled' OR c.name CONTAINS 'feature' OR c.name = 'active' RETURN count(c) AS cnt",
        ],
        min_signals=1,
        tool_prefix="flags_",
    ),
]


def detect_applicable_recipes(db: object) -> list[dict]:
    """Query the graph and return a list of recommended plugin recipes with evidence.

    Each result dict contains:
      - recipe: PluginRecipe
      - signals_matched: int
      - total_signals: int
      - evidence: list[str]  (human-readable descriptions of matched signals)

    Results are sorted by signals_matched descending.
    """
    results = []

    for recipe in RECIPES:
        signals_matched = 0
        evidence: list[str] = []

        for signal_query in recipe.signals:
            try:
                rows = db.execute(signal_query)  # type: ignore[union-attr]
                cnt = 0
                if rows:
                    first_row = rows[0]
                    if isinstance(first_row, dict):
                        cnt = first_row.get("cnt", 0) or 0
                    else:
                        # Fallback for non-dict row types
                        try:
                            cnt = list(first_row.values())[0] or 0
                        except Exception:
                            cnt = 0
                if cnt > 0:
                    signals_matched += 1
                    # Build a brief evidence string from the query
                    evidence.append(f"{cnt} match(es) for: {signal_query[:80].rstrip()}{'...' if len(signal_query) > 80 else ''}")
            except Exception:
                # Node type may not exist yet — treat as no signal
                pass

        if signals_matched >= recipe.min_signals:
            results.append({
                "recipe": recipe,
                "signals_matched": signals_matched,
                "total_signals": len(recipe.signals),
                "evidence": evidence,
            })

    results.sort(key=lambda r: r["signals_matched"], reverse=True)
    return results


def detect_feature_gaps(db: Any, meta_store: Any, plugins_dir: Path) -> list[dict]:
    """Find Feature nodes with no corresponding plugin.

    Queries phase-27 Feature clusters with ``symbol_count > 10`` that have
    no matching plugin slug on disk or in the meta store. Each gap is
    returned with a priority score proportional to its symbol count.

    Returns list of dicts: {slug, name, symbol_count, has_changes, score, source}.
    """
    try:
        existing_slugs = {m.name for m in meta_store.all()}
    except Exception:
        existing_slugs = set()

    try:
        rows = db.execute(
            "MATCH (f:Feature) WHERE f.symbol_count > 10 "
            "RETURN f.slug AS slug, f.name AS name, "
            "f.symbol_count AS symbol_count, f.has_changes AS has_changes "
            "ORDER BY f.symbol_count DESC"
        )
    except Exception:
        return []

    gaps: list[dict] = []
    for row in rows:
        slug = row.get("slug") or ""
        if not slug:
            continue
        if slug in existing_slugs:
            continue
        if (plugins_dir / f"{slug}.py").exists():
            continue
        symbol_count = row.get("symbol_count") or 0
        gaps.append({
            "slug": slug,
            "name": row.get("name") or slug,
            "symbol_count": symbol_count,
            "has_changes": bool(row.get("has_changes")),
            "score": min(symbol_count / 10.0, 10.0),
            "source": "feature_gap",
        })

    return gaps


def format_suggestions(results: list[dict]) -> str:
    """Return a markdown-formatted string with numbered recommendations, evidence, and suggested tool_prefix."""
    if not results:
        return (
            "No plugin recommendations found for this project.\n\n"
            "The suggestion engine looks for domain patterns (payments, tenancy, bookings, etc.).\n"
            "If your project has these features, run `laravelgraph analyze --phases 24,25,26` first\n"
            "to ensure database columns and tables are indexed."
        )

    lines = ["## Recommended Plugins\n"]
    for i, result in enumerate(results, 1):
        recipe: PluginRecipe = result["recipe"]
        matched = result["signals_matched"]
        total = result["total_signals"]
        evidence = result["evidence"]

        lines.append(f"### {i}. {recipe.title}")
        lines.append(f"**Plugin name:** `{recipe.name}`  |  **Signals matched:** {matched}/{total}\n")
        lines.append(f"{recipe.description}\n")

        if evidence:
            lines.append("**Evidence found:**")
            for ev in evidence:
                lines.append(f"- {ev}")
            lines.append("")

        lines.append(f"**Suggested tool_prefix:** `{recipe.tool_prefix}`")
        lines.append(f"**Scaffold command:** `laravelgraph plugin scaffold {recipe.name} --recipe {recipe.slug}`")
        lines.append("")

    return "\n".join(lines)
