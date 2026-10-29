from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

import pytest
import pytest_asyncio

BACKEND_ROOT = Path(__file__).resolve().parents[1]

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


_HERMETIC_ENV_KEYS = (
    "HOME",
    "AGENT_DATA_DIR",
    "AGENT_TEMPLATE_DIR",
    "HIVE_TEST_HERMETIC_ROOT",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "DOCKER_HOST",
    "SECRETS_MASTER_KEY",
)


def pytest_addoption(parser):
    parser.addoption("--failure-receipts", type=Path, help="Append failed test reports as JSON lines.")


def pytest_configure(config):
    """Pin every test process to a disposable filesystem authority root.

    This hook runs before test-module collection, which matters because several
    runtime modules resolve ``get_settings()`` into module-level path constants.
    """

    failure_receipts = config.getoption("failure_receipts")
    if failure_receipts is not None and not failure_receipts.is_absolute():
        config.option.failure_receipts = config.invocation_params.dir / failure_receipts

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
    os.environ.setdefault("AGENT_TEMPLATE_DIR", str(BACKEND_ROOT / "agent_template"))
    config._hive_hermetic_root = root
    config._hive_original_env = original_env

    # Defensive for third-party pytest plugins that may have imported config
    # before collection. Normal runs have no cached instance at this point.
    config_module = sys.modules.get("app.config")
    if config_module is not None:
        config_module.get_settings.cache_clear()


def _archive_worker_failure_log(item, report, target):
    tmp_path = item.funcargs.get("tmp_path")
    factory = getattr(item.config, "_tmp_path_factory", None)
    basetemp = getattr(factory, "_basetemp", None)
    if not isinstance(tmp_path, Path) or not isinstance(basetemp, Path):
        return []
    files = []
    for worker_log in sorted(tmp_path.rglob("worker.log")):
        if (
            worker_log.is_symlink()
            or not worker_log.is_file()
            or not worker_log.resolve().is_relative_to(basetemp.resolve())
        ):
            continue

        body = worker_log.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        case_digest = hashlib.sha256(report.nodeid.encode()).hexdigest()
        directory = target.parent / "pytest-failure-files"
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / f"{case_digest}-{report.when}-{digest}.log.gz"
        if not archive.exists():
            archive.write_bytes(gzip.compress(body, mtime=0))
        if gzip.decompress(archive.read_bytes()) != body:
            raise OSError("Worker failure log archive failed readback verification")
        files.append(
            {
                "name": worker_log.relative_to(tmp_path).as_posix(),
                "archive": archive.relative_to(target.parent).as_posix(),
                "sha256": digest,
                "bytes": len(body),
            }
        )
    return files


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    target = item.config.getoption("failure_receipts")
    if target is None:
        return

    terminal = item.config.pluginmanager.get_plugin("terminalreporter")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        timing = {key: getattr(report, key) for key in ("nodeid", "when", "outcome", "duration", "start", "stop")}
        with target.with_name("pytest-timings.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(timing) + "\n")
    except OSError as exc:
        if terminal is not None:
            terminal.write_line(f"Could not persist test timing: {exc}", yellow=True)
    if not report.failed:
        return

    if terminal is not None:
        terminal.write_sep("=", f"Failure: {report.nodeid} ({report.when})")
        terminal.write_line(report.longreprtext)
        terminal.flush()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "nodeid": report.nodeid,
            "when": report.when,
            "longrepr": report.longreprtext,
            "sections": [{"name": name, "content": content} for name, content in report.sections],
            "files": [],
        }
        try:
            receipt["files"] = _archive_worker_failure_log(item, report, target)
        except OSError as exc:
            receipt["file_capture_error"] = f"{type(exc).__name__}: {exc}"
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        if terminal is not None:
            terminal.write_line(f"Could not persist failure receipt: {exc}", yellow=True)


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


@pytest_asyncio.fixture(autouse=True)
async def _close_global_event_redis():
    """Release the process-global client while its function event loop is alive."""
    yield
    from app.core.events import close_redis

    await close_redis()


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
