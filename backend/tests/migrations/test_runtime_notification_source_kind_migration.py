from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import enter_rls_bypass
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
from app.models.tenant import Tenant
from app.models.user import User


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_notification_source_kind_0713.py"
)


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


def test_source_kind_migration_contract_and_orm_parity() -> None:
    from app.services.runtime_notification_outbox import canonical_completion_source_kind

    assert MIGRATION_PATH.exists()
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "runtime_notification_source_kind_0713"' in source
    assert 'down_revision = "runtime_notification_heartbeat_0713"' in source
    assert "DISABLE ROW LEVEL SECURITY" in source
    assert "_restore_rls_state" in source
    assert canonical_completion_source_kind("delegation") == "a2a_delegation"
    assert canonical_completion_source_kind("a2a_delegation") == "a2a_delegation"
    assert canonical_completion_source_kind("team_member") == "agent_team"
    assert canonical_completion_source_kind("approval_execution") == "approval"
    check = next(
        constraint
        for constraint in RuntimeNotificationOutbox.__table__.constraints
        if getattr(constraint, "name", None) == "ck_runtime_notification_outbox_source_kind"
    )
    assert "'delegation'" not in str(check.sqltext)


async def test_source_kind_upgrade_merges_legacy_and_canonical_intents_without_double_delivery(
    chain_migrated_pg_url: str,
) -> None:
    downgrade = await asyncio.to_thread(
        _run_alembic,
        chain_migrated_pg_url,
        "downgrade",
        "runtime_notification_heartbeat_0713",
    )
    assert downgrade.returncode == 0, downgrade.stdout[-2000:] + downgrade.stderr[-2000:]

    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, agent_id, session_id, run_id = (uuid4() for _ in range(5))
    canonical_id, legacy_id = uuid4(), uuid4()
    original_rls_state: tuple[bool, bool] | None = None
    try:
        async with session_factory() as db, enter_rls_bypass(db, reason="seed source-kind merge") as bypass:
            bypass.add(Tenant(id=tenant_id, name="Source kind merge", slug=f"source-kind-{tenant_id.hex[:8]}"))
            bypass.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"source-kind-{user_id.hex[:8]}",
                    email=f"source-kind-{user_id.hex[:8]}@example.test",
                    password_hash="x",
                    display_name="Source Kind Owner",
                )
            )
            await bypass.flush()
            bypass.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Source Kind Agent",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                )
            )
            await bypass.flush()
            bypass.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
            await bypass.flush()
            bypass.add_all(
                [
                    RuntimeNotificationOutbox(
                        id=canonical_id,
                        tenant_id=tenant_id,
                        source_kind="a2a_delegation",
                        source_run_id=str(run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="delegation",
                        summary="canonical delivered payload",
                        payload_rank=100,
                        status="delivered",
                        delivery_receipt_json={"continuation_run_id": "one"},
                    ),
                    RuntimeNotificationOutbox(
                        id=legacy_id,
                        tenant_id=tenant_id,
                        source_kind="delegation",
                        source_run_id=str(run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="a2a_delegation",
                        summary="highest ranked authoritative payload",
                        payload_rank=900,
                        status="pending",
                    ),
                ]
            )
            await bypass.commit()

        async with engine.connect() as connection:
            rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        original_rls_state = (bool(rls.relrowsecurity), bool(rls.relforcerowsecurity))

        upgrade = await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stdout[-2000:] + upgrade.stderr[-2000:]

        async with session_factory() as db, enter_rls_bypass(db, reason="verify source-kind merge") as bypass:
            rows = list(
                (
                    await bypass.execute(
                        select(RuntimeNotificationOutbox).where(
                            RuntimeNotificationOutbox.tenant_id == tenant_id,
                            RuntimeNotificationOutbox.source_run_id == str(run_id),
                        )
                    )
                )
                .scalars()
                .all()
            )
            total = int(
                (
                    await bypass.execute(
                        select(func.count())
                        .select_from(RuntimeNotificationOutbox)
                        .where(
                            RuntimeNotificationOutbox.tenant_id == tenant_id,
                            RuntimeNotificationOutbox.source_run_id == str(run_id),
                        )
                    )
                ).scalar_one()
            )
        assert total == 1
        assert len(rows) == 1
        merged = rows[0]
        assert merged.source_kind == "a2a_delegation"
        assert merged.task_type == "delegation"
        assert merged.summary == "highest ranked authoritative payload"
        assert merged.payload_rank == 900
        assert merged.status == "delivered"
        assert merged.delivery_receipt_json == {"continuation_run_id": "one"}

        async with engine.connect() as connection:
            upgraded_rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        assert (bool(upgraded_rls.relrowsecurity), bool(upgraded_rls.relforcerowsecurity)) == original_rls_state

        downgrade_again = await asyncio.to_thread(
            _run_alembic,
            chain_migrated_pg_url,
            "downgrade",
            "runtime_notification_heartbeat_0713",
        )
        assert downgrade_again.returncode == 0, downgrade_again.stdout[-2000:] + downgrade_again.stderr[-2000:]
        async with engine.connect() as connection:
            canonical_count = await connection.scalar(
                text(
                    "SELECT count(*) FROM runtime_notification_outbox "
                    "WHERE tenant_id = :tenant_id AND source_run_id = :run_id "
                    "AND source_kind = 'a2a_delegation'"
                ),
                {"tenant_id": tenant_id, "run_id": str(run_id)},
            )
            downgraded_rls = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = 'runtime_notification_outbox'::regclass"
                    )
                )
            ).one()
        assert canonical_count == 1
        assert (bool(downgraded_rls.relrowsecurity), bool(downgraded_rls.relforcerowsecurity)) == original_rls_state
    finally:
        await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
        await engine.dispose()


