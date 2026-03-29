"""Tests for the domain-anchored plugin auto-generation system.

These tests cover the core logic without requiring a live LLM or KuzuDB:
  - Domain token extraction
  - Domain anchor resolution (feature match + token scan paths)
  - Anchor → prompt formatting
  - Multi-tool spec validation and auto-fix
  - Plugin code assembly (summary, query, store tools)
  - Template fallback
  - Full generate_plugin() flow with mocks
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from laravelgraph.plugins.generator import (
    _assemble_plugin_code,
    _build_summary_text,
    _build_template_fallback,
    _categorise_symbols,
    _description_tokens,
    _expand_event_listeners,
    _format_anchors_for_prompt,
    _resolve_domain_anchors,
    _try_feature_match,
    _try_token_scan,
    generate_plugin,
)
from laravelgraph.plugins.validator import validate_plugin_file_content


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_anchors() -> dict:
    return {
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


class _MockDB:
    """Configurable mock database for testing domain resolution."""

    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        # Return fixture data keyed by a substring of the query
        for key, rows in self._data.items():
            if key.lower() in query.lower():
                return list(rows)
        return []

    def core(self):
        return self

    def plugin(self):
        return self

    def upsert_plugin_node(self, *a, **kw):
        pass


# ── _description_tokens ───────────────────────────────────────────────────────

class TestDescriptionTokens:
    def test_extracts_domain_words(self):
        tokens = _description_tokens("I need to understand the order lifecycle")
        assert "order" in tokens
        assert "lifecycle" in tokens

    def test_strips_stop_words(self):
        tokens = _description_tokens("show me all the routes and list the models")
        assert "show" not in tokens
        assert "list" not in tokens
        assert "all" not in tokens
        assert "the" not in tokens

    def test_strips_short_words(self):
        tokens = _description_tokens("go to api and do it")
        assert "go" not in tokens
        assert "do" not in tokens
        assert "it" not in tokens

    def test_caps_at_ten_tokens(self):
        desc = "reservation booking cancellation refund processing status timeline history audit notification"
        tokens = _description_tokens(desc)
        assert len(tokens) <= 10

    def test_empty_description(self):
        assert _description_tokens("") == []

    def test_only_stop_words(self):
        assert _description_tokens("show me all and the") == []


# ── _categorise_symbols ───────────────────────────────────────────────────────

class TestCategoriseSymbols:
    def test_routes_go_to_routes(self):
        anchors = _empty_anchors()
        _categorise_symbols([
            {"lbl": "Route", "name": "index", "http_method": "GET", "uri": "/orders", "action": "OrderController@index"}
        ], anchors)
        assert len(anchors["routes"]) == 1
        assert anchors["routes"][0]["uri"] == "/orders"

    def test_models_go_to_models(self):
        anchors = _empty_anchors()
        _categorise_symbols([
            {"lbl": "EloquentModel", "name": "Order", "db_table": "orders"}
        ], anchors)
        assert anchors["models"][0] == {"name": "Order", "table": "orders"}

    def test_events_go_to_events(self):
        anchors = _empty_anchors()
        _categorise_symbols([{"lbl": "Event", "name": "OrderPlaced"}], anchors)
        assert anchors["events"][0]["name"] == "OrderPlaced"
        assert anchors["events"][0]["listeners"] == []

    def test_jobs_go_to_jobs(self):
        anchors = _empty_anchors()
        _categorise_symbols([{"lbl": "Job", "name": "SendReceipt", "queue": "mail"}], anchors)
        assert anchors["jobs"][0] == {"name": "SendReceipt", "queue": "mail"}

    def test_deduplication(self):
        anchors = _empty_anchors()
        row = {"lbl": "EloquentModel", "name": "Order", "db_table": "orders"}
        _categorise_symbols([row, row], anchors)
        assert len(anchors["models"]) == 1

    def test_unknown_label_ignored(self):
        anchors = _empty_anchors()
        _categorise_symbols([{"lbl": "UnknownType", "name": "Foo"}], anchors)
        assert not anchors["routes"]
        assert not anchors["models"]


# ── _try_feature_match ────────────────────────────────────────────────────────

class TestTryFeatureMatch:
    def test_matches_feature_by_token(self):
        db = _MockDB({
            "MATCH (f:Feature)": [
                {"name": "Inventory Management", "slug": "inventory", "nid": "feat:inventory"},
                {"name": "User Auth", "slug": "auth", "nid": "feat:auth"},
            ],
            "BELONGS_TO_FEATURE": [
                {"lbl": "Route", "name": "index", "http_method": "GET", "uri": "/inventory", "action": "InventoryController@index", "fqn": None, "db_table": None, "queue": None},
                {"lbl": "EloquentModel", "name": "Product", "db_table": "products", "http_method": None, "uri": None, "action": None, "fqn": None, "queue": None},
            ],
        })
        anchors = _empty_anchors()
        _try_feature_match(db, ["inventory", "stock"], anchors)

        assert anchors["feature_name"] == "Inventory Management"
        assert anchors["feature_slug"] == "inventory"
        assert anchors["matched_by"] == "feature_node"
        assert any(r["uri"] == "/inventory" for r in anchors["routes"])
        assert any(m["name"] == "Product" for m in anchors["models"])

    def test_no_match_when_no_tokens_match(self):
        db = _MockDB({
            "MATCH (f:Feature)": [
                {"name": "Billing", "slug": "billing", "nid": "feat:billing"},
            ],
        })
        anchors = _empty_anchors()
        _try_feature_match(db, ["shipping", "delivery"], anchors)
        assert anchors["feature_name"] is None
        assert anchors["matched_by"] is None

    def test_picks_best_scoring_feature(self):
        db = _MockDB({
            "MATCH (f:Feature)": [
                {"name": "Order Export", "slug": "order-export", "nid": "feat:export"},
                {"name": "Order Management", "slug": "order", "nid": "feat:order"},
            ],
            "BELONGS_TO_FEATURE": [],
        })
        anchors = _empty_anchors()
        # "order" + "management" both appear in "Order Management"
        _try_feature_match(db, ["order", "management"], anchors)
        assert anchors["feature_slug"] == "order"

    def test_none_db_returns_empty(self):
        anchors = _empty_anchors()
        _try_feature_match(None, ["order"], anchors)  # type: ignore[arg-type]
        assert anchors["feature_name"] is None


# ── _try_token_scan ───────────────────────────────────────────────────────────

class TestTryTokenScan:
    def test_finds_routes_by_token(self):
        db = _MockDB({
            "MATCH (r:Route)": [
                {"method": "POST", "uri": "/bookings", "action": "BookingController@store"},
            ],
            "MATCH (m:EloquentModel)": [],
            "MATCH (e:Event)": [],
            "MATCH (j:Job)": [],
        })
        anchors = _empty_anchors()
        _try_token_scan(db, ["booking"], anchors)
        assert anchors["matched_by"] == "token_scan"
        assert any(r["uri"] == "/bookings" for r in anchors["routes"])

    def test_finds_models_by_token(self):
        db = _MockDB({
            "MATCH (r:Route)": [],
            "MATCH (m:EloquentModel)": [
                {"name": "Booking", "table_name": "bookings"},
            ],
            "MATCH (e:Event)": [],
            "MATCH (j:Job)": [],
        })
        anchors = _empty_anchors()
        _try_token_scan(db, ["booking"], anchors)
        assert any(m["name"] == "Booking" for m in anchors["models"])

    def test_empty_tokens_does_nothing(self):
        db = _MockDB()
        anchors = _empty_anchors()
        _try_token_scan(db, [], anchors)
        assert anchors["matched_by"] is None


# ── _expand_event_listeners ───────────────────────────────────────────────────

class TestExpandEventListeners:
    def test_attaches_listeners_to_events(self):
        db = _MockDB({
            "MATCH (e:Event": [
                {"listener_name": "SendNotification"},
                {"listener_name": "UpdateStatus"},
            ],
        })
        anchors = _empty_anchors()
        anchors["events"] = [{"name": "BookingConfirmed", "listeners": []}]
        _expand_event_listeners(db, anchors)
        assert "SendNotification" in anchors["events"][0]["listeners"]
        assert "UpdateStatus" in anchors["events"][0]["listeners"]

    def test_no_events_does_nothing(self):
        db = _MockDB()
        anchors = _empty_anchors()
        _expand_event_listeners(db, anchors)
        assert anchors["events"] == []


# ── _resolve_domain_anchors ───────────────────────────────────────────────────

class TestResolveDomainAnchors:
    def test_feature_path_takes_priority(self):
        db = _MockDB({
            "MATCH (f:Feature)": [
                {"name": "Booking System", "slug": "booking", "nid": "feat:booking"},
            ],
            "BELONGS_TO_FEATURE": [
                {"lbl": "Route", "name": "create", "http_method": "POST",
                 "uri": "/bookings", "action": "BookingController@store",
                 "fqn": None, "db_table": None, "queue": None},
            ],
            "MATCH (e:Event": [],
        })
        anchors = _resolve_domain_anchors(db, "How does the booking system work?")
        assert anchors["feature_name"] == "Booking System"
        assert anchors["matched_by"] == "feature_node"
        assert any(r["uri"] == "/bookings" for r in anchors["routes"])

    def test_falls_back_to_token_scan(self):
        db = _MockDB({
            "MATCH (f:Feature)": [],  # no features
            "MATCH (r:Route)": [
                {"method": "GET", "uri": "/subscriptions", "action": "SubController@index"},
            ],
            "MATCH (m:EloquentModel)": [],
            "MATCH (e:Event)": [],
            "MATCH (j:Job)": [],
            "MATCH (e:Event": [],
        })
        anchors = _resolve_domain_anchors(db, "Show subscription management")
        assert anchors["matched_by"] == "token_scan"
        assert any(r["uri"] == "/subscriptions" for r in anchors["routes"])

    def test_none_db_returns_empty_anchors(self):
        anchors = _resolve_domain_anchors(None, "anything")
        assert anchors["feature_name"] is None
        assert anchors["routes"] == []

    def test_tokens_stored_in_anchors(self):
        db = _MockDB({"MATCH (f:Feature)": []})
        anchors = _resolve_domain_anchors(db, "Show notification history and delivery status")
        assert "notification" in anchors["tokens_used"] or "history" in anchors["tokens_used"]


# ── _format_anchors_for_prompt ────────────────────────────────────────────────

class TestFormatAnchorsForPrompt:
    def test_includes_feature_name(self):
        anchors = _empty_anchors()
        anchors["feature_name"] = "Reservation System"
        anchors["matched_by"] = "feature_node"
        text = _format_anchors_for_prompt(anchors)
        assert "Reservation System" in text

    def test_includes_routes(self):
        anchors = _empty_anchors()
        anchors["routes"] = [{"method": "GET", "uri": "/items", "action": "ItemController@index"}]
        text = _format_anchors_for_prompt(anchors)
        assert "/items" in text

    def test_includes_events_with_listeners(self):
        anchors = _empty_anchors()
        anchors["events"] = [{"name": "ItemCreated", "listeners": ["NotifyAdmin"]}]
        text = _format_anchors_for_prompt(anchors)
        assert "ItemCreated" in text
        assert "NotifyAdmin" in text

    def test_empty_anchors_returns_fallback_message(self):
        text = _format_anchors_for_prompt(_empty_anchors())
        assert "No domain match" in text or "No domain data" in text


# ── _build_summary_text ───────────────────────────────────────────────────────

class TestBuildSummaryText:
    def test_includes_feature_name(self):
        anchors = _empty_anchors()
        anchors["feature_name"] = "Reservation System"
        text = _build_summary_text(anchors, [])
        assert "Reservation System" in text

    def test_includes_route_info(self):
        anchors = _empty_anchors()
        anchors["routes"] = [{"method": "POST", "uri": "/reserve", "action": "ReserveController@store"}]
        text = _build_summary_text(anchors, ["res_overview"])
        assert "/reserve" in text

    def test_lists_available_tools(self):
        anchors = _empty_anchors()
        text = _build_summary_text(anchors, ["res_overview", "res_events"])
        assert "res_overview()" in text
        assert "res_events()" in text

    def test_no_feature_uses_generic_header(self):
        text = _build_summary_text(_empty_anchors(), [])
        assert "Domain Overview" in text


# ── _assemble_plugin_code ─────────────────────────────────────────────────────

class TestAssemblePluginCode:
    def _make_spec(self, slug="item-tracker", prefix="item_", tools=None):
        if tools is None:
            tools = [{
                "name": "item_list",
                "description": "List all items",
                "cypher_query": "MATCH (r:Route) RETURN r.http_method AS m, r.uri AS u LIMIT 30",
                "result_format": "[{m}] {u}",
            }]
        return {"slug": slug, "prefix": prefix, "tools": tools}

    def test_produces_valid_python(self):
        code = _assemble_plugin_code(self._make_spec(), _empty_anchors())
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors

    def test_manifest_fields_correct(self):
        code = _assemble_plugin_code(self._make_spec(), _empty_anchors())
        assert '"name": "item-tracker"' in code
        assert '"tool_prefix": "item_"' in code
        assert '"version": "1.0.0"' in code

    def test_summary_tool_always_present(self):
        code = _assemble_plugin_code(self._make_spec(), _empty_anchors())
        assert "def item_summary()" in code

    def test_store_tool_always_present(self):
        code = _assemble_plugin_code(self._make_spec(), _empty_anchors())
        # Now accepts a free-text findings param
        assert "def item_store_discoveries(findings: str)" in code

    def test_llm_tool_present(self):
        code = _assemble_plugin_code(self._make_spec(), _empty_anchors())
        assert "def item_list()" in code

    def test_multiple_tools_all_present(self):
        spec = self._make_spec(tools=[
            {"name": "item_routes", "description": "Routes", "cypher_query": "MATCH (r:Route) RETURN r.uri AS u LIMIT 30", "result_format": "{u}"},
            {"name": "item_models", "description": "Models", "cypher_query": "MATCH (m:EloquentModel) RETURN m.name AS n LIMIT 30", "result_format": "{n}"},
        ])
        code = _assemble_plugin_code(spec, _empty_anchors())
        assert "def item_routes()" in code
        assert "def item_models()" in code
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors

    def test_result_format_aliases_substituted(self):
        spec = self._make_spec(tools=[{
            "name": "item_list",
            "description": "desc",
            "cypher_query": "MATCH (r:Route) RETURN r.http_method AS method, r.uri AS uri LIMIT 30",
            "result_format": "[{method}] {uri}",
        }])
        code = _assemble_plugin_code(spec, _empty_anchors())
        assert "r.get('method'" in code
        assert "r.get('uri'" in code

    def test_domain_anchors_appear_in_summary(self):
        anchors = _empty_anchors()
        anchors["feature_name"] = "Stock Control"
        anchors["routes"] = [{"method": "GET", "uri": "/stock", "action": "StockController@index"}]
        code = _assemble_plugin_code(self._make_spec(), anchors)
        assert "Stock Control" in code
        assert "/stock" in code

    def test_layer_1_passes_with_anchors(self):
        anchors = _empty_anchors()
        anchors["feature_name"] = "Notification Hub"
        anchors["events"] = [{"name": "AlertSent", "listeners": ["LogAlert"]}]
        code = _assemble_plugin_code(self._make_spec(), anchors)
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors

    def test_layer_3_passes(self):
        from laravelgraph.plugins.generator import _validate_execution
        code = _assemble_plugin_code(self._make_spec(), _empty_anchors())
        l3 = _validate_execution(code, None)
        assert l3.passed, l3.critique


# ── _build_template_fallback ──────────────────────────────────────────────────

class TestBuildTemplateFallback:
    def test_produces_valid_python(self):
        code = _build_template_fallback("Show me all the order processing flows and events")
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors

    def test_slug_derived_from_description(self):
        code = _build_template_fallback("Show reservation lifecycle")
        assert "reservation" in code or "lifecycle" in code

    def test_tool_prefix_not_laravelgraph(self):
        # The generated tool function names should not start with laravelgraph_,
        # but the docstring may reference laravelgraph_plugin_knowledge() — that's OK.
        code = _build_template_fallback("Show all things")
        import re
        tool_defs = re.findall(r'def\s+(\w+)\s*\(', code)
        for fn in tool_defs:
            assert not fn.startswith("laravelgraph_"), f"Tool function name should not start with laravelgraph_: {fn}"

    def test_summary_tool_present(self):
        code = _build_template_fallback("Show order status history")
        # The fallback generates a summary function
        assert "_summary" in code

    def test_empty_description_does_not_crash(self):
        code = _build_template_fallback("")
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors


# ── generate_plugin() integration ────────────────────────────────────────────

class _MockCfg:
    class llm:
        enabled = True
        provider = "auto"
        api_keys: dict = {}
        models: dict = {}
        base_urls: dict = {}


class TestGeneratePlugin:
    """End-to-end tests using mock LLM and mock DB."""

    def _make_db(self) -> _MockDB:
        return _MockDB({
            "MATCH (f:Feature)": [
                {"name": "Catalog", "slug": "catalog", "nid": "feat:catalog"},
            ],
            "BELONGS_TO_FEATURE": [
                {"lbl": "Route", "name": "index", "http_method": "GET",
                 "uri": "/catalog", "action": "CatalogController@index",
                 "fqn": None, "db_table": None, "queue": None},
                {"lbl": "EloquentModel", "name": "Product", "db_table": "products",
                 "http_method": None, "uri": None, "action": None, "fqn": None, "queue": None},
            ],
            "MATCH (e:Event": [],
        })

    def _llm_json_response(self) -> str:
        import json
        return json.dumps({
            "slug": "catalog-explorer",
            "prefix": "cat_",
            "tools": [{
                "name": "cat_routes",
                "description": "List catalog routes",
                "cypher_query": "MATCH (r:Route) RETURN r.http_method AS m, r.uri AS u LIMIT 30",
                "result_format": "[{m}] {u}",
            }],
        })

    def test_successful_generation(self, tmp_path):
        with patch("laravelgraph.plugins.generator._call_llm") as mock_llm, \
             patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
            mock_llm.return_value = self._llm_json_response()
            mock_judge.return_value = MagicMock(passed=True, layer=4, score=8.5, critique="Good")

            code, msg = generate_plugin(
                description="Show the catalog routes and products",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
            )

        assert code is not None
        assert "Plugin generated" in msg
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors

    def test_summary_tool_in_output(self, tmp_path):
        with patch("laravelgraph.plugins.generator._call_llm") as mock_llm, \
             patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
            mock_llm.return_value = self._llm_json_response()
            mock_judge.return_value = MagicMock(passed=True, layer=4, score=8.0, critique="")

            code, _ = generate_plugin(
                description="Catalog domain overview",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
            )

        assert code is not None
        assert "def cat_summary()" in code

    def test_domain_anchors_embedded_in_summary(self, tmp_path):
        with patch("laravelgraph.plugins.generator._call_llm") as mock_llm, \
             patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
            mock_llm.return_value = self._llm_json_response()
            mock_judge.return_value = MagicMock(passed=True, layer=4, score=8.0, critique="")

            code, _ = generate_plugin(
                description="Catalog domain overview",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
            )

        # The feature name and route should appear in the summary tool's hard-coded text
        assert "Catalog" in code
        assert "/catalog" in code

    def test_no_llm_returns_none(self, tmp_path):
        with patch("laravelgraph.plugins.generator._call_llm", return_value=None):
            code, msg = generate_plugin(
                description="anything",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
            )
        assert code is None
        assert "No LLM" in msg

    def test_invalid_json_from_llm_falls_back_to_template(self, tmp_path):
        with patch("laravelgraph.plugins.generator._call_llm", return_value="not json at all !!!"):
            code, msg = generate_plugin(
                description="Show catalog items and inventory levels",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
                max_iterations=1,
                allow_skeleton=True,  # explicit opt-in required since v0.3
            )
        # Template fallback should kick in when allow_skeleton=True
        assert code is not None
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors

    def test_invalid_json_no_skeleton_by_default(self, tmp_path):
        with patch("laravelgraph.plugins.generator._call_llm", return_value="not json at all !!!"):
            code, msg = generate_plugin(
                description="Show catalog items and inventory levels",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
                max_iterations=1,
                # allow_skeleton defaults to False
            )
        assert code is None
        assert "allow_skeleton" in msg

    def test_layer4_retry_on_low_score(self, tmp_path):
        call_count = 0

        def mock_llm(prompt, cfg):
            nonlocal call_count
            call_count += 1
            import json
            return json.dumps({
                "slug": "cat-v" + str(call_count),
                "prefix": "cat_",
                "tools": [{"name": "cat_query", "description": "desc",
                            "cypher_query": "MATCH (r:Route) RETURN r.uri AS u LIMIT 30",
                            "result_format": "{u}"}],
            })

        judge_call = 0

        def mock_judge(description, code, cfg):
            nonlocal judge_call
            judge_call += 1
            if judge_call == 1:
                return MagicMock(passed=False, layer=4, score=3.0, critique="Too generic")
            return MagicMock(passed=True, layer=4, score=8.0, critique="Good")

        with patch("laravelgraph.plugins.generator._call_llm", side_effect=mock_llm), \
             patch("laravelgraph.plugins.generator._validate_llm_judge", side_effect=mock_judge):
            code, msg = generate_plugin(
                description="Show catalog overview",
                project_root=tmp_path,
                core_db=self._make_db(),
                cfg=_MockCfg(),
                max_iterations=3,
            )

        assert code is not None
        assert call_count == 2  # retried once after low judge score

    def test_none_db_still_generates(self, tmp_path):
        """Should still produce something useful even without a graph connection."""
        with patch("laravelgraph.plugins.generator._call_llm") as mock_llm, \
             patch("laravelgraph.plugins.generator._validate_llm_judge") as mock_judge:
            import json
            mock_llm.return_value = json.dumps({
                "slug": "generic-query",
                "prefix": "gen_",
                "tools": [{"name": "gen_routes", "description": "List routes",
                            "cypher_query": "MATCH (r:Route) RETURN r.uri AS u LIMIT 30",
                            "result_format": "{u}"}],
            })
            mock_judge.return_value = MagicMock(passed=True, layer=4, score=7.5, critique="")

            code, msg = generate_plugin(
                description="Show all routes",
                project_root=tmp_path,
                core_db=None,
                cfg=_MockCfg(),
            )

        assert code is not None
        result = validate_plugin_file_content(code)
        assert result.passed, result.errors
