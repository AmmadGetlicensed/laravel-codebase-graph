"""Tests for the LLMConfig / config key migration (summary → llm)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from laravelgraph.config import Config, LLMConfig, SummaryConfig


# ── LLMConfig defaults ────────────────────────────────────────────────────────

class TestLLMConfigDefaults:
    def test_provider_defaults_to_auto(self):
        cfg = LLMConfig()
        assert cfg.provider == "auto"

    def test_enabled_defaults_to_true(self):
        cfg = LLMConfig()
        assert cfg.enabled is True

    def test_api_keys_default_empty(self):
        cfg = LLMConfig()
        assert cfg.api_keys == {}

    def test_models_default_empty(self):
        cfg = LLMConfig()
        assert cfg.models == {}

    def test_base_urls_default_empty(self):
        cfg = LLMConfig()
        assert cfg.base_urls == {}

    def test_summary_config_alias_is_llm_config(self):
        """SummaryConfig backward-compat alias must resolve to LLMConfig."""
        assert SummaryConfig is LLMConfig


@pytest.fixture(autouse=True)
def isolate_global_config(tmp_path: Path, monkeypatch):
    """Redirect global config dir to tmp_path/global so the real ~/.laravelgraph
    config doesn't bleed into tests."""
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    monkeypatch.setattr("laravelgraph.config._global_dir", lambda: global_dir)


# ── Config field rename ───────────────────────────────────────────────────────

class TestConfigLLMField:
    def test_config_has_llm_field(self):
        cfg = Config()
        assert hasattr(cfg, "llm")
        assert isinstance(cfg.llm, LLMConfig)

    def test_config_has_no_summary_field(self):
        cfg = Config()
        assert not hasattr(cfg, "summary")

    def test_llm_field_defaults(self):
        cfg = Config()
        assert cfg.llm.provider == "auto"
        assert cfg.llm.enabled is True


# ── Migration: "summary" → "llm" ─────────────────────────────────────────────

class TestConfigMigration:
    def test_migration_summary_key_to_llm(self, tmp_path: Path):
        """Config with old 'summary' key loads into cfg.llm correctly."""
        cfg_file = tmp_path / ".laravelgraph" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({
            "summary": {
                "provider": "groq",
                "api_keys": {"groq": "gsk-test"},
                "models": {"groq": "llama-3.3-70b-versatile"},
            }
        }))
        cfg = Config.load(tmp_path)
        assert cfg.llm.provider == "groq"
        assert cfg.llm.api_keys == {"groq": "gsk-test"}
        assert cfg.llm.models == {"groq": "llama-3.3-70b-versatile"}

    def test_migration_llm_key_untouched(self, tmp_path: Path):
        """Config with new 'llm' key is loaded without modification."""
        cfg_file = tmp_path / ".laravelgraph" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({
            "llm": {
                "provider": "openai",
                "api_keys": {"openai": "sk-test"},
            }
        }))
        cfg = Config.load(tmp_path)
        assert cfg.llm.provider == "openai"
        assert cfg.llm.api_keys == {"openai": "sk-test"}

    def test_migration_both_keys_llm_wins(self, tmp_path: Path):
        """If both 'summary' and 'llm' keys exist, 'llm' takes precedence."""
        cfg_file = tmp_path / ".laravelgraph" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({
            "summary": {"provider": "old-groq"},
            "llm": {"provider": "new-openai"},
        }))
        cfg = Config.load(tmp_path)
        assert cfg.llm.provider == "new-openai"

    def test_migration_empty_summary_section(self, tmp_path: Path):
        """Empty 'summary' key migrates to defaults."""
        cfg_file = tmp_path / ".laravelgraph" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({"summary": {}}))
        cfg = Config.load(tmp_path)
        assert cfg.llm.provider == "auto"
        assert cfg.llm.enabled is True

    def test_migration_preserves_other_config_keys(self, tmp_path: Path):
        """Migration of 'summary' does not affect other config sections."""
        cfg_file = tmp_path / ".laravelgraph" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text(json.dumps({
            "summary": {"provider": "groq"},
            "mcp": {"port": 4000},
        }))
        cfg = Config.load(tmp_path)
        assert cfg.llm.provider == "groq"
        assert cfg.mcp.port == 4000

    def test_migration_no_disk_write(self, tmp_path: Path):
        """Migration happens in-memory only; disk file keeps old 'summary' key."""
        cfg_file = tmp_path / ".laravelgraph" / "config.json"
        cfg_file.parent.mkdir(parents=True)
        original = {"summary": {"provider": "groq"}}
        cfg_file.write_text(json.dumps(original))
        Config.load(tmp_path)  # trigger migration
        # File should still have the old key on disk
        disk_data = json.loads(cfg_file.read_text())
        assert "summary" in disk_data
        assert "llm" not in disk_data

    def test_no_config_file_uses_defaults(self, tmp_path: Path):
        """Loading with no config file at all yields default LLMConfig."""
        cfg = Config.load(tmp_path)
        assert cfg.llm.provider == "auto"
        assert cfg.llm.enabled is True
