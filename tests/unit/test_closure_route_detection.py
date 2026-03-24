"""Unit tests for Closure-route controller delegation detection in phase_14_routes.

Covers all 6 dispatch patterns:
  1. (new Controller())->method(…)
  2. new Controller()->method(…)
  3. app(Controller::class)->method(…)  /  resolve(…)
  4. $app->make(Controller::class)->method(…)  /  app()->make(…)
  5. DI-injected closure parameter: function(…, Ctrl $c) { $c->method(…) }
  6. Static dispatch: Controller::method(…)

Also tests _extract_braced_body, _find_closure_body, _is_delegated_class, and
full route-file parsing end-to-end with closure routes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from laravelgraph.pipeline.phase_14_routes import (
    _extract_braced_body,
    _extract_arrow_expr,
    _find_closure_body,
    _find_closure_controller,
    _is_delegated_class,
    _parse_routes_from_file,
)

# ── _is_delegated_class ───────────────────────────────────────────────────────


class TestIsDelegatedClass:
    def test_controller_class_is_delegated(self):
        assert _is_delegated_class("BookingController") is True

    def test_service_class_is_delegated(self):
        assert _is_delegated_class("BookingService") is True

    def test_request_facade_not_delegated(self):
        assert _is_delegated_class("Request") is False

    def test_db_facade_not_delegated(self):
        assert _is_delegated_class("DB") is False

    def test_illuminate_class_not_delegated(self):
        assert _is_delegated_class("Illuminate\\Http\\Request") is False

    def test_model_base_not_delegated(self):
        assert _is_delegated_class("Model") is False

    def test_lowercase_not_delegated(self):
        assert _is_delegated_class("response") is False

    def test_fqn_controller_is_delegated(self):
        # The base class name "BookingController" is what matters
        assert _is_delegated_class("App\\Http\\Controllers\\BookingController") is True

    def test_response_not_delegated(self):
        assert _is_delegated_class("Response") is False

    def test_closure_not_delegated(self):
        assert _is_delegated_class("Closure") is False


# ── _extract_braced_body ──────────────────────────────────────────────────────


class TestExtractBracedBody:
    def test_simple_body(self):
        src = "{ return 'hello'; }"
        result = _extract_braced_body(src, 0)
        assert "return 'hello'" in result

    def test_nested_braces(self):
        src = "{ if (true) { return 1; } return 2; }"
        result = _extract_braced_body(src, 0)
        assert "if (true)" in result
        assert "return 2" in result

    def test_string_with_brace_not_counted(self):
        src = r"""{ $x = "he said {hi}"; return $x; }"""
        result = _extract_braced_body(src, 0)
        assert "$x" in result

    def test_line_comment_brace_not_counted(self):
        src = "{ // { fake brace\n return 1; }"
        result = _extract_braced_body(src, 0)
        assert "return 1" in result

    def test_block_comment_brace_not_counted(self):
        src = "{ /* { fake } */ return 2; }"
        result = _extract_braced_body(src, 0)
        assert "return 2" in result

    def test_empty_body(self):
        src = "{}"
        result = _extract_braced_body(src, 0)
        assert result == ""

    def test_starts_mid_source(self):
        src = "before { return 42; } after"
        result = _extract_braced_body(src, src.index("{"))
        assert "return 42" in result
        assert "after" not in result


# ── _find_closure_body ────────────────────────────────────────────────────────


class TestFindClosureBody:
    def _make_source_with_route(self, handler: str) -> tuple[str, int]:
        """Wrap handler in a Route::post call and return (source, match_end)."""
        import re
        src = f"Route::post('/test', {handler});"
        # Simulate the regex match end — position after the route regex would match
        # The regex captures handler up to first ')' in params
        m = re.search(r"Route::post\('/test',\s*function\([^)]*\)", src)
        if not m:
            m = re.search(r"Route::post\('/test',\s*fn\([^)]*\)", src)
        end = m.end() if m else src.index("{") - 1
        return src, end

    def test_regular_function_body_extracted(self):
        src = "x function(Request $r) { return $r->all(); } y"
        body = _find_closure_body(src, src.index("function") + len("function(Request $r)"))
        assert "return $r->all()" in body

    def test_arrow_function_body_extracted(self):
        src = "x fn(Request $r) => $r->all(); y"
        body = _find_closure_body(src, src.index("fn") + len("fn(Request $r)"))
        assert "$r->all()" in body

    def test_empty_when_no_body(self):
        src = "some text with no closure"
        body = _find_closure_body(src, 0)
        assert body == ""


# ── _find_closure_controller — all 6 patterns ────────────────────────────────


def _use_map() -> dict[str, str]:
    return {
        "BookingController": "App\\Http\\Controllers\\BookingController",
        "PaymentService": "App\\Services\\PaymentService",
        "OrderController": "App\\Http\\Controllers\\OrderController",
    }


def _class_map() -> dict[str, Path]:
    return {
        "App\\Http\\Controllers\\BookingController": Path("/app/Http/Controllers/BookingController.php"),
        "App\\Services\\PaymentService": Path("/app/Services/PaymentService.php"),
        "App\\Http\\Controllers\\OrderController": Path("/app/Http/Controllers/OrderController.php"),
    }


class TestClosurePattern1NewParen:
    """(new Controller())->method(…)"""

    def test_basic(self):
        body = "return (new BookingController())->createBooking($request);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"
        assert conf == pytest.approx(0.85)

    def test_without_constructor_parens(self):
        body = "return (new BookingController)->createBooking($r);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"

    def test_ignores_request_class(self):
        body = "return (new Request())->all();"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""


class TestClosurePattern2NewCall:
    """new Controller()->method(…) — no surrounding parens"""

    def test_basic(self):
        body = "$result = new BookingController()->processBooking($data);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "processBooking"
        assert conf == pytest.approx(0.85)

    def test_with_constructor_args(self):
        body = "return new OrderController($dep)->index();"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\OrderController"
        assert method == "index"


class TestClosurePattern3AppHelper:
    """app(Controller::class)->method(…)  /  resolve(…)"""

    def test_app_helper_class_constant(self):
        body = "return app(BookingController::class)->createBooking($request);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"
        assert conf == pytest.approx(0.90)

    def test_resolve_helper(self):
        body = "return resolve(BookingController::class)->createBooking($r);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"

    def test_app_helper_string(self):
        body = "return app('App\\Http\\Controllers\\BookingController')->createBooking($r);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"


class TestClosurePattern4ContainerMake:
    """$app->make(Controller::class)->method(…)  /  app()->make(…)->method(…)"""

    def test_variable_make(self):
        body = "return $app->make(BookingController::class)->createBooking($r);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"
        assert conf == pytest.approx(0.85)

    def test_app_call_make(self):
        body = "return app()->make(BookingController::class)->createBooking($r);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"


class TestClosurePattern5DIParam:
    """DI via closure param: function(…, BookingController $ctrl) { $ctrl->method(…) }"""

    def test_injected_controller_param(self):
        body = "return $ctrl->createBooking($request);"
        params = "Request $request, BookingController $ctrl"
        ctrl, method, conf = _find_closure_controller(body, params, _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"
        assert conf == pytest.approx(0.90)

    def test_ignores_request_param(self):
        body = "return $request->all();"
        params = "Request $request"
        ctrl, method, conf = _find_closure_controller(body, params, _use_map(), _class_map(), "App\\")
        assert ctrl == ""

    def test_multiple_params_correct_one_picked(self):
        body = "return $svc->process($request);"
        params = "Request $request, PaymentService $svc"
        ctrl, method, conf = _find_closure_controller(body, params, _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Services\\PaymentService"
        assert method == "process"


class TestClosurePattern6Static:
    """Static dispatch: Controller::method(…) — only when class is in class_map"""

    def test_static_call_confirmed_in_class_map(self):
        body = "return BookingController::createBooking($request);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == "App\\Http\\Controllers\\BookingController"
        assert method == "createBooking"
        assert conf == pytest.approx(0.70)

    def test_static_call_not_in_class_map_ignored(self):
        body = "return UnknownController::doSomething($x);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""

    def test_static_class_keyword_skipped(self):
        body = "return BookingController::class;"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""

    def test_db_facade_static_ignored(self):
        body = "return DB::table('bookings')->get();"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""


class TestPureClosure:
    """Routes with no controller delegation should remain unresolved."""

    def test_view_return_not_resolved(self):
        body = "return view('welcome');"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""

    def test_response_json_not_resolved(self):
        body = "return response()->json(['status' => 'ok']);"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""

    def test_empty_body_not_resolved(self):
        ctrl, method, conf = _find_closure_controller("", "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""
        assert conf == 0.0

    def test_auth_check_not_resolved(self):
        body = "return Auth::user()->profile();"
        ctrl, method, conf = _find_closure_controller(body, "", _use_map(), _class_map(), "App\\")
        assert ctrl == ""


# ── Full route-file parsing ───────────────────────────────────────────────────


class TestParseRoutesWithClosures:
    """Integration: _parse_routes_from_file resolves closure controllers."""

    def _write_route_file(self, tmp_path: Path, content: str) -> Path:
        f = tmp_path / "routes" / "web.php"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        return f

    def _class_map(self) -> dict[str, Path]:
        return {
            "App\\Http\\Controllers\\BookingController": Path("/app/BookingController.php"),
            "App\\Http\\Controllers\\PostController": Path("/app/PostController.php"),
        }

    def test_closure_new_paren_resolved(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::post('/bookings/create', function(Request $request) {
    return (new BookingController())->createBooking($request);
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert len(routes) == 1
        r = routes[0]
        assert r["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert r["action_method"] == "createBooking"

    def test_closure_app_helper_resolved(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::post('/v1/booking', function(Request $r) {
    return app(BookingController::class)->createBooking($r);
});
""")
        routes = _parse_routes_from_file(f, True, self._class_map(), "App\\")
        assert routes[0]["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert routes[0]["action_method"] == "createBooking"

    def test_arrow_function_resolved(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::post('/bookings', fn(Request $r) => (new BookingController())->createBooking($r));
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert routes[0]["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert routes[0]["action_method"] == "createBooking"

    def test_pure_view_closure_stays_empty(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use Illuminate\\Support\\Facades\\Route;

Route::get('/', function() {
    return view('welcome');
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert len(routes) == 1
        assert routes[0]["controller_fqn"] == ""
        assert routes[0]["action_method"] == ""

    def test_normal_array_route_unaffected(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\PostController;
use Illuminate\\Support\\Facades\\Route;

Route::get('/posts', [PostController::class, 'index']);
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert routes[0]["controller_fqn"] == "App\\Http\\Controllers\\PostController"
        assert routes[0]["action_method"] == "index"

    def test_mixed_file_closure_and_normal(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use App\\Http\\Controllers\\PostController;
use Illuminate\\Support\\Facades\\Route;

Route::get('/posts', [PostController::class, 'index']);
Route::post('/bookings', function(Request $r) {
    return (new BookingController())->createBooking($r);
});
Route::get('/', function() {
    return view('welcome');
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert len(routes) == 3

        post_route = next(r for r in routes if r["uri"] == "/posts")
        booking_route = next(r for r in routes if r["uri"] == "/bookings")
        home_route = next(r for r in routes if r["uri"] == "/")

        assert post_route["controller_fqn"] == "App\\Http\\Controllers\\PostController"
        assert booking_route["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert home_route["controller_fqn"] == ""

    def test_di_param_closure_resolved(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::post('/book', function(Request $request, BookingController $ctrl) {
    return $ctrl->createBooking($request);
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert routes[0]["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert routes[0]["action_method"] == "createBooking"


# ── Laravel 9+ group controller syntax ───────────────────────────────────────


class TestGroupControllerSyntax:
    """Route::group(['controller' => Ctrl::class], fn) and fluent Route::controller(Ctrl)."""

    def _write_route_file(self, tmp_path, content):
        from pathlib import Path
        f = tmp_path / "routes" / "api.php"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content, encoding="utf-8")
        return f

    def _class_map(self):
        return {
            "App\\Http\\Controllers\\BookingController": Path("/app/BookingController.php"),
            "App\\Http\\Controllers\\PostController": Path("/app/PostController.php"),
        }

    def test_array_group_controller(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::group(['controller' => BookingController::class], function() {
    Route::post('bookings/create', 'createBooking');
    Route::get('bookings', 'index');
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert len(routes) == 2
        create = next(r for r in routes if "create" in r["uri"])
        index_r = next(r for r in routes if r["action_method"] == "index")
        assert create["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert create["action_method"] == "createBooking"
        assert index_r["controller_fqn"] == "App\\Http\\Controllers\\BookingController"

    def test_array_group_with_prefix_and_controller(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::group(['prefix' => 'v1', 'controller' => BookingController::class], function() {
    Route::post('booking', 'createBooking');
});
""")
        routes = _parse_routes_from_file(f, True, self._class_map(), "App\\")
        assert len(routes) == 1
        assert routes[0]["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert routes[0]["action_method"] == "createBooking"

    def test_fluent_controller_group(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::controller(BookingController::class)->group(function() {
    Route::post('/bookings/create', 'createBooking');
    Route::get('/bookings', 'index');
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        fqns = {r["controller_fqn"] for r in routes}
        assert "App\\Http\\Controllers\\BookingController" in fqns
        methods = {r["action_method"] for r in routes}
        assert "createBooking" in methods
        assert "index" in methods

    def test_fluent_prefix_then_controller(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use Illuminate\\Support\\Facades\\Route;

Route::prefix('v1')->controller(BookingController::class)->group(function() {
    Route::post('booking', 'createBooking');
});
""")
        routes = _parse_routes_from_file(f, True, self._class_map(), "App\\")
        assert len(routes) == 1
        assert routes[0]["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert routes[0]["action_method"] == "createBooking"

    def test_bare_string_without_group_controller_stays_empty(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use Illuminate\\Support\\Facades\\Route;

Route::post('/orphan', 'orphanMethod');
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert len(routes) == 1
        assert routes[0]["controller_fqn"] == ""

    def test_two_groups_different_controllers(self, tmp_path):
        f = self._write_route_file(tmp_path, """<?php
use App\\Http\\Controllers\\BookingController;
use App\\Http\\Controllers\\PostController;
use Illuminate\\Support\\Facades\\Route;

Route::group(['controller' => BookingController::class], function() {
    Route::post('/bookings', 'createBooking');
});

Route::group(['controller' => PostController::class], function() {
    Route::get('/posts', 'index');
});
""")
        routes = _parse_routes_from_file(f, False, self._class_map(), "App\\")
        assert len(routes) == 2
        booking = next(r for r in routes if "booking" in r["uri"])
        post = next(r for r in routes if "post" in r["uri"])
        assert booking["controller_fqn"] == "App\\Http\\Controllers\\BookingController"
        assert post["controller_fqn"] == "App\\Http\\Controllers\\PostController"
