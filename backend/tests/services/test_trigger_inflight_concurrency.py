from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.database import tenant_scoped_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.asyncio
async def test_trigger_fire_delete_order_and_config_updates_preserve_canonical_marker(
    monkeypatch,
    owner_sessionmaker,
):
    from app.api import schedules as schedules_api
    from app.api import triggers as triggers_api
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.services import trigger_daemon
    from app.services.agent_tool_domains import triggers as trigger_tools
    from app.services.runtime_task_service import _settle_trigger_runtime_task

    tenant_id, user_id, agent_id, trigger_id, deleted_id, task_id = (uuid4() for _ in range(6))
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    owner = SimpleNamespace(id=user_id, tenant_id=tenant_id, role="member", department_id=None)
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id)
    detached_deleted = None
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Trigger race", slug=f"trigger-race-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"trigger-race-{user_id.hex[:10]}",
                email=f"trigger-race-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Trigger Race Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Trigger race agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        primary = AgentTrigger(
            id=trigger_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            name="race-primary",
            type="cron",
            config={
                "expr": "0 9 * * *",
                "trigger_class": "scheduled_job",
                "created_by": str(user_id),
                "authority_state": "owned",
                "_last_value": "canonical-value",
                "failure_count": 2,
                "last_failure_at": "2026-08-31T11:00:00+00:00",
                "last_failure": "canonical failure",
                "backoff_until": "2026-08-31T11:01:00+00:00",
            },
            reason="race primary",
            is_enabled=True,
            fire_count=0,
            cooldown_seconds=0,
        )
        detached_deleted = AgentTrigger(
            id=deleted_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            name="race-delete-first",
            type="cron",
            config={
                "expr": "0 10 * * *",
                "trigger_class": "scheduled_job",
                "created_by": str(user_id),
                "authority_state": "owned",
            },
            reason="delete first",
            is_enabled=True,
            fire_count=0,
            cooldown_seconds=0,
        )
        db.add_all([primary, detached_deleted])
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="running",
                parent_agent_id=agent_id,
                metadata_json={
                    "trigger_ids": [str(trigger_id)],
                    "trigger_names": ["race-primary"],
                    "trigger_types": ["cron"],
                },
            )
        )

    async def allow_access(*_args, **_kwargs):
        return agent, "manage"

    async def allow_authority(*_args, **_kwargs):
        return SimpleNamespace(authority_source="resource_owner", operator_view=False)

    async def resolve_tenant(*_args, **_kwargs):
        return tenant_id

    def scoped(tenant, **kwargs):
        return tenant_scoped_session(
            tenant,
            session_factory=owner_sessionmaker,
            require_tenant=kwargs.get("require_tenant", False),
            source=kwargs.get("source", "test_trigger_inflight_concurrency"),
        )

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(triggers_api, "check_agent_access", allow_access)
    monkeypatch.setattr(triggers_api, "authorize_trigger_action", allow_authority)
    monkeypatch.setattr(schedules_api, "require_agent_manage_access", allow_access)
    monkeypatch.setattr(schedules_api, "_authorize_schedule", allow_authority)
    monkeypatch.setattr(trigger_tools, "authorize_trigger_action", allow_authority)
    monkeypatch.setattr(trigger_tools, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(trigger_tools, "tenant_scoped_session", scoped)
    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", scoped)

    from app.services import audit_logger

    monkeypatch.setattr(audit_logger, "write_audit_log", no_audit)

    # Delete wins: the later atomic marker sees an incomplete batch and writes nothing.
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await triggers_api.delete_trigger(
            agent_id=agent_id,
            trigger_id=deleted_id,
            operator_view=False,
            operator_reason=None,
            current_user=owner,
            db=db,
        )
    assert detached_deleted is not None
    assert (
        await trigger_daemon._mark_trigger_fire_started(
            agent_id,
            [detached_deleted],
            now=now,
            runtime_task_id=uuid4(),
            event_keys={deleted_id: "delete-first"},
        )
        is False
    )

    # Disable wins before admission: scheduled/claimed work must not start.
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        primary = await db.get(AgentTrigger, trigger_id)
        assert primary is not None
        primary.is_enabled = False
        await db.commit()
    assert not await trigger_daemon._mark_trigger_fire_started(
        agent_id,
        [primary],
        now=now,
        runtime_task_id=task_id,
        event_keys={trigger_id: "disabled-first"},
    )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        primary = await db.get(AgentTrigger, trigger_id)
        assert primary is not None
        primary.is_enabled = True
        await db.commit()

    # Mark wins: both delete surfaces fail with a typed, recoverable conflict.
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        primary = await db.get(AgentTrigger, trigger_id)
    assert primary is not None
    assert await trigger_daemon._mark_trigger_fire_started(
        agent_id,
        [primary],
        now=now,
        runtime_task_id=task_id,
        event_keys={trigger_id: "mark-first"},
    )
    expected_marker = {
        "event_key": "mark-first",
        "runtime_task_id": str(task_id),
        "started_at": now.isoformat(),
    }

    for delete_call in (
        lambda db: triggers_api.delete_trigger(
            agent_id=agent_id,
            trigger_id=trigger_id,
            operator_view=False,
            operator_reason=None,
            current_user=owner,
            db=db,
        ),
        lambda db: schedules_api.delete_schedule(
            agent_id=agent_id,
            schedule_id=trigger_id,
            operator_view=False,
            operator_reason=None,
            current_user=owner,
            db=db,
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
                await delete_call(db)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == {
            "ok": False,
            "status": "trigger_fire_inflight",
            "runtime_task_id": str(task_id),
        }

    forged = {
        "expr": "0 11 * * *",
        "trigger_class": "scheduled_job",
        "_fire_inflight": {"runtime_task_id": "forged"},
        "_last_value": "forged",
        "_new_runtime_key": "forged",
        "failure_count": 999,
        "last_failure_at": "forged",
        "last_failure": "forged",
        "backoff_until": "forged",
    }
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await triggers_api.update_trigger(
            agent_id=agent_id,
            trigger_id=trigger_id,
            body=triggers_api.TriggerUpdate(config=forged),
            operator_view=False,
            operator_reason=None,
            current_user=owner,
            db=db,
        )
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        await schedules_api.update_schedule(
            agent_id=agent_id,
            schedule_id=trigger_id,
            data=schedules_api.ScheduleUpdate(name="race-schedule-updated"),
            operator_view=False,
            operator_reason=None,
            current_user=owner,
            db=db,
        )
    tool_result = await trigger_tools._handle_update_trigger(
        agent_id,
        {"name": "race-schedule-updated", "config": forged},
        user_id=user_id,
    )
    assert "updated" in tool_result

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        persisted = await db.get(AgentTrigger, trigger_id)
        assert persisted is not None
        assert persisted.config["_fire_inflight"] == expected_marker
        assert persisted.config["_last_value"] == "canonical-value"
        assert "_new_runtime_key" not in persisted.config
        assert persisted.config["failure_count"] == 2
        assert persisted.config["last_failure_at"] == "2026-08-31T11:00:00+00:00"
        assert persisted.config["last_failure"] == "canonical failure"
        assert persisted.config["backoff_until"] == "2026-08-31T11:01:00+00:00"

        task = await db.get(RuntimeTask, task_id, with_for_update=True)
        receipt = await _settle_trigger_runtime_task(db, task, status="skipped")
        assert receipt is not None
        assert receipt["trigger_outcomes"] == {str(trigger_id): "release"}

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        settled = await db.get(AgentTrigger, trigger_id)
        assert settled is not None
        assert "_fire_inflight" not in settled.config
        await triggers_api.delete_trigger(
            agent_id=agent_id,
            trigger_id=trigger_id,
            operator_view=False,
            operator_reason=None,
            current_user=owner,
            db=db,
        )

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        assert await db.get(AgentTrigger, trigger_id) is None
