from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import uvicorn


def _configure_environment() -> None:
    root = Path(os.getenv("HIVE_ATOMIC_HARNESS_ROOT", "/tmp/hive-atomic-harness")).resolve()
    agent_root = root / "agents"
    agent_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("AGENT_DATA_DIR", str(agent_root))
    os.environ.setdefault("SECRET_KEY", "atomic-harness-secret")
    os.environ.setdefault("JWT_SECRET_KEY", "atomic-harness-jwt-secret")
    os.environ.setdefault("SECRETS_MASTER_KEY", "atomic-harness-master-secret-32bytes")
    os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
    os.environ.setdefault("HIVE_CODE_EXEC_PROVIDER", "local")


def _upgrade_schema() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    schema_url = os.getenv("SCHEMA_DATABASE_URL") or os.environ["DATABASE_URL"]
    schema_env = {
        **os.environ,
        "DATABASE_URL": schema_url,
        "SCHEMA_DATABASE_URL": schema_url,
        "RLS_APP_PASSWORD": os.getenv("RLS_APP_PASSWORD", "atomic-harness-app-rls"),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_root,
        env=schema_env,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "tests.journeys.prepare_database"],
        cwd=backend_root,
        env=schema_env,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "app.scripts.grant_rls_app_role"],
        cwd=backend_root,
        env=schema_env,
        check=True,
    )


def main() -> None:
    _configure_environment()
    _upgrade_schema()
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=int(os.getenv("HIVE_JOURNEY_BACKEND_PORT", "8008")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
