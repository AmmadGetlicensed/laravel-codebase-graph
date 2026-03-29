"""Configuration management for LaravelGraph."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ── Global paths ──────────────────────────────────────────────────────────────

def _global_dir() -> Path:
    """~/.laravelgraph/ — stores the global registry and logs."""
    d = Path.home() / ".laravelgraph"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_dir(project_root: Path) -> Path:
    """<project>/.laravelgraph/ — per-project graph store."""
    d = project_root / ".laravelgraph"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Configuration model ───────────────────────────────────────────────────────

class EmbeddingConfig(BaseModel):
    enabled: bool = True
    model: str = "BAAI/bge-small-en-v1.5"
    batch_size: int = 64
    dimensions: int = 384


class SearchConfig(BaseModel):
    bm25_weight: float = 0.4
    vector_weight: float = 0.4
    fuzzy_weight: float = 0.2
    top_k: int = 20
    fuzzy_threshold: float = 0.6
    test_file_penalty: float = 0.5
    source_boost: float = 1.2


class PipelineConfig(BaseModel):
    skip_vendor: bool = True
    skip_storage: bool = True
    skip_bootstrap: bool = True
    php_extensions: list[str] = Field(default_factory=lambda: [".php"])
    blade_extension: str = ".blade.php"
    max_file_size_kb: int = 512
    call_confidence_threshold: float = 0.3
    git_history_months: int = 6
    change_coupling_threshold: float = 0.3
    watch_debounce_seconds: float = 30.0


class MCPConfig(BaseModel):
    transport: str = "stdio"  # "stdio" | "http"
    host: str = "127.0.0.1"
    port: int = 3000
    api_key: str = ""         # Bearer token for HTTP transport; empty = no auth
    log_requests: bool = True


class DatabaseConnectionConfig(BaseModel):
    """Configuration for a single live database connection to introspect.

    Supports MySQL and PostgreSQL. Either supply a full ``dsn`` string or the
    individual ``host`` / ``port`` / ``database`` / ``username`` / ``password``
    fields.  Password values may reference environment variables using the
    ``${VAR_NAME}`` syntax — they are resolved at connection time.
    """

    name: str                           # logical name matching Laravel connection key
    driver: str = "mysql"               # mysql | pgsql
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = ""                  # schema / database name
    username: str = ""
    password: str = ""                  # may use ${ENV_VAR} syntax
    dsn: str = ""                       # full DSN overrides individual fields
    analyze_procedures: bool = True
    analyze_views: bool = True
    analyze_triggers: bool = False
    ssl: bool = False                   # enable SSL (recommended for AWS RDS)
    query_cache_ttl: int = 300          # seconds to cache SELECT results (0 = disable)


class LLMConfig(BaseModel):
    """Configuration for LLM providers used for semantic summary generation.

    Supports 18+ providers. Keys are read from environment variables
    automatically — no config file needed for cloud providers.
    Local providers (Ollama, LM Studio, vLLM) require provider="<name>" to activate.

    provider: "auto" = first env var found wins (cloud only)
              "<name>" = use this specific provider
    api_keys: override env var per provider  {"groq": "gsk_...", ...}
    models:   override default model per provider  {"groq": "llama-3.3-70b-versatile"}
    base_urls: override endpoint per provider (custom deployments)
    """
    enabled: bool = True
    provider: str = "auto"
    api_keys: dict = Field(default_factory=dict)
    models: dict = Field(default_factory=dict)
    base_urls: dict = Field(default_factory=dict)
    max_source_lines: int = 50


# Backward-compat alias — existing code that imports SummaryConfig continues to work
SummaryConfig = LLMConfig


class LogConfig(BaseModel):
    level: str = "INFO"
    dir: Path = Field(default_factory=lambda: _global_dir() / "logs")
    json_format: bool = True


class Config(BaseModel):
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    log: LogConfig = Field(default_factory=LogConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    databases: list[DatabaseConnectionConfig] = Field(default_factory=list)

    @field_validator("log", mode="before")
    @classmethod
    def _expand_log_dir(cls, v: Any) -> Any:
        if isinstance(v, dict) and "dir" in v:
            v["dir"] = Path(v["dir"])
        return v

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Config":
        """Load config, merging global defaults with project-level overrides."""
        import json

        base: dict[str, Any] = {}

        # Global config
        global_cfg = _global_dir() / "config.json"
        if global_cfg.exists():
            with open(global_cfg) as f:
                global_data = json.load(f)
                _migrate_llm_key(global_data)
                base.update(global_data)

        # Project-level config
        if project_root:
            project_cfg = _index_dir(project_root) / "config.json"
            if project_cfg.exists():
                with open(project_cfg) as f:
                    project_data = json.load(f)
                    _migrate_llm_key(project_data)
                    _deep_merge(base, project_data)

        # Environment variable overrides
        if lvl := os.environ.get("LARAVELGRAPH_LOG_LEVEL"):
            base.setdefault("log", {})["level"] = lvl
        if port := os.environ.get("LARAVELGRAPH_PORT"):
            base.setdefault("mcp", {})["port"] = int(port)
        if key := os.environ.get("LARAVELGRAPH_API_KEY"):
            base.setdefault("mcp", {})["api_key"] = key

        return cls.model_validate(base)


def _migrate_llm_key(data: dict) -> None:
    """In-place: rename old 'summary' key to 'llm' if 'llm' is absent."""
    if "summary" in data and "llm" not in data:
        data["llm"] = data.pop("summary")


def _deep_merge(base: dict, override: dict) -> None:
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ── Path helpers used throughout the codebase ─────────────────────────────────

def global_dir() -> Path:
    return _global_dir()


def index_dir(project_root: Path) -> Path:
    return _index_dir(project_root)


def registry_path() -> Path:
    return _global_dir() / "repos.json"


def log_dir() -> Path:
    d = _global_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d
