"""Deterministic identity for the backend source bytes running this process."""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path


BUILD_IDENTITY_SCHEMA = "hive.build_identity.v1"
_RUNTIME_SOURCE_PATHS = (
    "app",
    "alembic",
    "scripts",
    "agent_template",
    "hr_agent_template",
    "packs",
    "VERSION",
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "entrypoint.sh",
)
_IGNORED_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".DS_Store"})


def _included_files(root: Path, included_paths: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for relative in included_paths:
        candidate = root / relative
        if not candidate.exists():
            raise FileNotFoundError(f"build identity source is missing: {relative}")
        paths = [candidate] if candidate.is_file() else candidate.rglob("*")
        for path in paths:
            relative_path = path.relative_to(root)
            if any(part in _IGNORED_NAMES for part in relative_path.parts) or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise ValueError(f"build identity source contains a symlink: {relative_path.as_posix()}")
            if path.is_file():
                files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def source_build_identity(
    root: Path,
    *,
    included_paths: tuple[str, ...] = _RUNTIME_SOURCE_PATHS,
) -> dict[str, str | int]:
    """Hash canonical path, mode, length, and bytes for the runtime source."""

    resolved_root = root.resolve(strict=True)
    files = _included_files(resolved_root, included_paths)
    if not files:
        raise ValueError("build identity source is empty")
    digest = hashlib.sha256()
    for path in files:
        payload = path.read_bytes()
        relative = path.relative_to(resolved_root).as_posix()
        mode = os.stat(path, follow_symlinks=False).st_mode & 0o777
        digest.update(f"{relative}\0{mode:04o}\0{len(payload)}\0".encode())
        digest.update(payload)
        digest.update(b"\0")
    sha256 = digest.hexdigest()
    return {
        "schema": BUILD_IDENTITY_SCHEMA,
        "status": "ok",
        "revision": f"source-sha256:{sha256}",
        "sha256": sha256,
        "file_count": len(files),
    }


@lru_cache(maxsize=1)
def current_build_identity() -> dict[str, str | int]:
    return source_build_identity(Path(__file__).resolve().parents[1])


__all__ = ["BUILD_IDENTITY_SCHEMA", "current_build_identity", "source_build_identity"]
