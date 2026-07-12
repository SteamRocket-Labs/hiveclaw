from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "approval_execution_jobs_0712.py"


def _alembic_downgrade(database_url: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


async def test_approval_execution_job_bootstrap_schema_contract(migrated_pg_url: str) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            schema = await connection.run_sync(
                lambda sync_connection: {
                    "approval_columns": {
                        column["name"] for column in inspect(sync_connection).get_columns("approval_requests")
                    },
                    "approval_checks": {
                        constraint["name"]: constraint["sqltext"]
                        for constraint in inspect(sync_connection).get_check_constraints("approval_requests")
                    },
                    "runtime_checks": {
                        constraint["name"]: constraint["sqltext"]
                        for constraint in inspect(sync_connection).get_check_constraints("runtime_tasks")
                    },
                    "approval_uniques": {
                        constraint["name"]
                        for constraint in inspect(sync_connection).get_unique_constraints("approval_requests")
                    },
                }
            )
        assert "execution_task_id" in schema["approval_columns"]
        assert "queued" in schema["approval_checks"]["ck_approval_requests_execution_status"]
        assert "approval_execution" in schema["runtime_checks"]["ck_runtime_tasks_task_type"]
        assert "uq_approval_requests_execution_task_id" in schema["approval_uniques"]
    finally:
        await engine.dispose()


def test_approval_execution_job_migration_backfills_only_safe_unconsumed_tickets() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "approval_execution_jobs_0712"' in source
    assert 'down_revision = "budget_transition_outbox_0711"' in source
    assert "execution_status = 'needs_reapproval'" in source
    assert "execution_status = 'queued'" in source
    assert "consumed_at IS NULL" in source
    assert "expires_at > now()" in source
    assert "approval-execution:" in source
    assert "ON CONFLICT (root_idempotency_key) DO NOTHING" in source


async def test_approval_execution_job_migration_really_backfills_and_expires_legacy_rows(pg_container) -> None:
    from app.models.agent import Agent
    from app.models.audit import ApprovalRequest
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"approvaljob_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    # Bootstrap current metadata, rewind exactly this migration, seed the
    # legacy parent shape, then run the real parent->head upgrade.
    _alembic_upgrade(database_url, "head")
    _alembic_downgrade(database_url, "budget_transition_outbox_0711")

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    valid_id = uuid.uuid4()
    expired_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="Approval Migration", slug=f"approval-{tenant_id.hex[:8]}"))
            db.add(
                User(
                    id=user_id,
                    username=f"approval-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@approval-migration.test",
                    password_hash="x",
                    display_name="Approval Owner",
                    tenant_id=tenant_id,
                )
            )
            await db.flush()
            db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Approval Agent", creator_id=user_id))
            await db.flush()
            for approval_id, expires_at in (
                (valid_id, datetime.now(timezone.utc) + timedelta(minutes=30)),
                (expired_id, datetime.now(timezone.utc) - timedelta(minutes=1)),
            ):
                await db.execute(
                    ApprovalRequest.__table__.insert().values(
                        id=approval_id,
                        agent_id=agent_id,
                        tenant_id=tenant_id,
                        action_type="workspace.write",
                        details={"session_id": f"session-{approval_id}"},
                        status="approved",
                        requested_by=user_id,
                        resolved_by=user_id,
                        resolved_at=datetime.now(timezone.utc),
                        decision_id=f"decision:{approval_id}",
                        tool_name="write_file",
                        normalized_arguments={"path": "workspace/report.md", "content": "approved"},
                        input_hash="a" * 64,
                        policy_snapshot={},
                        policy_snapshot_hash="b" * 64,
                        execution_envelope={"schema": "hive.approval_execution_envelope.v2"},
                        execution_envelope_hash="c" * 64,
                        expires_at=expires_at,
                        execution_status="approved",
                        execution_idempotency_key=f"approval:{approval_id}",
                    )
                )
            await db.commit()
            eligibility = (
                await db.execute(
                    text(
                        "SELECT id, status::text AS decision_status, execution_status, consumed_at, "
                        "expires_at > now() AS fresh, tenant_id IS NOT NULL AS has_tenant, "
                        "requested_by IS NOT NULL AS has_requester, resolved_by IS NOT NULL AS has_approver, "
                        "tool_name IS NOT NULL AS has_tool, normalized_arguments IS NOT NULL AS has_args, "
                        "input_hash IS NOT NULL AS has_input_hash, policy_snapshot_hash IS NOT NULL AS has_policy, "
                        "execution_envelope IS NOT NULL AS has_envelope, "
                        "execution_envelope_hash IS NOT NULL AS has_envelope_hash, decision_id IS NOT NULL AS has_decision "
                        "FROM approval_requests WHERE id = :valid_id"
                    ),
                    {"valid_id": valid_id},
                )
            ).one()
            assert eligibility.decision_status == "approved"
            assert eligibility.execution_status == "approved"
            assert eligibility.consumed_at is None
            assert all(eligibility[4:])
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            approvals = {
                row.id: row
                for row in (
                    await connection.execute(
                        text(
                            "SELECT id, execution_status, execution_task_id, execution_receipt "
                            "FROM approval_requests WHERE id IN (:valid_id, :expired_id)"
                        ),
                        {"valid_id": valid_id, "expired_id": expired_id},
                    )
                ).all()
            }
            task = (
                await connection.execute(
                    text(
                        "SELECT id, task_type, status, root_idempotency_key FROM runtime_tasks "
                        "WHERE root_idempotency_key = :key"
                    ),
                    {"key": f"approval-execution:{valid_id}"},
                )
            ).one_or_none()
        assert task is not None, approvals
        assert approvals[valid_id].execution_status == "queued"
        assert approvals[valid_id].execution_task_id == task.id
        assert approvals[valid_id].execution_receipt["backfill_state"] == "queued"
        assert task.task_type == "approval_execution"
        assert task.status == "pending"
        assert approvals[expired_id].execution_status == "needs_reapproval"
        assert approvals[expired_id].execution_task_id is None
        assert approvals[expired_id].execution_receipt["backfill_reason"] == "expired"
    finally:
        await engine.dispose()
