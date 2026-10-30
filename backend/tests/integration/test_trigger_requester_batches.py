"""Scheduled batches preserve each requester's native work and budget root."""

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from tests.integration import conftest as integration
from tests.integration.test_m1g6_role_configuration_postgres import _app
from tests.integration.test_m1g7_routine_settings_postgres import scoped_runtime_factories  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(scope="module")
def pg_container():
    # Tick notifications and global claims belong to this module's catalog.
    yield from integration.pg_container.__wrapped__()


@pytest.fixture(scope="module")
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


@pytest.mark.parametrize("requesters", ["same", "different"])
async def test_tick_keeps_native_requester_work_and_budget_batches_separate(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    scoped_runtime_factories,  # noqa: F811
    requesters,
):
    from app import database
    from app.api.plans import router as plan_router
    from app.api.triggers import router as trigger_router
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.models.agent import Agent, AgentPermission
    from app.models.chat_session import ChatSession
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionCommand, SessionTurnInput
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.models.llm import LLMModel
    from app.services import runtime_task_worker, trigger_daemon
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.trigger_instruction_sources import native_trigger_input_sources

    app, (tenant_id, owner_id, agent_id, owner_session_id, _active_id) = await _app(
        owner_sessionmaker, app_user_sessionmaker
    )
    app.include_router(plan_router, prefix="/api")
    app.include_router(trigger_router, prefix="/api")
    other_id, other_session_id, model_id = (uuid.uuid4() for _ in range(3))
    async with owner_sessionmaker() as db:
        other = User(
            id=other_id,
            tenant_id=tenant_id,
            username="schedule-" + other_id.hex,
            email=other_id.hex + "@schedule.test",
            display_name="Other requester",
            password_hash="synthetic",
            role="member",
        )
        db.add(other)
        db.add(
            LLMModel(
                id=model_id,
                tenant_id=tenant_id,
                label="Scheduled native model",
                provider="openai",
                model="synthetic-scheduled-model",
                api_key_encrypted="synthetic-no-provider-call",
                enabled=True,
            )
        )
        await db.flush()
        db.add(AgentPermission(agent_id=agent_id, tenant_id=tenant_id, scope_type="company", access_level="use"))
        db.add(
            ChatSession(
                id=other_session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=other_id,
                source_channel="web",
                title="Other requester's private work",
            )
        )
        (await db.get(Agent, agent_id)).primary_model_id = model_id
        await db.commit()
        owner = await db.get(User, owner_id)

    original_factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original_factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    scoped_runtime_factories()
    data_root = tmp_path / "agents"
    data_root.mkdir()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(data_root))
    wakeups, leases = [], set()

    async def wakeup(**payload):
        wakeups.append(payload)

    async def acquire(trigger_id, event_key):
        key = (trigger_id, event_key)
        if key in leases:
            return False
        leases.add(key)
        return True

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", wakeup)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", acquire)
    trigger_ids, expected_owners, markers = [], {}, {}
    endpoint = f"/api/agents/{agent_id}"
    actors = [
        (owner, owner_session_id),
        (owner, owner_session_id) if requesters == "same" else (other, other_session_id),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://requester-schedules.test") as client:
        for index, (actor, session_id) in enumerate(actors):
            app.dependency_overrides[get_current_user] = lambda actor=actor: actor
            marker = f"PRIVATE_SCHEDULE_{index}_{actor.id.hex}"
            draft = await client.post(
                endpoint + "/triggers",
                json={
                    "name": f"requester_job_{index}",
                    "type": "interval",
                    "is_enabled": False,
                    "config": {"minutes": 15},
                    "reason": marker,
                },
            )
            assert draft.status_code == 201, draft.text
            trigger_id = uuid.UUID(draft.json()["id"])
            review = await client.post(
                endpoint + "/plan-recommendations",
                json={
                    "original_request": "Enable this scheduled task without plan mode.",
                    "session_id": str(session_id),
                    "source": "agent_aware",
                    "action_kind": "enable_autonomous_wake",
                    "tool_name": "trigger_rest",
                },
            )
            assert review.status_code == 201, review.text
            declined = await client.post(endpoint + "/plan-recommendations/" + review.json()["id"] + "/decline")
            assert declined.status_code == 200, declined.text
            enabled = await client.patch(
                endpoint + "/triggers/" + str(trigger_id),
                json={
                    "is_enabled": True,
                    "plan_mode_decision": "declined",
                    "plan_recommendation_id": declined.json()["id"],
                },
            )
            assert enabled.status_code == 200, enabled.text
            trigger_ids.append(trigger_id)
            expected_owners[str(trigger_id)], markers[str(trigger_id)] = actor.id, marker

    async with owner_sessionmaker() as db:
        for trigger_id in trigger_ids:
            (await db.get(AgentTrigger, trigger_id)).created_at = datetime.now(timezone.utc) - timedelta(days=1)
        await db.commit()
    await trigger_daemon._tick()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        assert (
            await db.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"))
        ).one() == (False, False)
        wrappers = list(
            await db.scalars(
                select(RuntimeTask).where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type == "trigger")
            )
        )
        observed = [
            {
                "id": str(row.id),
                "status": row.status,
                "requester": str(row.root_user_id),
                "root_session_id": row.root_session_id,
                "budget_run_id": str(row.budget_run_id),
                "trigger_ids": row.metadata_json["trigger_ids"],
            }
            for row in wrappers
        ]
        (tmp_path / "scheduled-requester-batches.json").write_text(json.dumps(observed, indent=2))
        assert len(wrappers) == len({actor.id for actor, _session in actors}), observed
        assert len(wakeups) == len(wrappers)
        for wrapper in wrappers:
            assert wrapper.status == "pending", observed
            ids = wrapper.metadata_json["trigger_ids"]
            assert ids and {expected_owners[value] for value in ids} == {wrapper.root_user_id}, observed
            budget = await db.get(RuntimeBudgetRun, wrapper.budget_run_id)
            assert budget.root_user_id == wrapper.root_user_id and budget.root_runtime_task_id == wrapper.id
            assert budget.root_session_id == wrapper.root_session_id and wrapper.root_session_id is not None
        claims = await RuntimeTaskClaimService(
            db=db, worker_id="scheduled-requester-worker", task_types=["trigger"], lease_seconds=60
        ).claim_available(batch_size=2)
        assert {row.id for row in claims} == {row.id for row in wrappers}

    for claim in claims:
        assert await trigger_daemon.execute_claimed_trigger_runtime_task(str(claim.id))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        sessions = set()
        for wrapper in wrappers:
            current = await db.get(RuntimeTask, wrapper.id)
            assert current.status == "completed", current.metadata_json
            session = await db.get(ChatSession, uuid.UUID(current.child_session_id))
            assert session.user_id == current.root_user_id
            sessions.add(session.id)
            rows = list(await db.scalars(select(SessionTurnInput).where(SessionTurnInput.session_id == session.id)))
            assert len(rows) == 1
            row = rows[0]
            command = await db.get(SessionCommand, row.command_id)
            assert command.principal_id == current.root_user_id
            references, complete = await native_trigger_input_sources(
                db, tenant_id=tenant_id, agent_id=agent_id, row=row, command=command
            )
            assert complete and len(references) == 1
            content = json.dumps(row.content_parts_json)
            ids = current.metadata_json["trigger_ids"]
            assert all((marker in content) == (identity in ids) for identity, marker in markers.items())
        assert len(sessions) == len(wrappers)
    await trigger_daemon._tick()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant_id)
        repeated = list(
            await db.scalars(
                select(RuntimeTask).where(RuntimeTask.parent_agent_id == agent_id, RuntimeTask.task_type == "trigger")
            )
        )
        assert {row.id for row in repeated} == {row.id for row in wrappers}
