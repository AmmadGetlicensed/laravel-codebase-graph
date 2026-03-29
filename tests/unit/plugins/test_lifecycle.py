"""Tests for the Plugin Lifecycle Engine.

Covers:
  - detect_feature_gaps() — Feature node gap detection
  - get_domain_query_frequencies() — log mining
  - take_domain_snapshot() — graph snapshot capture
  - check_domain_drift() — drift detection logic
  - PluginMetaStore new methods — set_cooldown, increment_self_improvement_count,
    set_last_improved_at, set_domain_coverage_snapshot, is_in_cooldown
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from laravelgraph.plugins.meta import PluginMeta, PluginMetaStore
from laravelgraph.plugins.suggest import detect_feature_gaps


# ── PluginMeta new fields ─────────────────────────────────────────────────────

class TestPluginMetaNewFields:
    def test_domain_coverage_snapshot_default_empty(self):
        meta = PluginMeta(name="test-plugin")
        assert meta.domain_coverage_snapshot == {}

    def test_domain_coverage_snapshot_stored(self):
        snap = {"route_count": 5, "model_count": 2}
        meta = PluginMeta(name="test-plugin", domain_coverage_snapshot=snap)
        assert meta.domain_coverage_snapshot["route_count"] == 5


# ── PluginMetaStore new methods ───────────────────────────────────────────────

class TestPluginMetaStoreNewMethods:
    @pytest.fixture
    def store(self, tmp_path):
        s = PluginMetaStore(tmp_path)
        s.set(PluginMeta(name="my-plugin", status="active"))
        return s

    def test_set_cooldown_sets_iso_timestamp(self, store):
        store.set_cooldown("my-plugin", hours=24)
        meta = store.get("my-plugin")
        assert meta is not None
        assert meta.improvement_cooldown_until != ""
        # Should parse as ISO datetime
        from datetime import datetime
        dt = datetime.fromisoformat(meta.improvement_cooldown_until)
        assert dt is not None

    def test_set_cooldown_default_hours(self, store):
        store.set_cooldown("my-plugin")
        assert store.is_in_cooldown("my-plugin")

    def test_set_cooldown_noop_on_missing(self, store):
        store.set_cooldown("nonexistent", hours=10)  # must not raise

    def test_is_in_cooldown_false_without_cooldown(self, store):
        assert not store.is_in_cooldown("my-plugin")

    def test_is_in_cooldown_true_after_set(self, store):
        store.set_cooldown("my-plugin", hours=48)
        assert store.is_in_cooldown("my-plugin")

    def test_is_in_cooldown_false_for_missing_plugin(self, store):
        assert not store.is_in_cooldown("does-not-exist")

    def test_increment_self_improvement_count(self, store):
        store.increment_self_improvement_count("my-plugin")
        store.increment_self_improvement_count("my-plugin")
        meta = store.get("my-plugin")
        assert meta.self_improvement_count == 2

    def test_increment_noop_on_missing(self, store):
        store.increment_self_improvement_count("missing")  # must not raise

    def test_set_last_improved_at_now(self, store):
        before = time.time()
        store.set_last_improved_at("my-plugin")
        after = time.time()
        meta = store.get("my-plugin")
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(meta.last_improved_at).timestamp()
        assert before <= dt <= after

    def test_set_last_improved_at_with_unix_ts(self, store):
        ts = 1700000000.0
        store.set_last_improved_at("my-plugin", ts=ts)
        meta = store.get("my-plugin")
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(meta.last_improved_at)
        assert abs(dt.timestamp() - ts) < 1.0

    def test_set_domain_coverage_snapshot(self, store):
        snap = {"route_count": 3, "model_count": 1, "event_count": 0, "has_changes": False}
        store.set_domain_coverage_snapshot("my-plugin", snap)
        meta = store.get("my-plugin")
        assert meta.domain_coverage_snapshot["route_count"] == 3

    def test_set_domain_coverage_snapshot_noop_on_missing(self, store):
        store.set_domain_coverage_snapshot("nonexistent", {"x": 1})  # must not raise

    def test_meta_persists_across_reload(self, tmp_path):
        s1 = PluginMetaStore(tmp_path)
        s1.set(PluginMeta(name="p", status="active"))
        s1.set_domain_coverage_snapshot("p", {"route_count": 7})
        s1.set_cooldown("p", hours=24)

        s2 = PluginMetaStore(tmp_path)
        meta = s2.get("p")
        assert meta.domain_coverage_snapshot["route_count"] == 7
        assert s2.is_in_cooldown("p")


# ── detect_feature_gaps ───────────────────────────────────────────────────────

class TestDetectFeatureGaps:
    def _make_db(self, rows):
        db = MagicMock()
        db.execute.return_value = rows
        return db

    def _make_meta_store(self, existing_names=None):
        store = MagicMock()
        metas = [MagicMock(name=n) for n in (existing_names or [])]
        store.all.return_value = metas
        return store

    def test_returns_empty_when_no_feature_nodes(self, tmp_path):
        db = self._make_db([])
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        assert result == []

    def test_returns_gap_for_uncovered_feature(self, tmp_path):
        db = self._make_db([
            {"slug": "payment", "name": "Payment", "symbol_count": 50, "has_changes": False}
        ])
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        assert len(result) == 1
        assert result[0]["slug"] == "payment"

    def test_skips_feature_already_in_meta_store(self, tmp_path):
        db = self._make_db([
            {"slug": "payment", "name": "Payment", "symbol_count": 50, "has_changes": False}
        ])
        existing = MagicMock()
        existing.name = "payment"
        store = MagicMock()
        store.all.return_value = [existing]
        result = detect_feature_gaps(db, store, tmp_path)
        assert result == []

    def test_skips_feature_with_existing_plugin_file(self, tmp_path):
        db = self._make_db([
            {"slug": "payment", "name": "Payment", "symbol_count": 50, "has_changes": False}
        ])
        store = self._make_meta_store()
        (tmp_path / "payment.py").write_text("# plugin")
        result = detect_feature_gaps(db, store, tmp_path)
        assert result == []

    def test_score_proportional_to_symbol_count(self, tmp_path):
        db = self._make_db([
            {"slug": "big", "name": "Big", "symbol_count": 100, "has_changes": False},
            {"slug": "small", "name": "Small", "symbol_count": 20, "has_changes": False},
        ])
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        scores = {r["slug"]: r["score"] for r in result}
        assert scores["big"] > scores["small"]

    def test_score_capped_at_10(self, tmp_path):
        db = self._make_db([
            {"slug": "huge", "name": "Huge", "symbol_count": 9999, "has_changes": False}
        ])
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        assert result[0]["score"] == 10.0

    def test_source_is_feature_gap(self, tmp_path):
        db = self._make_db([
            {"slug": "orders", "name": "Orders", "symbol_count": 30, "has_changes": False}
        ])
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        assert result[0]["source"] == "feature_gap"

    def test_db_exception_returns_empty(self, tmp_path):
        db = MagicMock()
        db.execute.side_effect = RuntimeError("DB error")
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        assert result == []

    def test_has_changes_flag_propagated(self, tmp_path):
        db = self._make_db([
            {"slug": "events", "name": "Events", "symbol_count": 25, "has_changes": True}
        ])
        store = self._make_meta_store()
        result = detect_feature_gaps(db, store, tmp_path)
        assert result[0]["has_changes"] is True


# ── get_domain_query_frequencies ─────────────────────────────────────────────

class TestGetDomainQueryFrequencies:
    def _write_log(self, log_dir: Path, entries: list[dict]) -> None:
        log_file = log_dir / "test.jsonl"
        lines = [json.dumps(e) for e in entries]
        log_file.write_text("\n".join(lines), encoding="utf-8")

    def test_returns_empty_for_empty_log_dir(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        result = get_domain_query_frequencies(tmp_path)
        assert result == []

    def test_counts_feature_context_calls(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        entries = [
            {"tool": "laravelgraph_feature_context", "feature": "user management",
             "timestamp": "2026-01-01T10:00:00+00:00"},
            {"tool": "laravelgraph_feature_context", "feature": "user management",
             "timestamp": "2026-01-01T11:00:00+00:00"},
            {"tool": "laravelgraph_feature_context", "feature": "user management",
             "timestamp": "2026-01-01T12:00:00+00:00"},
        ]
        self._write_log(tmp_path, entries)
        result = get_domain_query_frequencies(tmp_path, since_hours=0, min_calls=3)
        slugs = [r["slug"] for r in result]
        assert any("user" in s for s in slugs)

    def test_counts_explain_calls(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        entries = [
            {"tool": "laravelgraph_explain", "feature": "payment flow",
             "timestamp": "2026-01-01T10:00:00+00:00"},
            {"tool": "laravelgraph_explain", "feature": "payment flow",
             "timestamp": "2026-01-01T11:00:00+00:00"},
            {"tool": "laravelgraph_explain", "feature": "payment flow",
             "timestamp": "2026-01-01T12:00:00+00:00"},
        ]
        self._write_log(tmp_path, entries)
        result = get_domain_query_frequencies(tmp_path, since_hours=0, min_calls=3)
        assert len(result) >= 1
        assert result[0]["count"] >= 3

    def test_respects_min_calls_threshold(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        entries = [
            {"tool": "laravelgraph_feature_context", "feature": "rare domain",
             "timestamp": "2026-01-01T10:00:00+00:00"},
            {"tool": "laravelgraph_feature_context", "feature": "rare domain",
             "timestamp": "2026-01-01T11:00:00+00:00"},
        ]
        self._write_log(tmp_path, entries)
        result = get_domain_query_frequencies(tmp_path, since_hours=0, min_calls=5)
        assert result == []

    def test_ignores_irrelevant_tool_calls(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        entries = [
            {"tool": "laravelgraph_routes", "feature": "user management"},
            {"tool": "laravelgraph_models", "feature": "user management"},
            {"tool": "laravelgraph_routes", "feature": "user management"},
        ]
        self._write_log(tmp_path, entries)
        result = get_domain_query_frequencies(tmp_path, since_hours=0, min_calls=1)
        assert result == []

    def test_sorted_by_count_descending(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        entries = []
        for _ in range(5):
            entries.append({"tool": "laravelgraph_feature_context", "feature": "payment",
                            "timestamp": "2026-01-01T10:00:00+00:00"})
        for _ in range(3):
            entries.append({"tool": "laravelgraph_feature_context", "feature": "users",
                            "timestamp": "2026-01-01T10:00:00+00:00"})
        self._write_log(tmp_path, entries)
        result = get_domain_query_frequencies(tmp_path, since_hours=0, min_calls=1)
        assert result[0]["count"] >= result[-1]["count"]

    def test_result_has_required_keys(self, tmp_path):
        from laravelgraph.logging_manager import get_domain_query_frequencies
        entries = [
            {"tool": "laravelgraph_feature_context", "feature": "something",
             "timestamp": "2026-01-01T10:00:00+00:00"},
            {"tool": "laravelgraph_feature_context", "feature": "something",
             "timestamp": "2026-01-01T11:00:00+00:00"},
            {"tool": "laravelgraph_feature_context", "feature": "something",
             "timestamp": "2026-01-01T12:00:00+00:00"},
        ]
        self._write_log(tmp_path, entries)
        result = get_domain_query_frequencies(tmp_path, since_hours=0, min_calls=1)
        assert len(result) > 0
        for r in result:
            assert "slug" in r
            assert "count" in r
            assert "last_seen" in r


# ── take_domain_snapshot ──────────────────────────────────────────────────────

class TestTakeDomainSnapshot:
    def test_returns_zero_counts_when_db_none(self):
        from laravelgraph.plugins.self_improve import take_domain_snapshot
        snap = take_domain_snapshot(None, "payment")
        assert snap == {"route_count": 0, "model_count": 0, "event_count": 0, "has_changes": False}

    def test_captures_route_count(self):
        from laravelgraph.plugins.self_improve import take_domain_snapshot
        db = MagicMock()
        db.execute.side_effect = [
            [{"hc": False}],            # Feature node query
            [{"cnt": 5}],               # Route count
            [{"cnt": 2}],               # EloquentModel count
            [{"cnt": 1}],               # Event count
        ]
        snap = take_domain_snapshot(db, "payment")
        assert snap["route_count"] == 5
        assert snap["model_count"] == 2
        assert snap["event_count"] == 1

    def test_handles_db_exception_gracefully(self):
        from laravelgraph.plugins.self_improve import take_domain_snapshot
        db = MagicMock()
        db.execute.side_effect = RuntimeError("DB error")
        snap = take_domain_snapshot(db, "payment")
        assert snap["route_count"] == 0  # defaults, no exception raised

    def test_captures_has_changes_flag(self):
        from laravelgraph.plugins.self_improve import take_domain_snapshot
        db = MagicMock()
        db.execute.side_effect = [
            [{"hc": True}],
            [{"cnt": 3}],
            [{"cnt": 1}],
            [{"cnt": 0}],
        ]
        snap = take_domain_snapshot(db, "events")
        assert snap["has_changes"] is True


# ── check_domain_drift ────────────────────────────────────────────────────────

class TestCheckDomainDrift:
    def _meta_with_snapshot(self, snap: dict) -> PluginMeta:
        return PluginMeta(name="test-plugin", domain_coverage_snapshot=snap)

    def _db_with_counts(self, route=0, model=0, event=0, has_changes=False):
        db = MagicMock()
        db.execute.side_effect = [
            [{"hc": has_changes}],
            [{"cnt": route}],
            [{"cnt": model}],
            [{"cnt": event}],
        ]
        return db

    def test_no_drift_when_no_snapshot(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = PluginMeta(name="test-plugin")  # empty snapshot
        db = MagicMock()
        assert check_domain_drift(db, meta) is False

    def test_no_drift_when_counts_stable(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 5, "model_count": 2, "event_count": 0})
        db = self._db_with_counts(route=5, model=2, event=0)
        assert check_domain_drift(db, meta) is False

    def test_drift_when_route_count_increases_over_20_pct(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 10, "model_count": 2})
        db = self._db_with_counts(route=13, model=2)  # +30%
        assert check_domain_drift(db, meta) is True

    def test_no_drift_when_route_change_under_20_pct(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 10, "model_count": 2})
        db = self._db_with_counts(route=11, model=2)  # +10%
        assert check_domain_drift(db, meta) is False

    def test_drift_when_model_count_increases(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 5, "model_count": 2})
        db = self._db_with_counts(route=5, model=3)  # new model
        assert check_domain_drift(db, meta) is True

    def test_no_drift_when_model_count_same(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 5, "model_count": 2})
        db = self._db_with_counts(route=5, model=2)
        assert check_domain_drift(db, meta) is False

    def test_drift_when_has_changes_true(self):
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 5, "model_count": 2, "has_changes": False})
        db = self._db_with_counts(route=5, model=2, has_changes=True)
        assert check_domain_drift(db, meta) is True

    def test_no_drift_when_route_count_zero_in_snapshot(self):
        """When snapshot route_count is 0, the 20% threshold check is skipped."""
        from laravelgraph.plugins.self_improve import check_domain_drift
        meta = self._meta_with_snapshot({"route_count": 0, "model_count": 1})
        db = self._db_with_counts(route=5, model=1)
        # route_count 0 → skip route drift; model count unchanged → no drift
        assert check_domain_drift(db, meta) is False
