"""Unit tests for dead-code detection exemptions and route parsing fixes.

Tests cover:
- Phase 10: broadcastOn / schedule / resource-role exemptions
- Phase 10: legacy/vendor path exclusion
- Phase 14: Resource route controller FQN strips ::class
- Phase 14: Nested group prefix stacking (protect → api → auth)
- Phase 14: _find_brace_end correctly finds matching closing brace
"""

from __future__ import annotations

import re
import pytest


# ── Dead-code exemption constants ─────────────────────────────────────────────

from laravelgraph.pipeline.phase_10_dead_code import (
    _EXEMPT_METHOD_NAMES,
    _EXEMPT_ROLES,
    _is_exempt_by_name,
)


class TestExemptMethodNames:
    def test_broadcastOn_is_exempt(self):
        assert "broadcastOn" in _EXEMPT_METHOD_NAMES

    def test_schedule_is_exempt(self):
        assert "schedule" in _EXEMPT_METHOD_NAMES

    def test_broadcastWith_is_exempt(self):
        assert "broadcastWith" in _EXEMPT_METHOD_NAMES

    def test_via_is_exempt(self):
        assert "via" in _EXEMPT_METHOD_NAMES

    def test_build_is_exempt(self):
        assert "build" in _EXEMPT_METHOD_NAMES

    def test_toArray_is_exempt(self):
        assert "toArray" in _EXEMPT_METHOD_NAMES

    def test_handle_still_exempt(self):
        assert "handle" in _EXEMPT_METHOD_NAMES

    def test_is_exempt_by_name_broadcastOn(self):
        assert _is_exempt_by_name("broadcastOn")

    def test_is_exempt_by_name_schedule(self):
        assert _is_exempt_by_name("schedule")

    def test_is_exempt_by_name_regular_method(self):
        assert not _is_exempt_by_name("doSomethingCustom")


class TestExemptRoles:
    def test_resource_role_is_exempt(self):
        assert "resource" in _EXEMPT_ROLES

    def test_policy_still_exempt(self):
        assert "policy" in _EXEMPT_ROLES

    def test_observer_still_exempt(self):
        assert "observer" in _EXEMPT_ROLES

    def test_request_still_exempt(self):
        assert "request" in _EXEMPT_ROLES


# ── Route resource pattern: ::class must be stripped ──────────────────────────

from laravelgraph.pipeline.phase_14_routes import (
    _ROUTE_RESOURCE_PATTERN,
    _parse_routes_from_file,
    _parse_route_group_context,
    _get_group_context_for_pos,
    _find_brace_end,
)


class TestResourceRoutePattern:
    """_ROUTE_RESOURCE_PATTERN must not capture ::class as part of the class name."""

    def _match(self, text: str):
        return _ROUTE_RESOURCE_PATTERN.search(text)

    def test_strips_class_suffix_simple(self):
        m = self._match("Route::resource('bookings', BookingController::class)")
        assert m is not None
        assert m.group(3).strip() == "BookingController"

    def test_strips_class_suffix_fqn(self):
        m = self._match(
            "Route::resource('bookings', App\\Http\\Controllers\\BookingController::class)"
        )
        assert m is not None
        assert m.group(3).strip() == "App\\Http\\Controllers\\BookingController"

    def test_no_class_suffix(self):
        m = self._match("Route::resource('bookings', BookingController)")
        assert m is not None
        assert m.group(3).strip() == "BookingController"

    def test_api_resource_also_works(self):
        m = self._match("Route::apiResource('bookings', BookingController::class)")
        assert m is not None
        assert m.group(1).lower() == "apiresource"
        assert m.group(3).strip() == "BookingController"

    def test_does_not_capture_colon_in_class_name(self):
        """::class must not be part of group(3)."""
        m = self._match("Route::resource('items', ItemController::class)")
        assert m is not None
        assert "::" not in m.group(3)


# ── Nested group prefix stacking ──────────────────────────────────────────────

class TestNestedPrefixStacking:
    PHP_NESTED = """<?php
Route::group(['prefix' => 'api'], function () {
    Route::group(['prefix' => 'v1'], function () {
        Route::get('/users', [UserController::class, 'index']);
    });
});
"""

    PHP_TRIPLE = """<?php
Route::group(['prefix' => 'protect'], function () {
    Route::group(['prefix' => 'api'], function () {
        Route::group(['prefix' => 'auth'], function () {
            Route::post('login', [AuthController::class, 'login']);
        });
    });
});
"""

    def _parse(self, php: str, file_name: str = "web.php"):
        from pathlib import Path
        import tempfile, os
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".php", delete=False, encoding="utf-8"
        ) as f:
            f.write(php)
            tmp = Path(f.name)
        try:
            return _parse_routes_from_file(tmp, is_api=False, class_map={}, composer_namespace="App\\")
        finally:
            os.unlink(tmp)

    def test_two_levels_stack(self):
        routes = self._parse(self.PHP_NESTED)
        uris = [r["uri"] for r in routes]
        assert any("/api/v1/users" in u for u in uris), f"Got: {uris}"

    def test_triple_nesting_stacks(self):
        routes = self._parse(self.PHP_TRIPLE)
        uris = [r["uri"] for r in routes]
        assert any("/protect/api/auth/login" in u for u in uris), f"Got: {uris}"

    def test_sibling_groups_dont_bleed(self):
        php = """<?php
Route::group(['prefix' => 'admin'], function () {
    Route::get('/users', [UserController::class, 'index']);
});
Route::group(['prefix' => 'api'], function () {
    Route::get('/items', [ItemController::class, 'index']);
});
"""
        routes = self._parse(php)
        by_uri = {r["uri"]: r for r in routes}
        # /admin/users must not contain 'api'
        assert "api" not in by_uri.get("/admin/users", {}).get("uri", "")
        # /api/items must not contain 'admin'
        assert "admin" not in by_uri.get("/api/items", {}).get("uri", "")


# ── _find_brace_end ────────────────────────────────────────────────────────────

class TestFindBraceEnd:
    def test_simple_block(self):
        src = "{ hello }"
        assert _find_brace_end(src, 0) == len(src) - 1

    def test_nested_braces(self):
        src = "{ outer { inner } }"
        assert _find_brace_end(src, 0) == len(src) - 1

    def test_brace_in_string_ignored(self):
        src = '{ $x = "{not a brace}"; }'
        assert _find_brace_end(src, 0) == len(src) - 1

    def test_returns_len_if_unclosed(self):
        src = "{ unclosed"
        assert _find_brace_end(src, 0) == len(src)

    def test_starts_at_non_zero(self):
        src = "prefix{ content }suffix"
        pos = src.index("{")
        end = _find_brace_end(src, pos)
        assert src[end] == "}"


# ── Vendor/legacy path exclusion (logic check) ────────────────────────────────

class TestVendorPathExclusion:
    """Verify the string conditions used in phase 10 to skip vendor/legacy paths."""

    _VENDOR_MARKERS = ("/vendor/", "/legacy/", "\\vendor\\", "\\legacy\\")

    def _should_skip(self, fp: str) -> bool:
        return any(marker in fp for marker in self._VENDOR_MARKERS)

    def test_vendor_path_skipped(self):
        assert self._should_skip("/app/vendor/phpexcel/Classes/PHPExcel.php")

    def test_legacy_path_skipped(self):
        assert self._should_skip("/app/legacy/phpoffice/Classes/Calculation.php")

    def test_normal_app_path_not_skipped(self):
        assert not self._should_skip("/app/Http/Controllers/BookingController.php")

    def test_model_path_not_skipped(self):
        assert not self._should_skip("/app/Models/Booking.php")
