from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


_HERMETIC_ENV_KEYS = (
    "HOME",
    "AGENT_DATA_DIR",
    "HIVE_TEST_HERMETIC_ROOT",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "DOCKER_HOST",
    "SECRETS_MASTER_KEY",
)


def pytest_configure(config):
    """Pin every test process to a disposable filesystem authority root.

    This hook runs before test-module collection, which matters because several
    runtime modules resolve ``get_settings()`` into module-level path constants.
    """

    original_env = {key: os.environ.get(key) for key in _HERMETIC_ENV_KEYS}
    original_home = Path.home()
    if not os.environ.get("DOCKER_HOST"):
        desktop_socket = original_home / ".docker" / "run" / "docker.sock"
        if desktop_socket.exists():
            os.environ["DOCKER_HOST"] = f"unix://{desktop_socket}"

    root = Path(tempfile.mkdtemp(prefix="hive-pytest-hermetic-"))
    home = root / "home"
    agent_data = root / "agents"
    for path in (
        home,
        agent_data,
        root / "xdg-cache",
        root / "xdg-config",
        root / "xdg-data",
    ):
        path.mkdir(parents=True, exist_ok=True)

    os.environ.update(
        {
            "HOME": str(home),
            "AGENT_DATA_DIR": str(agent_data),
            "HIVE_TEST_HERMETIC_ROOT": str(root),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "SECRETS_MASTER_KEY": "hive-pytest-secrets-master-key-0001",
        }
    )
    config._hive_hermetic_root = root
    config._hive_original_env = original_env

    # Defensive for third-party pytest plugins that may have imported config
    # before collection. Normal runs have no cached instance at this point.
    config_module = sys.modules.get("app.config")
    if config_module is not None:
        config_module.get_settings.cache_clear()


def pytest_unconfigure(config):
    original_env = getattr(config, "_hive_original_env", {})
    for key in _HERMETIC_ENV_KEYS:
        value = original_env.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    root = getattr(config, "_hive_hermetic_root", None)
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_global_engine_pools():
    """Drop the module-level engines' pooled connections after every test.

    pytest-asyncio gives each test its own event loop, but ``app.database``'s
    engines are process-global with pooled connections bound to whichever loop
    first used them. A later test that reaches the global engine (any code
    path not wired through an injected session factory) then explodes with
    ``RuntimeError: ... attached to a different loop`` — an order-dependent
    failure class (e.g. test_skill_distiller poisoning the subagent spawn
    path's team-contract lookup).

    ``dispose(close=False)`` on the sync facade discards the pool without
    awaiting connection close — the loop-safe disposal SQLAlchemy documents
    for multi-loop asyncio use; asyncpg terminates the dropped connections on
    garbage collection.
    """
    yield
    from app import database

    database.engine.sync_engine.dispose(close=False)
    if database.schema_engine is not database.engine:
        database.schema_engine.sync_engine.dispose(close=False)


@pytest.fixture(scope="session", autouse=True)
def _initialize_test_secrets_provider():
    from app.services.secrets_provider import init_secrets_provider

    init_secrets_provider(os.environ["SECRETS_MASTER_KEY"])


@pytest.fixture
async def workflow_principals(owner_sessionmaker, tenant_id):
    """Seed real FK-backed actors for workflow asset revision/audit tests."""

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.user import User

    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"workflow-{user_id.hex[:10]}",
                email=f"workflow-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Workflow Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="workflow-agent",
                role_description="Workflow test actor",
                creator_id=user_id,
                owner_user_id=user_id,
                status="idle",
            )
        )
    return SimpleNamespace(user_id=user_id, agent_id=agent_id)


@pytest.fixture
def durable_recovery_checkpoint(monkeypatch):
    """Give kernel behavior tests an explicit durable checkpoint authority."""

    receipt = {
        "path": "/isolated-test/recovery.json",
        "ref": "runtime_artifacts/recovery_manifests/test.json",
        "sha256": "d" * 64,
        "bytes": 10,
        "ephemeral": False,
    }
    monkeypatch.setattr(
        "app.kernel.engine._persist_recovery_manifest_checkpoint",
        lambda *_args, **_kwargs: dict(receipt),
    )
    return receipt
