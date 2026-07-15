from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "local_agent_action_gov_0712.py"


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


def test_local_agent_action_governance_migration_contract_exists() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "local_agent_action_gov_0712"' in source
    assert 'down_revision = "channel_secret_encryption_0712"' in source
    assert "approval_id" in source
    assert "delivery_attempt_count" in source
    assert "delivery_lease_expires_at" in source
    assert "local_agent.execute" in source
    assert "requires_approval" in source
    assert "LOCAL_AGENT_POLICY_SEED" in source
    assert "expires_at" in source


@pytest.mark.asyncio
async def test_local_agent_action_governance_upgrade_has_typed_columns_and_bearer_expiry(
    revision_parent_migrated_pg_url: str,
) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]: column
                    for column in inspect(sync_connection).get_columns("local_agent_channel_messages")
                }
            )
            fk_rows = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_foreign_keys("local_agent_channel_messages")
            )
            null_expiry_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM local_agent_bridge_connections WHERE status = 'active' AND expires_at IS NULL"
                )
            )
    finally:
        await engine.dispose()

    assert {"approval_id", "delivery_attempt_count", "delivery_lease_expires_at"}.issubset(columns)
    assert columns["delivery_attempt_count"]["nullable"] is False
    assert any(
        row["referred_table"] == "approval_requests" and row["constrained_columns"] == ["approval_id"]
        for row in fk_rows
    )
    assert null_expiry_count == 0


@pytest.mark.asyncio
async def test_real_upgrade_backfills_legacy_delivery_token_and_missing_policies(pg_container) -> None:
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest  # noqa: F401 - resolves message FK metadata
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_session import ChatSession  # noqa: F401 - resolves session FK metadata
    from app.models.local_agent_channel import LocalAgentChannelMessage, LocalAgentChannelSession
    from app.models.local_bridge import LocalAgentBridgeConnection
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.integration.conftest import _async_url

    database_name = f"local_action_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic(database_url, "upgrade", "head")

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    session_id = uuid.uuid4()
    message_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory.begin() as db:
            db.add(Tenant(id=tenant_id, name="Local Action", slug=f"local-action-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"local-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@local-action.test",
                    password_hash="x",
                    display_name="Local owner",
                    tenant_id=tenant_id,
                    role="member",
                )
            )
            await db.flush()
            db.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    creator_id=user_id,
                    owner_user_id=user_id,
                    sponsor_user_id=user_id,
                    name="Legacy Local Agent",
                    agent_type="local_agent",
                    status="running",
                )
            )
            await db.flush()
            db.add(
                CapabilityPolicy(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    capability="local_agent.execute",
                    allowed=False,
                    requires_approval=False,
                    conditions={"owner_override": True},
                )
            )
            db.add(
                LocalAgentBridgeConnection(
                    id=connection_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    user_id=user_id,
                    device_name="Legacy Mac",
                    client_kind="hive-connect",
                    device_fingerprint="legacy-mac",
                    token_hash=f"legacy-{connection_id}",
                    scopes=["local_agent:connect", "local_agent:receive"],
                    status="active",
                    expires_at=None,
                )
            )
            await db.flush()
            db.add(
                LocalAgentChannelSession(
                    id=session_id,
                    tenant_id=tenant_id,
                    owner_user_id=user_id,
                    source_agent_id=agent_id,
                    connection_id=connection_id,
                    source="a2a",
                    status="active",
                )
            )
            await db.flush()
            db.add(
                LocalAgentChannelMessage(
                    id=message_id,
                    tenant_id=tenant_id,
                    owner_user_id=user_id,
                    source_agent_id=agent_id,
                    session_id=session_id,
                    sender_user_id=user_id,
                    direction="hive_to_local",
                    content="legacy delivered work",
                    idempotency_key=f"legacy:{message_id}",
                    replay_key=f"local:{message_id}",
                    status="delivered",
                )
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "channel_secret_encryption_0712")
    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            expiry = await connection.scalar(
                text("SELECT expires_at FROM local_agent_bridge_connections WHERE id = :id"),
                {"id": connection_id},
            )
            delivery = (
                await connection.execute(
                    text(
                        "SELECT delivery_attempt_count, delivery_lease_expires_at "
                        "FROM local_agent_channel_messages WHERE id = :id"
                    ),
                    {"id": message_id},
                )
            ).one()
            policies = (
                await connection.execute(
                    text(
                        "SELECT capability, allowed, requires_approval, conditions "
                        "FROM capability_policies WHERE agent_id = :agent_id ORDER BY capability"
                    ),
                    {"agent_id": agent_id},
                )
            ).all()
        assert expiry is not None
        assert delivery.delivery_attempt_count == 1
        assert delivery.delivery_lease_expires_at is not None
        assert {row.capability for row in policies} == {
            "local_agent.execute",
            "local_agent.file_download",
            "local_agent.file_upload",
            "local_agent.event_stream",
            "local_agent.result_report",
        }
        execute = next(row for row in policies if row.capability == "local_agent.execute")
        assert execute.allowed is False
        assert execute.requires_approval is False
        assert execute.conditions == {"owner_override": True}
        seeded = [row for row in policies if row.capability != "local_agent.execute"]
        assert all(row.conditions["seeded_by"] == "local_agent_action_gov_0712" for row in seeded)
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "channel_secret_encryption_0712")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            remaining = (
                await connection.execute(
                    text("SELECT capability, conditions FROM capability_policies WHERE agent_id = :agent_id"),
                    {"agent_id": agent_id},
                )
            ).all()
        assert [(row.capability, row.conditions) for row in remaining] == [
            ("local_agent.execute", {"owner_override": True})
        ]
    finally:
        await engine.dispose()
