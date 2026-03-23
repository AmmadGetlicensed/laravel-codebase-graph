"""Global repository registry — tracks all indexed Laravel projects."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from laravelgraph.config import registry_path
from laravelgraph.logging import get_logger

logger = get_logger(__name__)


class RepoEntry:
    """Metadata for a single indexed repository."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def path(self) -> str:
        return self._data["path"]

    @property
    def name(self) -> str:
        return self._data.get("name", Path(self.path).name)

    @property
    def indexed_at(self) -> float:
        return self._data.get("indexed_at", 0.0)

    @property
    def laravel_version(self) -> str:
        return self._data.get("laravel_version", "unknown")

    @property
    def php_version(self) -> str:
        return self._data.get("php_version", "unknown")

    @property
    def stats(self) -> dict[str, Any]:
        return self._data.get("stats", {})

    def to_dict(self) -> dict[str, Any]:
        return self._data.copy()


class Registry:
    """Manages the global list of indexed repositories."""

    def __init__(self) -> None:
        self._path = registry_path()

    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"repos": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(data, f, indent=2)

    def register(
        self,
        project_root: Path,
        laravel_version: str = "unknown",
        php_version: str = "unknown",
        stats: dict[str, Any] | None = None,
    ) -> None:
        data = self._load()
        key = str(project_root.resolve())
        data["repos"][key] = {
            "path": key,
            "name": project_root.name,
            "laravel_version": laravel_version,
            "php_version": php_version,
            "indexed_at": time.time(),
            "stats": stats or {},
        }
        self._save(data)
        logger.info("Repository registered", path=key, name=project_root.name)

    def unregister(self, project_root: Path) -> bool:
        data = self._load()
        key = str(project_root.resolve())
        if key in data["repos"]:
            del data["repos"][key]
            self._save(data)
            logger.info("Repository unregistered", path=key)
            return True
        return False

    def all(self) -> list[RepoEntry]:
        data = self._load()
        return [RepoEntry(v) for v in data["repos"].values()]

    def get(self, project_root: Path) -> RepoEntry | None:
        data = self._load()
        key = str(project_root.resolve())
        entry = data["repos"].get(key)
        return RepoEntry(entry) if entry else None

    def is_indexed(self, project_root: Path) -> bool:
        return self.get(project_root) is not None
