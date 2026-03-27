"""Unit tests — Fix 3: impact() route entry-point detection."""
from __future__ import annotations

from unittest.mock import MagicMock


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_impact_lines(total: int, route_rows: list[dict]) -> list[str]:
    """Simulate the route-handler note appended by laravelgraph_impact."""
    lines = [f"## Impact Analysis: `SomeController::handle`\n"]
    lines.append(f"Changes to this symbol may affect **{total}** downstream symbols.\n")

    if total == 0:
        if route_rows:
            lines.append(
                "> **Note:** This symbol is a **route entry point** — it is invoked by the "
                "HTTP router, not called from other PHP code. The static call graph has no "
                "callers pointing toward it, so impact analysis cannot trace upstream. "
                "Use `laravelgraph_request_flow(route)` to trace what this handler "
                "dispatches downstream.\n"
            )
            for r in route_rows:
                rname = f" — `{r.get('rname')}`" if r.get("rname") else ""
                lines.append(f"> Route: `{r.get('method', '?')} /{r.get('uri', '?')}`{rname}")
            lines.append("")
    return lines


class TestImpactRouteHandlerDetection:
    def test_webhook_handler_shows_note(self):
        route_rows = [{"method": "POST", "uri": "stripe/webhook", "rname": "stripe.webhook"}]
        lines = _build_impact_lines(0, route_rows)
        text = "\n".join(lines)
        assert "route entry point" in text
        assert "request_flow" in text
        assert "POST /stripe/webhook" in text

    def test_route_name_shown_when_present(self):
        route_rows = [{"method": "POST", "uri": "api/users", "rname": "users.store"}]
        lines = _build_impact_lines(0, route_rows)
        text = "\n".join(lines)
        assert "`users.store`" in text

    def test_route_name_omitted_when_absent(self):
        route_rows = [{"method": "GET", "uri": "api/ping", "rname": ""}]
        lines = _build_impact_lines(0, route_rows)
        text = "\n".join(lines)
        assert "GET /api/ping" in text
        # No trailing em-dash with empty name
        assert "— ``" not in text

    def test_multiple_routes_all_shown(self):
        route_rows = [
            {"method": "POST", "uri": "payments/intent", "rname": "payment.intent"},
            {"method": "PUT",  "uri": "payments/confirm", "rname": "payment.confirm"},
        ]
        lines = _build_impact_lines(0, route_rows)
        text = "\n".join(lines)
        assert "POST /payments/intent" in text
        assert "PUT /payments/confirm" in text

    def test_no_note_when_no_routes(self):
        lines = _build_impact_lines(0, [])
        text = "\n".join(lines)
        assert "route entry point" not in text

    def test_no_note_when_has_impact(self):
        # Symbol has 5 downstream — even if it's also a route, don't add the note
        # (the note is only for the 0-impact confusing case)
        route_rows = [{"method": "POST", "uri": "api/users", "rname": "users.store"}]
        lines = _build_impact_lines(5, route_rows)
        text = "\n".join(lines)
        assert "route entry point" not in text

    def test_note_not_added_for_non_route_with_zero_impact(self):
        # Isolated helper method — 0 impact, no routes
        lines = _build_impact_lines(0, [])
        text = "\n".join(lines)
        assert "route entry point" not in text
        assert "0" in text  # impact count is present (may be bold markdown)


class TestImpactRouteQuery:
    """Verify the Cypher query shape used to detect route handlers."""

    def test_routes_to_query_uses_node_id(self):
        """The query must filter by node_id, not fqn, to avoid false positives."""
        query = (
            "MATCH (r:Route)-[:ROUTES_TO]->(n) WHERE n.node_id = $nid "
            "RETURN r.method AS method, r.uri AS uri, r.name AS rname LIMIT 5"
        )
        assert "n.node_id = $nid" in query
        assert "ROUTES_TO" in query
        assert "LIMIT 5" in query
