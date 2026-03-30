"""Unit tests for schema_sync — auto-extension of REL_TYPES."""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from laravelgraph.pipeline.schema_sync import _scan_dir, sync_schema
import laravelgraph.core.schema as _schema


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_pairs(rel_name: str) -> set[tuple[str, str]]:
    for name, pairs, _ in _schema.REL_TYPES:
        if name == rel_name:
            return {tuple(p) for p in pairs}  # type: ignore[misc]
    return set()


def _remove_pair(rel_name: str, pair: tuple[str, str]) -> None:
    """Cleanup helper: remove a pair added during a test."""
    for name, pairs, _ in _schema.REL_TYPES:
        if name == rel_name and isinstance(pairs, list):
            try:
                pairs.remove(list(pair))
            except ValueError:
                pass


# ── _scan_dir ─────────────────────────────────────────────────────────────────

class TestScanDir:

    def test_detects_attribute_form(self, tmp_path: Path) -> None:
        (tmp_path / "phase_01.py").write_text(
            'db.upsert_rel("CALLS", "Method", m_nid, "Job", j_nid, {})'
        )
        assert _scan_dir(tmp_path) == {"CALLS": {("Method", "Job")}}

    def test_detects_bare_function_form(self, tmp_path: Path) -> None:
        (tmp_path / "phase_02.py").write_text(
            'upsert_rel("DISPATCHES", "Listener", l_nid, "Job", j_nid, props)'
        )
        assert _scan_dir(tmp_path) == {"DISPATCHES": {("Listener", "Job")}}

    def test_ignores_dynamic_args(self, tmp_path: Path) -> None:
        (tmp_path / "phase_03.py").write_text(
            'db.upsert_rel(rel_type, from_label, a, to_label, b, {})'
        )
        assert _scan_dir(tmp_path) == {}

    def test_multiple_calls_same_rel(self, tmp_path: Path) -> None:
        (tmp_path / "phase_04.py").write_text(textwrap.dedent("""\
            db.upsert_rel("CALLS", "Method", a, "Method", b, {})
            db.upsert_rel("CALLS", "Method", a, "Function_", b, {})
        """))
        assert _scan_dir(tmp_path) == {"CALLS": {("Method", "Method"), ("Method", "Function_")}}

    def test_multiple_files_merged(self, tmp_path: Path) -> None:
        (tmp_path / "phase_05.py").write_text('db.upsert_rel("CALLS", "Method", a, "Job", b, {})')
        (tmp_path / "phase_06.py").write_text('db.upsert_rel("SCHEDULES", "ScheduledTask", a, "Job", b, {})')
        assert _scan_dir(tmp_path) == {
            "CALLS": {("Method", "Job")},
            "SCHEDULES": {("ScheduledTask", "Job")},
        }

    def test_respects_glob_pattern(self, tmp_path: Path) -> None:
        """Files not matching the glob are ignored."""
        (tmp_path / "helper.py").write_text('db.upsert_rel("CALLS", "Method", a, "Job", b, {})')
        # Default glob is phase_*.py — helper.py should not match
        assert _scan_dir(tmp_path) == {}
        # With *.py glob it should be found
        assert _scan_dir(tmp_path, glob="*.py") == {"CALLS": {("Method", "Job")}}

    def test_handles_syntax_error_gracefully(self, tmp_path: Path) -> None:
        (tmp_path / "phase_bad.py").write_text("def broken(: pass")
        assert _scan_dir(tmp_path) == {}

    def test_empty_directory(self, tmp_path: Path) -> None:
        assert _scan_dir(tmp_path) == {}


# ── sync_schema ───────────────────────────────────────────────────────────────

class TestSyncSchema:

    def test_known_existing_pair_not_duplicated(self, tmp_path: Path) -> None:
        # ("Method", "Event") already exists in DISPATCHES — should not be re-added
        (tmp_path / "phase_x.py").write_text(
            'db.upsert_rel("DISPATCHES", "Method", a, "Event", b, {})'
        )
        before = len(_get_pairs("DISPATCHES"))
        # Patch _PIPELINE_DIR so only our tmp_path is scanned
        with patch("laravelgraph.pipeline.schema_sync._PIPELINE_DIR", tmp_path):
            added = sync_schema()
        assert added == 0
        assert len(_get_pairs("DISPATCHES")) == before

    def test_new_pair_is_added(self, tmp_path: Path) -> None:
        (tmp_path / "phase_x.py").write_text(
            'db.upsert_rel("DISPATCHES", "Controller", a, "Job", b, {})'
        )
        assert ("Controller", "Job") not in _get_pairs("DISPATCHES")

        with patch("laravelgraph.pipeline.schema_sync._PIPELINE_DIR", tmp_path):
            added = sync_schema()

        assert added == 1
        assert ("Controller", "Job") in _get_pairs("DISPATCHES")

        _remove_pair("DISPATCHES", ("Controller", "Job"))

    def test_extra_scan_dir_uses_star_glob(self, tmp_path: Path) -> None:
        """Plugin files (not phase_*.py) in extra_scan_dirs must be picked up."""
        empty_pipeline = tmp_path / "pipeline"
        empty_pipeline.mkdir()
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        (plugins_dir / "my_plugin.py").write_text(
            'db.upsert_rel("DISPATCHES", "ServiceProvider", a, "Job", b, {})'
        )
        assert ("ServiceProvider", "Job") not in _get_pairs("DISPATCHES")

        with patch("laravelgraph.pipeline.schema_sync._PIPELINE_DIR", empty_pipeline):
            added = sync_schema(extra_scan_dirs=[plugins_dir])

        assert added == 1
        assert ("ServiceProvider", "Job") in _get_pairs("DISPATCHES")

        _remove_pair("DISPATCHES", ("ServiceProvider", "Job"))

    def test_unknown_rel_type_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "phase_x.py").write_text(
            'db.upsert_rel("NONEXISTENT_REL", "Method", a, "Job", b, {})'
        )
        with patch("laravelgraph.pipeline.schema_sync._PIPELINE_DIR", tmp_path):
            added = sync_schema()
        assert added == 0

    def test_nonexistent_extra_dir_is_ignored(self, tmp_path: Path) -> None:
        empty = tmp_path / "pipeline"
        empty.mkdir()
        with patch("laravelgraph.pipeline.schema_sync._PIPELINE_DIR", empty):
            added = sync_schema(extra_scan_dirs=[tmp_path / "does_not_exist"])
        assert added == 0

    def test_pipeline_dir_scanned_by_default(self) -> None:
        """Calling sync_schema() with no args should not crash and returns an int."""
        added = sync_schema()
        assert isinstance(added, int)
        assert added >= 0
