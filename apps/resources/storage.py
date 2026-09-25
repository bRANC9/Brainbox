"""Storage adapters.

The source of truth for knowledge is the file system, not PostgreSQL. The
business logic only talks to this adapter, so a Git-backed or S3-backed
implementation can be swapped in later (Phase 2+).
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from django.conf import settings


class StorageError(Exception):
    pass


class LocalStorage:
    """Filesystem storage rooted at ``settings.KNOWLEDGE_DATA_ROOT``."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    # -- path helpers --------------------------------------------------------
    def _validate(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise StorageError(f"Path escapes storage root: {path}")
        return resolved

    def path_for(
        self,
        *,
        workspace_id,
        project_id=None,
        kind: str,
        rel_path: str = "",
    ) -> Path:
        parts = [self.root, "workspaces", str(workspace_id)]
        if project_id:
            parts += ["projects", str(project_id)]
        parts += [kind]
        path = Path(*parts)
        if rel_path:
            path = path / rel_path
        return self._validate(path)

    def workspace_dir(self, workspace_id) -> Path:
        return self._validate(self.root / "workspaces" / str(workspace_id))

    # -- io ------------------------------------------------------------------
    def ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

    def write_text(self, path: Path, content: str) -> None:
        path = self._validate(path)
        self.ensure_parent(path)
        path.write_text(content, encoding="utf-8")

    def read_text(self, path: Path) -> str:
        path = self._validate(path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def write_bytes(self, path: Path, data: bytes) -> None:
        path = self._validate(path)
        self.ensure_parent(path)
        path.write_bytes(data)

    def read_bytes(self, path: Path) -> bytes:
        path = self._validate(path)
        return path.read_bytes()

    def exists(self, path: Path) -> bool:
        return self._validate(path).exists()

    def delete(self, path: Path) -> None:
        path = self._validate(path)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


@lru_cache(maxsize=1)
def get_storage() -> LocalStorage:
    return LocalStorage(settings.KNOWLEDGE_DATA_ROOT)
