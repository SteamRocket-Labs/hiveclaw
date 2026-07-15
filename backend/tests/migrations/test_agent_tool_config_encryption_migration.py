from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "agent_tool_config_encryption_0715.py"
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "app" / "scripts" / "migrate_agent_tool_configs.py"
MASTER_KEY = "agent-tool-migration-master-key-000001"
PARENT_REVISION = "audit_evidence_immutability_0715"


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, "SECRETS_MASTER_KEY": MASTER_KEY},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


def test_agent_tool_config_migration_and_operator_command_contract_exist() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    script = SCRIPT_PATH.read_text(encoding="utf-8")

    assert 'revision = "agent_tool_config_encryption_0715"' in migration
    assert f'down_revision = "{PARENT_REVISION}"' in migration
    assert "SECRETS_MASTER_KEY" in migration
    assert "migrate_agent_tool_config_rows" in migration
    assert "plaintext" in script
    assert 'parser.add_argument("--apply"' in script
    assert 'parser.add_argument("--confirm"' in script
    assert "--apply requires --confirm" in script


@pytest.mark.asyncio
async def test_real_migration_encrypts_legacy_agent_tool_configs_and_secure_downgrade_keeps_ciphertext(
    pg_container,
    monkeypatch,
) -> None:
    from tests.integration.conftest import _async_url

    database_name = f"agent_tool_secret_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    # Exercise the real release-upgrade data path.  Fresh bootstrap first
    # stamps the current head; the secure downgrade keeps schema/data intact so
    # a legacy plaintext row can be inserted at the previous revision.
    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", PARENT_REVISION)

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tool_id = uuid.uuid4()
    assignment_id = uuid.uuid4()
    legacy_config = {
        "smithery_namespace": "legacy-namespace",
        "nested": {"githubPersonalAccessToken": "legacy-github-secret"},
        "api_key": "legacy-direct-key",
    }
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        from app.models.tenant import Tenant
        from app.models.tool import Tool
        from app.models.user import User
        from tests.migrations.conftest import insert_agent_at_schema_revision

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    name="AgentTool Migration",
                    slug=f"agent-tool-{tenant_id.hex[:8]}",
                )
            )
            session.add(
                User(
                    id=user_id,
                    username=f"agent-tool-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@agent-tool.test",
                    password_hash="x",
                    display_name="Owner",
                    tenant_id=tenant_id,
                    role="org_admin",
                )
            )
            await session.flush()
            await insert_agent_at_schema_revision(
                session,
                agent_id=agent_id,
                tenant_id=tenant_id,
                name="AgentTool Migration Agent",
                creator_id=user_id,
                status="idle",
            )
            session.add(
                Tool(
                    id=tool_id,
                    name=f"mcp_legacy_{tool_id.hex[:8]}",
                    display_name="Legacy MCP",
                    description="migration fixture",
                    type="mcp",
                    category="mcp",
                    parameters_schema={},
                    config={},
                    config_schema={},
                    enabled=True,
                    is_default=False,
                    tenant_id=tenant_id,
                )
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO agent_tools "
                    "(id, agent_id, tenant_id, tool_id, enabled, config, source) "
                    "VALUES (:id, :agent_id, :tenant_id, :tool_id, true, CAST(:config AS json), 'user_installed')"
                ),
                {
                    "id": assignment_id,
                    "agent_id": agent_id,
                    "tenant_id": tenant_id,
                    "tool_id": tool_id,
                    "config": json.dumps(legacy_config),
                },
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            raw_config = (
                await connection.execute(
                    text("SELECT config FROM agent_tools WHERE id = :id"),
                    {"id": assignment_id},
                )
            ).scalar_one()
        rendered = str(raw_config)
        assert set(raw_config) == {"__hive_agent_tool_config_v1__"}
        assert raw_config["__hive_agent_tool_config_v1__"].startswith("hive:agent-tool-config:v1:")
        assert "legacy-github-secret" not in rendered
        assert "legacy-direct-key" not in rendered
        assert "legacy-namespace" not in rendered

        from app.models.tool import AgentTool
        from app.services import secrets_provider
        from app.services.secrets_provider import FernetSecretsProvider

        monkeypatch.setattr(secrets_provider, "_provider", FernetSecretsProvider(MASTER_KEY))
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            restored = (
                (await session.execute(AgentTool.__table__.select().where(AgentTool.id == assignment_id)))
                .mappings()
                .one()["config"]
            )
        assert restored == legacy_config

        live_config = {
            "email_address": "ops@example.test",
            "auth_code": "fresh-live-write-secret",
        }
        async with session_factory.begin() as session:
            assignment = (await session.execute(select(AgentTool).where(AgentTool.id == assignment_id))).scalar_one()
            assignment.config = live_config
        async with engine.connect() as connection:
            live_raw_config = (
                await connection.execute(
                    text("SELECT config FROM agent_tools WHERE id = :id"),
                    {"id": assignment_id},
                )
            ).scalar_one()
        assert set(live_raw_config) == {"__hive_agent_tool_config_v1__"}
        assert "fresh-live-write-secret" not in str(live_raw_config)
        async with session_factory() as session:
            live_restored = (
                await session.execute(select(AgentTool.config).where(AgentTool.id == assignment_id))
            ).scalar_one()
        assert live_restored == live_config
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", PARENT_REVISION)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            after_downgrade = (
                await connection.execute(
                    text("SELECT config FROM agent_tools WHERE id = :id"),
                    {"id": assignment_id},
                )
            ).scalar_one()
        assert set(after_downgrade) == {"__hive_agent_tool_config_v1__"}
        assert "legacy-direct-key" not in str(after_downgrade)
        assert "fresh-live-write-secret" not in str(after_downgrade)
    finally:
        await engine.dispose()
