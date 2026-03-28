"""Plugin metadata store.

Tracks per-plugin: status, usage stats, contribution scores, system prompts,
self-improvement history. Stored in .laravelgraph/plugin_meta.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class PluginMeta:
    name: str
    status: str = "active"                  # "active" | "disabled"
    created_at: str = ""                    # ISO timestamp
    last_used: str = ""                     # ISO timestamp or ""
    call_count: int = 0
    empty_result_count: int = 0             # calls returning empty/no data
    error_count: int = 0
    agent_followup_count: int = 0           # agent called another tool immediately after
    self_improvement_count: int = 0
    last_improved_at: str = ""
    improvement_cooldown_until: str = ""    # ISO timestamp — don't improve before this
    system_prompt: str = ""
    removal_reasons: list = field(default_factory=list)  # past reasons for removal
    contribution_score: float = 0.0         # 0.0 - 100.0, computed
    plugin_node_count: int = 0              # nodes written to plugin graph


class PluginMetaStore:
    """File-backed store for per-plugin metadata at index_dir/plugin_meta.json."""

    def __init__(self, index_dir: Path) -> None:
        self._path = index_dir / "plugin_meta.json"
        self._data: dict[str, PluginMeta] = {}
        self._load()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def get(self, name: str) -> PluginMeta | None:
        return self._data.get(name)

    def set(self, meta: PluginMeta) -> None:
        self._data[meta.name] = meta
        self._save()

    def all(self) -> list[PluginMeta]:
        return list(self._data.values())

    def delete(self, name: str) -> None:
        if name in self._data:
            del self._data[name]
            self._save()

    # ── Counters ──────────────────────────────────────────────────────────────

    def log_call(self, name: str, empty: bool = False, error: bool = False) -> None:
        """Increment call counters and update last_used timestamp."""
        meta = self._data.get(name)
        if meta is None:
            return
        meta.call_count += 1
        meta.last_used = datetime.now(timezone.utc).isoformat()
        if empty:
            meta.empty_result_count += 1
        if error:
            meta.error_count += 1
        self._save()

    def log_followup(self, name: str) -> None:
        """Increment agent_followup_count."""
        meta = self._data.get(name)
        if meta is None:
            return
        meta.agent_followup_count += 1
        self._save()

    # ── Status management ─────────────────────────────────────────────────────

    def enable(self, name: str) -> None:
        meta = self._data.get(name)
        if meta is None:
            return
        meta.status = "active"
        self._save()

    def disable(self, name: str) -> None:
        meta = self._data.get(name)
        if meta is None:
            return
        meta.status = "disabled"
        self._save()

    def set_system_prompt(self, name: str, prompt: str) -> None:
        meta = self._data.get(name)
        if meta is None:
            return
        meta.system_prompt = prompt
        self._save()

    def is_active(self, name: str) -> bool:
        meta = self._data.get(name)
        return meta is not None and meta.status == "active"

    # ── Self-improvement ──────────────────────────────────────────────────────

    def check_improvement_needed(self, name: str) -> bool:
        """Return True if this plugin's usage stats suggest it needs self-improvement.

        Checks:
          - call_count > 20 AND empty_result_count/call_count > 0.25
          - call_count > 20 AND error_count/call_count > 0.15
          - call_count > 30 AND agent_followup_count/call_count > 0.40
          AND cooldown has expired (or not set), AND status == "active".
        """
        meta = self._data.get(name)
        if meta is None or meta.status != "active":
            return False

        # Check cooldown
        if meta.improvement_cooldown_until:
            try:
                cooldown_until = datetime.fromisoformat(meta.improvement_cooldown_until)
                if datetime.now(timezone.utc) < cooldown_until:
                    return False
            except ValueError:
                pass

        calls = meta.call_count
        if calls == 0:
            return False

        if calls > 20 and meta.empty_result_count / calls > 0.25:
            return True
        if calls > 20 and meta.error_count / calls > 0.15:
            return True
        if calls > 30 and meta.agent_followup_count / calls > 0.40:
            return True

        return False

    def set_improvement_cooldown(self, name: str, hours: int = 48) -> None:
        """Set the improvement cooldown to now + hours."""
        meta = self._data.get(name)
        if meta is None:
            return
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        meta.improvement_cooldown_until = until.isoformat()
        self._save()

    # ── Contribution scoring ──────────────────────────────────────────────────

    def compute_contribution(
        self,
        name: str,
        total_system_calls: int,
        total_plugin_nodes: int,
    ) -> float:
        """Compute a weighted contribution score 0-100 and store it.

        Weights:
          40% usage:       call_count / max(total_system_calls, 1) * 100
          30% scope:       plugin_node_count / max(total_plugin_nodes, 1) * 100
          30% reliability: (1 - error_rate - empty_rate) * 100
        """
        meta = self._data.get(name)
        if meta is None:
            return 0.0

        calls = meta.call_count
        usage_score = (calls / max(total_system_calls, 1)) * 100.0

        scope_score = (meta.plugin_node_count / max(total_plugin_nodes, 1)) * 100.0

        if calls > 0:
            error_rate = meta.error_count / calls
            empty_rate = meta.empty_result_count / calls
            reliability_score = max(0.0, (1.0 - error_rate - empty_rate)) * 100.0
        else:
            reliability_score = 100.0

        score = (
            0.40 * usage_score
            + 0.30 * scope_score
            + 0.30 * reliability_score
        )
        score = max(0.0, min(100.0, score))
        meta.contribution_score = score
        self._save()
        return score

    # ── System prompts ────────────────────────────────────────────────────────

    def get_all_system_prompts(self) -> list[str]:
        """Return system_prompts of all active plugins that have non-empty prompts."""
        return [
            m.system_prompt
            for m in self._data.values()
            if m.status == "active" and m.system_prompt
        ]

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            with open(self._path) as f:
                raw = json.load(f)
            self._data = {
                name: PluginMeta(**entry)
                for name, entry in raw.items()
            }
        except Exception:
            self._data = {}

    def _save(self) -> None:
        """Write JSON atomically (write to .tmp, rename)."""
        payload = {name: asdict(meta) for name, meta in self._data.items()}
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=".plugin_meta_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
