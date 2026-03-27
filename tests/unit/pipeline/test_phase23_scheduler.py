"""Unit tests — Fix 4: phase_23 commented scheduler detection."""
from __future__ import annotations

import pytest

from laravelgraph.pipeline.phase_23_schedule import (
    _detect_commented_tasks,
    _extract_schedule_body,
    _split_statements,
)


# ── _detect_commented_tasks ───────────────────────────────────────────────────

class TestDetectCommentedTasks:

    def test_all_active_returns_correct_counts(self):
        source = """
        protected function schedule(Schedule $schedule): void
        {
            $schedule->command('emails:send')->daily();
            $schedule->command('backup:run')->weekly();
        }
        """
        active, commented = _detect_commented_tasks(source)
        assert active == 2
        assert commented == 0

    def test_all_commented_returns_correct_counts(self):
        source = """
        protected function schedule(Schedule $schedule): void
        {
            // $schedule->command('emails:send')->daily();
            // $schedule->command('backup:run')->weekly();
            // $schedule->job(new PurgeUnpaidOrders)->daily();
        }
        """
        active, commented = _detect_commented_tasks(source)
        assert active == 0
        assert commented == 3

    def test_mixed_active_and_commented(self):
        source = """
        protected function schedule(Schedule $schedule): void
        {
            $schedule->command('active:task')->hourly();
            // $schedule->command('disabled:task')->daily();
        }
        """
        active, commented = _detect_commented_tasks(source)
        assert active == 1
        assert commented == 1

    def test_hash_comment_detected(self):
        source = "# $schedule->command('emails:send')->daily();"
        active, commented = _detect_commented_tasks(source)
        assert active == 0
        assert commented == 1

    def test_block_comment_detected(self):
        source = """
        /*
         * $schedule->command('emails:send')->daily();
         */
        """
        active, commented = _detect_commented_tasks(source)
        assert active == 0
        assert commented == 1

    def test_no_schedule_calls_returns_zeros(self):
        source = """
        protected function schedule(Schedule $schedule): void
        {
            // nothing here
        }
        """
        active, commented = _detect_commented_tasks(source)
        assert active == 0
        assert commented == 0

    def test_multiline_chain_on_separate_line_counted_once(self):
        source = """
            $schedule->command('emails:send')
                     ->daily()
                     ->withoutOverlapping();
        """
        # Only the first line has $schedule->
        active, commented = _detect_commented_tasks(source)
        assert active == 1
        assert commented == 0

    def test_real_world_laravel_11_migration_pattern(self):
        """All tasks commented with note 'not executing from here in laravel 11'."""
        source = """
        protected function schedule(Schedule $schedule): void
        {
            // not executing from here in laravel 11
            // $schedule->command('purge:unpaid')->daily();
            // $schedule->command('notify:customers')->everyFiveMinutes();
            // $schedule->job(new ProcessPendingOrders)->hourly();
        }
        """
        active, commented = _detect_commented_tasks(source)
        assert active == 0
        assert commented == 3

    def test_empty_source_returns_zeros(self):
        active, commented = _detect_commented_tasks("")
        assert active == 0
        assert commented == 0


# ── scheduler_disabled derivation ─────────────────────────────────────────────

class TestSchedulerDisabledFlag:

    def _is_disabled(self, source: str) -> bool:
        active, commented = _detect_commented_tasks(source)
        return commented > 0 and active == 0

    def test_disabled_when_all_commented(self):
        source = "// $schedule->command('emails:send')->daily();"
        assert self._is_disabled(source) is True

    def test_not_disabled_when_some_active(self):
        source = """
            $schedule->command('active')->daily();
            // $schedule->command('disabled')->daily();
        """
        assert self._is_disabled(source) is False

    def test_not_disabled_when_all_active(self):
        source = "$schedule->command('emails:send')->daily();"
        assert self._is_disabled(source) is False

    def test_not_disabled_when_no_tasks(self):
        assert self._is_disabled("") is False


# ── ctx.stats populated correctly ─────────────────────────────────────────────

class TestPhase23StatsIntegration:
    """Verify that run() stores the expected stats keys."""

    def _make_ctx(self, kernel_source: str, tmp_path):
        """Build a minimal PipelineContext pointing to a temp Kernel.php."""
        from unittest.mock import MagicMock
        import json

        kernel = tmp_path / "Kernel.php"
        kernel.write_text(kernel_source, encoding="utf-8")

        ctx = MagicMock()
        ctx.project_root = tmp_path
        ctx.php_files = [kernel]
        ctx.stats = {}
        ctx.db = MagicMock()
        ctx.db._insert_node = MagicMock()
        ctx.db.execute = MagicMock(return_value=[])
        ctx.db.upsert_rel = MagicMock()
        return ctx

    def test_disabled_stats_stored(self, tmp_path):
        from laravelgraph.pipeline.phase_23_schedule import run

        source = """<?php
        namespace App\\Console;
        class Kernel extends ConsoleKernel {
            protected function schedule(Schedule $schedule): void {
                // $schedule->command('emails:send')->daily();
                // $schedule->command('backup:run')->weekly();
            }
        }
        """
        # Create the expected directory structure
        console_dir = tmp_path / "app" / "Console"
        console_dir.mkdir(parents=True)
        kernel_file = console_dir / "Kernel.php"
        kernel_file.write_text(source)

        ctx = self._make_ctx(source, tmp_path)
        # Override php_files to be empty so it falls through to candidate_paths
        ctx.php_files = []

        run(ctx)

        assert ctx.stats.get("scheduler_disabled") is True
        assert ctx.stats.get("scheduler_commented_tasks") == 2
        assert ctx.stats.get("scheduled_tasks") == 0

    def test_active_stats_stored(self, tmp_path):
        from laravelgraph.pipeline.phase_23_schedule import run

        source = """<?php
        namespace App\\Console;
        class Kernel extends ConsoleKernel {
            protected function schedule(Schedule $schedule): void {
                $schedule->command('emails:send')->daily();
            }
        }
        """
        console_dir = tmp_path / "app" / "Console"
        console_dir.mkdir(parents=True)
        kernel_file = console_dir / "Kernel.php"
        kernel_file.write_text(source)

        ctx = self._make_ctx(source, tmp_path)
        ctx.php_files = []

        run(ctx)

        assert ctx.stats.get("scheduler_disabled") is False
        assert ctx.stats.get("scheduled_tasks") == 1
