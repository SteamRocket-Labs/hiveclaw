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
