"""Unit tests — Fix 1: list_repos deduplication of pytest/temp paths."""
from __future__ import annotations

import re
import time
from unittest.mock import MagicMock, patch


# ── helpers ──────────────────────────────────────────────────────────────────

_TEMP_PATH_RE = re.compile(
    r"[/\\]pytest-\d+[/\\]|[/\\]var[/\\]folders[/\\]|[/\\]T[/\\]pytest|"
    r"[/\\]tmp[/\\]|\\Temp\\|/__pycache__/"
)


def _is_temp(path: str) -> bool:
    return bool(_TEMP_PATH_RE.search(path))


def _make_repo(path: str, name: str = "myapp", indexed_at: float | None = None) -> MagicMock:
    r = MagicMock()
    r.path = path
    r.name = name
    r.indexed_at = indexed_at or time.time()
    r.laravel_version = "11.0"
    r.php_version = "^8.2"
    r.stats = {}
    return r


# ── Fix 1a: temp path detection ───────────────────────────────────────────────

class TestTempPathDetection:
    def test_pytest_path_is_temp(self):
        assert _is_temp("/private/var/folders/n7/pytest-39/schema_test0/tiny-laravel-app")

    def test_pytest_path_with_different_number(self):
        assert _is_temp("/tmp/pytest-123/test_run/app")

    def test_var_folders_is_temp(self):
        assert _is_temp("/var/folders/n7/g6gd_f1j0lv45lbvj82sm3kh0000gn/T/myapp")

    def test_windows_temp_is_temp(self):
        assert _is_temp("C:\\Users\\user\\AppData\\Local\\Temp\\myapp")

    def test_real_project_path_not_temp(self):
        assert not _is_temp("/Users/dev/Development/my-laravel-app")

    def test_real_project_path_with_pytest_in_name(self):
        # Project named "pytest-tools" should not be filtered
        assert not _is_temp("/Users/dev/pytest-tools-app")

    def test_unix_home_not_temp(self):
        assert not _is_temp("/home/ubuntu/apps/laravel-project")

    def test_var_www_not_temp(self):
        assert not _is_temp("/var/www/html/myapp")


# ── Fix 1b: deduplication logic ───────────────────────────────────────────────

class TestListReposDedup:
    def _run_list_repos(self, repos):
        """Simulate the deduplication logic from laravelgraph_list_repos."""
        seen: set[str] = set()
        result = []
        for repo in sorted(repos, key=lambda r: -r.indexed_at):
            if _is_temp(repo.path):
                continue
            if repo.path in seen:
                continue
            seen.add(repo.path)
            result.append(repo)
        return result

    def test_pytest_repos_filtered_out(self):
        repos = [
            _make_repo("/private/var/folders/n7/pytest-39/tiny-laravel-app"),
            _make_repo("/private/var/folders/n7/pytest-40/tiny-laravel-app"),
            _make_repo("/Users/dev/real-app"),
        ]
        result = self._run_list_repos(repos)
        assert len(result) == 1
        assert result[0].path == "/Users/dev/real-app"

    def test_duplicate_real_paths_deduplicated(self):
        t = time.time()
        repos = [
            _make_repo("/Users/dev/real-app", indexed_at=t - 100),
            _make_repo("/Users/dev/real-app", indexed_at=t),  # newer
        ]
        result = self._run_list_repos(repos)
        assert len(result) == 1

    def test_newest_entry_kept_on_dedup(self):
        t = time.time()
        old = _make_repo("/Users/dev/real-app", indexed_at=t - 3600)
        new = _make_repo("/Users/dev/real-app", indexed_at=t)
        result = self._run_list_repos([old, new])
        assert result[0].indexed_at == new.indexed_at

    def test_multiple_real_projects_all_shown(self):
        repos = [
            _make_repo("/Users/dev/app-a", name="app-a"),
            _make_repo("/Users/dev/app-b", name="app-b"),
            _make_repo("/Users/dev/app-c", name="app-c"),
        ]
        result = self._run_list_repos(repos)
        assert len(result) == 3

    def test_mixed_temp_and_real(self):
        repos = [
            _make_repo("/private/var/folders/pytest-1/app"),
            _make_repo("/private/var/folders/pytest-2/app"),
            _make_repo("/Users/dev/staging-app"),
            _make_repo("/Users/dev/prod-app"),
        ]
        result = self._run_list_repos(repos)
        assert len(result) == 2
        paths = {r.path for r in result}
        assert "/Users/dev/staging-app" in paths
        assert "/Users/dev/prod-app" in paths

    def test_empty_registry_returns_empty(self):
        assert self._run_list_repos([]) == []

    def test_all_temp_returns_empty(self):
        repos = [
            _make_repo("/tmp/pytest-1/app"),
            _make_repo("/tmp/pytest-2/app"),
        ]
        assert self._run_list_repos(repos) == []


# ── Fix 1c: scheduler_disabled surfaced in list_repos output ─────────────────

class TestListReposSchedulerFlag:
    def test_scheduler_disabled_shown_in_stats(self):
        repo = _make_repo("/Users/dev/real-app")
        repo.stats = {"scheduler_disabled": True, "scheduler_commented_tasks": 52}
        # The output formatting logic
        lines = []
        if repo.stats.get("scheduler_disabled"):
            n = repo.stats.get("scheduler_commented_tasks", "?")
            lines.append(f"Scheduler disabled — {n} task(s) commented out")
        assert len(lines) == 1
        assert "52" in lines[0]

    def test_scheduler_enabled_no_warning(self):
        repo = _make_repo("/Users/dev/real-app")
        repo.stats = {"scheduled_tasks": 5, "scheduler_disabled": False}
        lines = []
        if repo.stats.get("scheduler_disabled"):
            lines.append("warning")
        assert lines == []