async def test_source_kind_upgrade_preserves_legacy_processing_and_delivered_survivor_ids(
    chain_migrated_pg_url: str,
) -> None:
    downgrade = await asyncio.to_thread(
        _run_alembic,
        chain_migrated_pg_url,
        "downgrade",
        "runtime_notification_heartbeat_0713",
    )
    assert downgrade.returncode == 0, downgrade.stdout[-2000:] + downgrade.stderr[-2000:]

    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, agent_id, session_id = (uuid4() for _ in range(4))
    processing_run_id, delivered_run_id = uuid4(), uuid4()
    canonical_processing_id, legacy_processing_id = uuid4(), uuid4()
    canonical_delivered_id, legacy_delivered_id = uuid4(), uuid4()
    processing_locked_at = datetime.now(UTC) - timedelta(seconds=5)
    delivered_at = datetime.now(UTC) - timedelta(seconds=20)
    try:
        async with session_factory() as db, enter_rls_bypass(db, reason="seed survivor identity merge") as bypass:
            bypass.add(Tenant(id=tenant_id, name="Survivor merge", slug=f"survivor-{tenant_id.hex[:8]}"))
            bypass.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"survivor-{user_id.hex[:8]}",
                    email=f"survivor-{user_id.hex[:8]}@example.test",
                    password_hash="x",
                    display_name="Survivor Owner",
                )
            )
            await bypass.flush()
            bypass.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Survivor Agent",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                )
            )
            await bypass.flush()
            bypass.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
            await bypass.flush()
            bypass.add_all(
                [
                    RuntimeNotificationOutbox(
                        id=canonical_processing_id,
                        tenant_id=tenant_id,
                        source_kind="a2a_delegation",
                        source_run_id=str(processing_run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="delegation",
                        summary="higher ranked pending payload",
                        payload_rank=900,
                        status="pending",
                        attempt_count=11,
                    ),
                    RuntimeNotificationOutbox(
                        id=legacy_processing_id,
                        tenant_id=tenant_id,
                        source_kind="delegation",
                        source_run_id=str(processing_run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="a2a_delegation",
                        summary="legacy in-flight authority",
                        payload_rank=100,
                        status="processing",
                        attempt_count=3,
                        locked_by="legacy-processing-worker",
                        locked_at=processing_locked_at,
                    ),
                    RuntimeNotificationOutbox(
                        id=canonical_delivered_id,
                        tenant_id=tenant_id,
                        source_kind="a2a_delegation",
                        source_run_id=str(delivered_run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="delegation",
                        summary="higher ranked delivered payload",
                        payload_rank=950,
                        status="pending",
                    ),
                    RuntimeNotificationOutbox(
                        id=legacy_delivered_id,
                        tenant_id=tenant_id,
                        source_kind="delegation",
                        source_run_id=str(delivered_run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="a2a_delegation",
                        summary="legacy delivered authority",
                        payload_rank=100,
                        status="delivered",
                        attempt_count=4,
                        delivery_receipt_json={"continuation_run_id": "legacy-delivered-once"},
                        delivered_at=delivered_at,
                    ),
                ]
            )
            await bypass.commit()

        upgrade = await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stdout[-2000:] + upgrade.stderr[-2000:]

        async with session_factory() as db, enter_rls_bypass(db, reason="verify survivor identities") as bypass:
            rows = list(
                (
                    await bypass.execute(
                        select(RuntimeNotificationOutbox).where(
                            RuntimeNotificationOutbox.tenant_id == tenant_id,
                            RuntimeNotificationOutbox.source_run_id.in_(
                                (str(processing_run_id), str(delivered_run_id))
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2
        by_run_id = {row.source_run_id: row for row in rows}
        processing = by_run_id[str(processing_run_id)]
        assert processing.id == legacy_processing_id
        assert processing.source_kind == "a2a_delegation"
        assert processing.task_type == "delegation"
        assert processing.status == "processing"
        assert processing.summary == "higher ranked pending payload"
        assert processing.payload_rank == 900
        assert processing.attempt_count == 3
        assert processing.locked_by == "legacy-processing-worker"
        assert processing.locked_at == processing_locked_at

        delivered = by_run_id[str(delivered_run_id)]
        assert delivered.id == legacy_delivered_id
        assert delivered.source_kind == "a2a_delegation"
        assert delivered.task_type == "delegation"
        assert delivered.status == "delivered"
        assert delivered.summary == "higher ranked delivered payload"
        assert delivered.payload_rank == 950
        assert delivered.delivery_receipt_json == {"continuation_run_id": "legacy-delivered-once"}
        assert delivered.delivered_at == delivered_at
    finally:
        await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
        await engine.dispose()


@pytest.mark.parametrize(
    ("canonical_status", "legacy_status", "error_marker"),
    (
        ("processing", "processing", "dual_processing_completion_collision"),
        ("processing", "delivered", "delivered_processing_completion_collision"),
        ("delivered", "processing", "delivered_processing_completion_collision"),
    ),
)
async def test_source_kind_upgrade_rejects_ambiguous_delivery_authority_collision(
    chain_migrated_pg_url: str,
    canonical_status: str,
    legacy_status: str,
    error_marker: str,
) -> None:
    downgrade = await asyncio.to_thread(
        _run_alembic,
        chain_migrated_pg_url,
        "downgrade",
        "runtime_notification_heartbeat_0713",
    )
    assert downgrade.returncode == 0, downgrade.stdout[-2000:] + downgrade.stderr[-2000:]

    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id, user_id, agent_id, session_id, run_id = (uuid4() for _ in range(5))
    try:
        async with session_factory() as db, enter_rls_bypass(db, reason="seed dual processing") as bypass:
            bypass.add(Tenant(id=tenant_id, name="Dual processing", slug=f"dual-{tenant_id.hex[:8]}"))
            bypass.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=f"dual-{user_id.hex[:8]}",
                    email=f"dual-{user_id.hex[:8]}@example.test",
                    password_hash="x",
                    display_name="Dual Owner",
                )
            )
            await bypass.flush()
            bypass.add(
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name="Dual Agent",
                    creator_id=user_id,
                    sponsor_user_id=user_id,
                )
            )
            await bypass.flush()
            bypass.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
            await bypass.flush()
            for source_kind, worker_id, status in (
                ("a2a_delegation", "canonical-worker", canonical_status),
                ("delegation", "legacy-worker", legacy_status),
            ):
                bypass.add(
                    RuntimeNotificationOutbox(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        source_kind=source_kind,
                        source_run_id=str(run_id),
                        parent_session_id=session_id,
                        parent_agent_id=agent_id,
                        parent_user_id=user_id,
                        terminal_status="completed",
                        task_type="delegation",
                        summary=f"claimed by {worker_id}",
                        status=status,
                        attempt_count=1,
                        locked_by=worker_id if status == "processing" else None,
                        locked_at=datetime.now(UTC) if status == "processing" else None,
                        delivery_receipt_json=(
                            {"continuation_run_id": f"delivered-by-{worker_id}"} if status == "delivered" else None
                        ),
                        delivered_at=datetime.now(UTC) if status == "delivered" else None,
                    )
                )
            await bypass.commit()

        upgrade = await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        combined_output = upgrade.stdout + upgrade.stderr
        assert upgrade.returncode != 0
        assert error_marker in combined_output
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM runtime_notification_outbox WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant_id},
            )
            await connection.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM agents WHERE id = :id"), {"id": agent_id})
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_id})
        repair = await asyncio.to_thread(_run_alembic, chain_migrated_pg_url, "upgrade", "head")
        assert repair.returncode == 0, repair.stdout[-2000:] + repair.stderr[-2000:]
        await engine.dispose()
