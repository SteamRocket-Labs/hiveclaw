from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.tenant import Tenant
from app.models.user import User


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_notification_heartbeat_0713.py"


def _run_alembic(database_url: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    backend_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=backend_root,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_heartbeat_notification_migration_is_single_head_and_matches_orm() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "runtime_notification_heartbeat_0713"' in source
    assert 'down_revision = "runtime_task_claim_lanes_0713"' in source
    assert "source_kind = 'heartbeat'" in source
    assert "DISABLE ROW LEVEL SECURITY" in source
    assert "_restore_rls_state" in source

    check_text = " ".join(
        str(constraint.sqltext)
        for constraint in RuntimeNotificationOutbox.__table__.constraints
        if getattr(constraint, "name", None) == "ck_runtime_notification_outbox_source_kind"
    )
    assert "heartbeat" in check_text

    heads = _run_alembic("postgresql://unused/unused", "heads")
    assert heads.returncode == 0, heads.stderr
    assert heads.stdout.strip() == "runtime_notification_source_kind_0713 (head)"


async def test_heartbeat_notification_migration_real_pg_roundtrip_preserves_rls(
    chain_migrated_pg_url: str,
) -> None:
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, agent_id, session_id, outbox_id = (uuid4() for _ in range(5))
    try:
        async with session_factory() as db, enter_rls_bypass(db, reason="seed heartbeat migration outbox") as bypass:
            bypass.add(Tenant(id=tenant_id, name="Heartbeat migration", slug=f"heartbeat-mig-{tenant_id.hex[:10]}"))
            bypass.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"heartbeat-mig-{user_id.hex[:8]}",
                    email=f"heartbeat-mig-{user_id.hex[:8]}@example.test",
                    password_hash="x",
                    display_name="Heartbeat Migration Owner",
                )
            )
            await bypass.flush()
            bypass.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    creator_id=user_id,
                    owner_user_id=user_id,
                    name="Heartbeat Migration Agent",
                )
            )
            await bypass.flush()
            bypass.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
            await bypass.flush()
            bypass.add(
                RuntimeNotificationOutbox(
                    id=outbox_id,
                    tenant_id=tenant_id,
                    source_kind="heartbeat",
                    source_run_id=str(uuid4()),
                    parent_session_id=session_id,
                    parent_agent_id=agent_id,
                    parent_user_id=user_id,
                    terminal_status="needs_reconciliation",
                    task_type="heartbeat",
                    summary="Heartbeat outcome requires reconciliation.",
                )
            )
            await bypass.commit()

        async with engine.connect() as connection:
            original_rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
            checks = await connection.run_sync(
                lambda sync_connection: {
                    item["name"]: item["sqltext"]
                    for item in inspect(sync_connection).get_check_constraints("runtime_notification_outbox")
                }
            )
        original_rls_state = (bool(original_rls.relrowsecurity), bool(original_rls.relforcerowsecurity))
        assert "heartbeat" in checks["ck_runtime_notification_outbox_source_kind"]

        downgrade = await asyncio.to_thread(
            _run_alembic,
            chain_migrated_pg_url,
            "downgrade",
            "runtime_task_claim_lanes_0713",
        )
        assert downgrade.returncode == 0, downgrade.stdout[-2000:] + downgrade.stderr[-2000:]
        async with engine.connect() as connection:
            remaining = await connection.scalar(
                text("SELECT count(*) FROM runtime_notification_outbox WHERE id = :id"),
                {"id": outbox_id},
            )
            previous_constraint = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'runtime_notification_outbox'::regclass "
                    "AND conname = 'ck_runtime_notification_outbox_source_kind'"
                )
            )
            restored_rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        assert remaining == 0
        assert "heartbeat" not in str(previous_constraint)
        assert "system_plan_run" in str(previous_constraint)
        assert (bool(restored_rls.relrowsecurity), bool(restored_rls.relforcerowsecurity)) == original_rls_state

        upgrade = await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stdout[-2000:] + upgrade.stderr[-2000:]
        async with engine.connect() as connection:
            upgraded_constraint = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'runtime_notification_outbox'::regclass "
                    "AND conname = 'ck_runtime_notification_outbox_source_kind'"
                )
            )
            upgraded_rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        assert "heartbeat" in str(upgraded_constraint)
        assert (bool(upgraded_rls.relrowsecurity), bool(upgraded_rls.relforcerowsecurity)) == original_rls_state
    finally:
        await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
        await engine.dispose()
