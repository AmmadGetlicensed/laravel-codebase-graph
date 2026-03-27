"""Unit tests for phase_05 dispatch detection helpers.

Tests the _find_dispatches and _resolve_dispatch_target functions which detect
Laravel event/job dispatch patterns in PHP method source text and create
DISPATCHES edges (Method → Event/Job).

These tests are pure unit tests — no pipeline or DB required.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from laravelgraph.pipeline.phase_05_calls import (
    _find_dispatches,
    _resolve_dispatch_target,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _has(result: list, cls: str, dtype: str) -> bool:
    """Return True if any triple in result starts with (cls, dtype)."""
    return any(r[0] == cls and r[1] == dtype for r in result)


# ── _find_dispatches ──────────────────────────────────────────────────────────

class TestFindDispatchesEventHelper:
    """event(new X()) — always an event."""

    def test_basic_event_helper(self):
        src = "event(new UserRegistered($user));"
        result = _find_dispatches(src, {})
        assert _has(result, "UserRegistered", "event")

    def test_event_helper_with_spaces(self):
        src = "event( new CourseBooked( $booking ) );"
        result = _find_dispatches(src, {})
        assert _has(result, "CourseBooked", "event")

    def test_event_helper_ignores_non_event_calls(self):
        src = "logger()->info('no dispatch here');"
        result = _find_dispatches(src, {})
        assert result == []

    def test_event_helper_with_namespace_strips_to_short(self):
        src = r"event(new App\Events\UserRegistered($user));"
        result = _find_dispatches(src, {})
        assert _has(result, "UserRegistered", "event")

    def test_multiple_event_helpers_in_one_method(self):
        src = (
            "event(new UserRegistered($user));\n"
            "event(new PaymentReceived($order));"
        )
        result = _find_dispatches(src, {})
        names = [r[0] for r in result]
        assert "UserRegistered" in names
        assert "PaymentReceived" in names


class TestFindDispatchesStaticDispatch:
    """ClassName::dispatch() — static dispatch."""

    def test_job_static_dispatch(self):
        src = "SendWelcomeEmail::dispatch($user);"
        result = _find_dispatches(src, {})
        assert _has(result, "SendWelcomeEmail", "job")

    def test_event_static_dispatch_by_name(self):
        src = "UserRegisteredEvent::dispatch($user);"
        result = _find_dispatches(src, {})
        assert _has(result, "UserRegisteredEvent", "event")

    def test_dispatch_if(self):
        src = "CourseBookedJob::dispatchIf($cond, $booking);"
        result = _find_dispatches(src, {})
        assert _has(result, "CourseBookedJob", "job")

    def test_dispatch_now(self):
        src = "SendContact2HubSpot::dispatchNow($id);"
        result = _find_dispatches(src, {})
        assert _has(result, "SendContact2HubSpot", "job")

    def test_dispatch_after_response(self):
        src = "CreateWalletPasses::dispatchAfterResponse($bookingId);"
        result = _find_dispatches(src, {})
        assert _has(result, "CreateWalletPasses", "job")

    def test_skips_generic_event_facade(self):
        # Event::dispatch(new X()) is handled by _EVENT_FACADE_RE, not _STATIC_DISPATCH_RE
        src = "Event::dispatch(new CourseBooked($b));"
        result = _find_dispatches(src, {})
        # "Event" should be skipped as a generic facade
        names = [r[0] for r in result]
        assert "Event" not in names

    def test_skips_bus_facade(self):
        src = "Bus::dispatch($job);"
        result = _find_dispatches(src, {})
        names = [r[0] for r in result]
        assert "Bus" not in names

    def test_skips_queue_facade(self):
        src = "Queue::push($job);"
        result = _find_dispatches(src, {})
        names = [r[0] for r in result]
        assert "Queue" not in names


class TestFindDispatchesNewSyntax:
    """dispatch(new X()) — helper function syntax."""

    def test_dispatch_new_job(self):
        src = "dispatch(new CourseBookedJob($booking));"
        result = _find_dispatches(src, {})
        assert _has(result, "CourseBookedJob", "job")

    def test_dispatch_if_new_syntax(self):
        src = "dispatchIf($cond, new SendEmailToNonQualhubCenter($id));"
        result = _find_dispatches(src, {})
        assert _has(result, "SendEmailToNonQualhubCenter", "job")

    def test_event_suffix_treated_as_event(self):
        src = "dispatch(new PaymentConfirmedEvent($order));"
        result = _find_dispatches(src, {})
        assert _has(result, "PaymentConfirmedEvent", "event")


class TestFindDispatchesEventFacade:
    """Event::dispatch(new X()) — Event facade."""

    def test_event_facade_dispatch(self):
        src = "Event::dispatch(new UserRegistered($user));"
        result = _find_dispatches(src, {})
        assert _has(result, "UserRegistered", "event")

    def test_event_facade_with_namespace(self):
        src = r"Event::dispatch(new App\Events\CourseBooked($b));"
        result = _find_dispatches(src, {})
        assert _has(result, "CourseBooked", "event")

    def test_event_facade_not_double_counted(self):
        """Event::dispatch(new X()) must not produce two entries for X."""
        src = "Event::dispatch(new UserUpdated($user));"
        result = _find_dispatches(src, {})
        matching = [r for r in result if r[0] == "UserUpdated"]
        assert len(matching) == 1, f"Expected 1 entry for UserUpdated, got {len(matching)}: {matching}"


class TestFindDispatchesRealWorldPatterns:
    """Patterns taken directly from real codebase feedback."""

    def test_course_booked_job_dispatch(self):
        src = "CourseBookedJob::dispatchIf($booking->firstaid_guru_event_id == 0, $booking);"
        result = _find_dispatches(src, {})
        assert _has(result, "CourseBookedJob", "job")

    def test_send_email_dispatch_if(self):
        src = "SendEmailToNonQualhubCenter::dispatchIf($center->qualhub == 0, $id);"
        result = _find_dispatches(src, {})
        assert _has(result, "SendEmailToNonQualhubCenter", "job")

    def test_create_wallet_passes(self):
        src = "CreateWalletPasses::dispatch($booking->id);"
        result = _find_dispatches(src, {})
        assert _has(result, "CreateWalletPasses", "job")

    def test_notify_graham(self):
        src = "NotifyGrahamAboutHandcuffsPurchased::dispatchIf($product_id == 8, $booking);"
        result = _find_dispatches(src, {})
        assert _has(result, "NotifyGrahamAboutHandcuffsPurchased", "job")

    def test_referrer_email_dispatch(self):
        src = "ReferrerEmail::dispatch($orderId);"
        result = _find_dispatches(src, {})
        assert _has(result, "ReferrerEmail", "job")

    def test_send_contact_hubspot(self):
        src = "SendContact2HubSpot::dispatchIf($booking->firstaid_guru_event_id == 0, $booking);"
        result = _find_dispatches(src, {})
        assert _has(result, "SendContact2HubSpot", "job")

    def test_multiple_dispatches_in_payment_confirmation(self):
        src = """
            CourseBookedJob::dispatchIf($booking->firstaid_guru_event_id == 0, $booking);
            SendEmailToNonQualhubCenter::dispatchIf($center->qualhub == 0, $id);
            CreateNewLicenceOrder::dispatchIf($license_price > 0, $bookingId);
            ReferrerEmail::dispatch($orderId);
            SendContact2HubSpot::dispatchIf($cond, $booking);
            CreateWalletPasses::dispatch($booking->id);
        """
        result = _find_dispatches(src, {})
        names = [r[0] for r in result]
        assert "CourseBookedJob" in names
        assert "SendEmailToNonQualhubCenter" in names
        assert "CreateNewLicenceOrder" in names
        assert "ReferrerEmail" in names
        assert "SendContact2HubSpot" in names
        assert "CreateWalletPasses" in names

    def test_user_service_fixture_pattern(self):
        """Matches the actual tiny-laravel-app UserService::create pattern."""
        src = "event(new UserRegistered($user));"
        result = _find_dispatches(src, {})
        assert _has(result, "UserRegistered", "event")

    def test_no_false_positives_in_plain_code(self):
        src = """
            $user = User::create($data);
            $data['password'] = Hash::make($data['password']);
            return response()->json($user, 201);
        """
        result = _find_dispatches(src, {})
        # Hash::make, User::create, response() should not produce dispatch entries
        # (User doesn't end in "Event"/"Job" and Hash/response are not dispatch calls)
        names = [r[0] for r in result]
        assert "Hash" not in names
        assert "User" not in names
        assert "response" not in names


# ── _resolve_dispatch_target ─────────────────────────────────────────────────

class TestResolveDispatchTarget:
    """Tests for resolving dispatched class names to graph node_ids."""

    def _make_ctx(self, fqn_index: dict) -> MagicMock:
        ctx = MagicMock()
        ctx.fqn_index = fqn_index
        return ctx

    def test_resolves_via_file_alias(self):
        ctx = self._make_ctx({
            "App\\Events\\UserRegistered": "event:App\\Events\\UserRegistered",
        })
        aliases = {"UserRegistered": "App\\Events\\UserRegistered"}
        result = _resolve_dispatch_target("UserRegistered", "event", aliases, {}, ctx)
        assert result == "event:App\\Events\\UserRegistered"

    def test_resolves_via_short_name_reverse_index(self):
        ctx = self._make_ctx({
            "App\\Events\\UserRegistered": "event:App\\Events\\UserRegistered",
        })
        short_to_fqns = {"UserRegistered": ["App\\Events\\UserRegistered"]}
        result = _resolve_dispatch_target("UserRegistered", "event", {}, short_to_fqns, ctx)
        assert result == "event:App\\Events\\UserRegistered"

    def test_prefers_events_directory_over_others(self):
        ctx = self._make_ctx({
            "App\\Events\\CourseBooked": "event:App\\Events\\CourseBooked",
            "App\\Models\\CourseBooked": "class:App\\Models\\CourseBooked",
        })
        short_to_fqns = {
            "CourseBooked": ["App\\Models\\CourseBooked", "App\\Events\\CourseBooked"],
        }
        result = _resolve_dispatch_target("CourseBooked", "event", {}, short_to_fqns, ctx)
        assert result == "event:App\\Events\\CourseBooked"

    def test_prefers_jobs_directory(self):
        ctx = self._make_ctx({
            "App\\Jobs\\SendWelcomeEmail": "job:App\\Jobs\\SendWelcomeEmail",
            "App\\Services\\SendWelcomeEmail": "class:App\\Services\\SendWelcomeEmail",
        })
        short_to_fqns = {
            "SendWelcomeEmail": [
                "App\\Services\\SendWelcomeEmail",
                "App\\Jobs\\SendWelcomeEmail",
            ],
        }
        result = _resolve_dispatch_target("SendWelcomeEmail", "job", {}, short_to_fqns, ctx)
        assert result == "job:App\\Jobs\\SendWelcomeEmail"

    def test_returns_none_when_not_found(self):
        ctx = self._make_ctx({})
        result = _resolve_dispatch_target("NonExistentClass", "event", {}, {}, ctx)
        assert result is None

    def test_alias_takes_priority_over_reverse_index(self):
        """use-statement alias is authoritative — prefer it over short-name guessing."""
        ctx = self._make_ctx({
            "App\\Events\\UserRegistered": "event:App\\Events\\UserRegistered",
            "App\\Models\\UserRegistered": "class:App\\Models\\UserRegistered",
        })
        aliases = {"UserRegistered": "App\\Events\\UserRegistered"}
        short_to_fqns = {
            "UserRegistered": ["App\\Models\\UserRegistered", "App\\Events\\UserRegistered"],
        }
        result = _resolve_dispatch_target("UserRegistered", "event", aliases, short_to_fqns, ctx)
        assert result == "event:App\\Events\\UserRegistered"

    def test_single_candidate_returned_directly(self):
        ctx = self._make_ctx({
            "App\\Jobs\\CreateWalletPasses": "job:App\\Jobs\\CreateWalletPasses",
        })
        short_to_fqns = {"CreateWalletPasses": ["App\\Jobs\\CreateWalletPasses"]}
        result = _resolve_dispatch_target("CreateWalletPasses", "job", {}, short_to_fqns, ctx)
        assert result == "job:App\\Jobs\\CreateWalletPasses"
