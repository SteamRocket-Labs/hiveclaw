from __future__ import annotations

import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_production_docker_images_install_bubblewrap_for_code_sandbox() -> None:
    """Linux production images must include the bwrap runner required by
    HIVE_CODE_SANDBOX_MODE=auto; otherwise code_exec/run_command fail closed
    in production even though local macOS tests pass via sandbox-exec."""

    for dockerfile in ("Dockerfile", "backend/Dockerfile"):
        text = _read(dockerfile)
        production_stage = text.split("FROM python:3.12-slim AS production", 1)[1]
        assert re.search(r"\bbubblewrap\b", production_stage), f"{dockerfile} production image lacks bubblewrap"


def test_docker_compose_forwards_debug_and_secrets_master_key() -> None:
    """docker-compose must expose the same envs that startup validates."""

    text = _read("docker-compose.yml")
    backend_block = text.split("  backend:", 1)[1].split("  frontend:", 1)[0]

    assert "DEBUG: ${DEBUG:-false}" in backend_block
    assert "SECRETS_MASTER_KEY: ${SECRETS_MASTER_KEY:-}" in backend_block


def test_harness_ci_uses_fail_fast_postgres_and_keeps_sqlite_test_only() -> None:
    """Unit tests inject sessions; real DB tests provision PostgreSQL explicitly."""

    workflow = _read(".github/workflows/harness-ci.yml")
    assert "postgresql+asyncpg://hive:hive@127.0.0.1:1/hive_ci_no_db" in workflow
    assert "sqlite+aiosqlite" not in workflow

    pyproject = tomllib.loads(_read("backend/pyproject.toml"))
    dependencies = pyproject["project"]["dependencies"]
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    assert not any(dependency.split(">=", 1)[0] == "aiosqlite" for dependency in dependencies)
    assert any(dependency.split(">=", 1)[0] == "aiosqlite" for dependency in dev_dependencies)


def test_shipping_builds_consume_the_locked_dependency_set() -> None:
    """Images install the committed lock and use the same uv release as CI.

    uv 0.9.21 --frozen installs a present lock without checking pyproject drift.
    --locked is the check that fails closed when the lock is missing or stale.
    """

    workflow = _read(".github/workflows/harness-ci.yml")
    root = _read("Dockerfile")
    backend = _read("backend/Dockerfile")
    frontend = _read("frontend/Dockerfile")
    locked_export = "uv export --python /usr/local/bin/python --locked --no-dev --no-emit-project"

    assert workflow.count('python -m pip install "uv==0.9.21"') == 2
    assert workflow.count("uv sync --locked --extra dev") == 2
    assert "uv sync --frozen" not in workflow
    assert 'node-version: "22"' in workflow
    assert "FROM node:22-alpine AS build" in frontend
    assert "npm ci" in frontend

    for name, dockerfile in (("Dockerfile", root), ("backend/Dockerfile", backend)):
        assert "uv==0.9.21" in dockerfile, name
        assert locked_export in dockerfile, name
        assert "--require-hashes" in dockerfile, name
        assert "uv.lock" in dockerfile, name
        assert "pip install --no-cache-dir ." not in dockerfile, name
        assert "python scripts/patch_lark_oapi.py" in dockerfile, name

    assert "npm install -g @larksuite/cli@1.0.98" in root
    assert "npm install -g @larksuite/cli &&" not in root
    assert "@larksuite/cli" not in backend
    assert "setup_20.x" in root
