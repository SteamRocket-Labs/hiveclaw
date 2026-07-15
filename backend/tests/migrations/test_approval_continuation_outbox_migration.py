from __future__ import annotations

import runpy
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "approval_continuation_outbox_0712.py"


def test_approval_continuation_migration_has_reversible_constraint_and_backfill() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "approval_continuation_outbox_0712"' in source
    assert 'down_revision = "hook_failure_modes_0712"' in source
    assert "ck_runtime_notification_outbox_source_kind" in source
    assert "'approval'" in source
    assert "approval_requests" in source
    assert "chat_sessions" in source
    assert "ON CONFLICT ON CONSTRAINT uq_runtime_notification_outbox_delivery DO NOTHING" in source


async def test_approval_continuation_constraint_is_installed_in_real_postgres(revision_parent_migrated_pg_url: str) -> None:
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            checks = await connection.run_sync(
                lambda sync_connection: {
                    constraint["name"]: constraint["sqltext"]
                    for constraint in inspect(sync_connection).get_check_constraints("runtime_notification_outbox")
                }
            )
        assert "approval" in checks["ck_runtime_notification_outbox_source_kind"]
    finally:
        await engine.dispose()


async def test_approval_continuation_backfills_terminal_legacy_job(owner_sessionmaker) -> None:
    from app.models.audit import ApprovalRequest
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_task import RuntimeTask
    from tests.services.test_approval_execution_runtime import _seed_execution_job

    namespace = runpy.run_path(str(MIGRATION))
    tenant_id, user_id, agent_id, approval_id, task_id = await _seed_execution_job(owner_sessionmaker)
    session_id = uuid4()
    async with owner_sessionmaker() as db:
        approval = await db.get(ApprovalRequest, approval_id)
        task = await db.get(RuntimeTask, task_id)
        assert approval is not None and task is not None
        approval.details = {**dict(approval.details or {}), "session_id": str(session_id)}
        approval.execution_status = "succeeded"
        approval.execution_result = "legacy approved result"
        task.status = "completed"
        task.result_summary = "legacy approved result"
        task.parent_session_id = str(session_id)
        task.completed_at = task.created_at
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                user_id=user_id,
                title="Legacy approval origin",
                source_channel="web",
                session_kind="human_chat",
                actor_type="user",
                runtime_source="web_chat",
                visibility_scope="direct_user",
                listed_surface="chat",
            )
        )
        await db.flush()
        await db.execute(
            text("DELETE FROM runtime_notification_outbox WHERE source_run_id = :task_id"),
            {"task_id": str(task_id)},
        )
        await db.execute(text(namespace["_BACKFILL_OUTBOX_SQL"]))
        await db.execute(text(namespace["_BACKFILL_RECEIPT_SQL"]))
        await db.commit()

    async with owner_sessionmaker() as db:
        row = (
            await db.execute(
                select(RuntimeNotificationOutbox).where(RuntimeNotificationOutbox.source_run_id == str(task_id))
            )
        ).scalar_one()
        approval = await db.get(ApprovalRequest, approval_id)
    assert row.source_kind == "approval"
    assert row.parent_session_id == session_id
    assert row.metadata_json["approval_id"] == str(approval_id)
    assert row.metadata_json["reconciled_from_legacy_approval_execution"] is True
    assert approval is not None
    assert approval.execution_receipt["continuation_status"] == "queued"
    assert approval.execution_receipt["continuation_outbox_id"] == str(row.id)
