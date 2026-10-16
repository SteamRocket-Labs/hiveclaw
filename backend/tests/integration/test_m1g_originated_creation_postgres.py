"""Original-work creation uses the canonical HR ledger, never a second runtime."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.hr_creation import router
from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.tools.handlers.hr import _build_blueprint_preview_payload, preview_agent_blueprint
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def origin(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Origin company", slug=f"origin-{tenant_id.hex}"))
        user = User(id=user_id, tenant_id=tenant_id, username=f"origin-{user_id.hex}",
                    email=f"{user_id.hex}@origin.test", password_hash="synthetic", display_name="Origin owner", is_active=True)
        db.add(user)
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, creator_id=user_id, owner_user_id=user_id,
                     name="My Assistant", role_description="Assist the owner", status="idle",
                     agent_type="native", agent_class="internal_tenant"))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id,
                           source_channel="web", runtime_source="web_chat", actor_type="user",
                           session_kind="human_chat", visibility_scope="direct_user", listed_surface="chat"))
        db.add(RuntimeTask(id=run_id, tenant_id=tenant_id, task_type="web_chat_turn", status="running",
                           parent_agent_id=agent_id, child_agent_id=agent_id,
                           parent_session_id=str(session_id), child_session_id=str(session_id),
                           root_session_id=str(session_id), root_user_id=user_id,
                           prompt="Create a long-term research employee."))
        await db.commit()
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    context = ToolExecutionContext(agent_id=agent_id, user_id=user_id, tenant_id=str(tenant_id),
                                   session_id=str(session_id), runtime_task_id=str(run_id),
                                   workspace=tmp_path, emit_runtime_hooks=False)
    return user, context


def _arguments():
    return {"name": "Research colleague", "role_description": "Prepare referenced research drafts",
            "primary_users": ["Owner"], "core_outputs": ["Research draft"],
            "boundaries": "Draft only; never send externally", "focus_content": "Wait for the owner to assign research",
            "permission_scope": "self", "first_task_autostart": False}


def _client(user, sessionmaker):
    async def scoped_db():
        async with sessionmaker() as db:
            await pin_rls_tenant_context(db, user.tenant_id)
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = scoped_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://origin")


async def _draft(owner_sessionmaker, context):
    from app.services.hr_creation_service import upsert_hr_creation_draft

    async with owner_sessionmaker() as db:
        draft = await upsert_hr_creation_draft(
            db, tenant_id=uuid.UUID(context.tenant_id), hr_agent_id=context.agent_id,
            session_id=uuid.UUID(context.session_id), requested_by_user_id=context.user_id,
            preview_payload=_build_blueprint_preview_payload(_arguments()),
        )
        await db.commit()
        return draft.id


async def test_native_preview_remains_in_original_work_without_hr_session(
    origin, owner_sessionmaker,
):
    user, context = origin
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert payload.get("status") == "preview", payload
    assert payload["session_id"] == context.session_id
    assert payload["hr_agent_id"] == str(context.agent_id)
    assert payload["requested_by_user_id"] == str(user.id)
    assert payload["blueprint"]["first_task_autostart"] is False
    async with owner_sessionmaker() as db:
        assert len((await db.scalars(select(ChatSession).where(ChatSession.tenant_id == user.tenant_id))).all()) == 1
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 1


async def test_origin_confirmation_queues_once_and_recovery_discovers_source(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    wakeups = []

    async def wake(**kwargs):
        wakeups.append(kwargs["runtime_task_id"])

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", wake)
    base = f"/api/agents/{context.agent_id}/hr-creation-drafts"
    async with _client(user, app_user_sessionmaker) as client:
        first = await client.post(f"{base}/{draft_id}/confirm", json={"blueprint_version": 1})
        assert first.status_code == 200, first.text
        second = await client.post(f"{base}/{draft_id}/confirm", json={"blueprint_version": 1})
        assert second.status_code == 200, second.text
        assert first.json()["provisioning_task_id"] == second.json()["provisioning_task_id"]
        assert len(wakeups) == 1
        recovered = await client.get("/api/agents/hr-creation-drafts/mine")
        assert recovered.status_code == 200, recovered.text
        assert any(row["blueprint_id"] == str(draft_id) and row["session_id"] == context.session_id
                   for row in recovered.json())
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.confirmed_by_user_id == user.id
        assert draft.provisioning_task_id == wakeups[0]
    from app.tools.handlers.hr import _claim_canonical_hr_blueprint

    claimed = await _claim_canonical_hr_blueprint(ToolExecutionRequest(
        tool_name="create_digital_employee", arguments={"blueprint_id": str(draft_id)}, context=context,
    ))
    assert claimed[0] == draft_id
    assert claimed[2] == user.id
    assert str(claimed[3]) == context.session_id


async def test_read_access_keeps_draft_visible_but_cannot_confirm(
    origin, owner_sessionmaker, app_user_sessionmaker,
):
    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, context.agent_id)
        peer_id = uuid.uuid4()
        db.add(User(id=peer_id, tenant_id=user.tenant_id, username=f"peer-{peer_id.hex}",
                    email=f"{peer_id.hex}@origin.test", password_hash="synthetic", display_name="Peer"))
        await db.flush()
        agent.owner_user_id = peer_id
        db.add(AgentPermission(agent_id=context.agent_id, tenant_id=user.tenant_id,
                               scope_type="user", scope_id=user.id, access_level="read"))
        await db.commit()
    base = f"/api/agents/{context.agent_id}/hr-creation-drafts/{draft_id}"
    async with _client(user, app_user_sessionmaker) as client:
        visible = await client.get(base)
        assert visible.status_code == 200, visible.text
        denied = await client.post(base + "/confirm", json={"blueprint_version": 1})
        assert denied.status_code == 403, denied.text
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "awaiting_confirmation"
        assert draft.provisioning_task_id is None


async def test_native_preview_cannot_borrow_another_users_session(origin, owner_sessionmaker):
    _, context = origin
    async with owner_sessionmaker() as db:
        session = await db.get(ChatSession, uuid.UUID(context.session_id))
        session.user_id = None
        await db.commit()
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert payload.get("status") == "error", payload
    async with owner_sessionmaker() as db:
        assert not (await db.scalars(select(HrCreationDraft).where(
            HrCreationDraft.tenant_id == uuid.UUID(context.tenant_id),
        ))).all()


async def test_platform_requester_uses_the_runtime_selected_company(origin, owner_sessionmaker):
    user, context = origin
    async with owner_sessionmaker() as db:
        principal = await db.get(User, user.id)
        principal.role = "platform_admin"
        principal.tenant_id = None
        await db.commit()
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert payload.get("status") == "preview", payload
    assert payload["session_id"] == context.session_id
    assert payload["requested_by_user_id"] == str(user.id)


async def test_provisioning_rechecks_source_access_before_acquiring_a_claim(origin, owner_sessionmaker, app_user_sessionmaker):
    from app.services.hr_creation_service import HrCreationConflict, confirm_hr_creation_draft_record
    from app.tools.handlers.hr import _claim_canonical_hr_blueprint

    user, context = origin
    draft_id = await _draft(owner_sessionmaker, context)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        confirm_hr_creation_draft_record(draft, confirming_user_id=user.id, blueprint_version=1)
        source = await db.get(Agent, context.agent_id)
        source.deactivated_at = datetime.now(timezone.utc)
        await db.commit()
    with pytest.raises(HrCreationConflict) as error:
        await _claim_canonical_hr_blueprint(ToolExecutionRequest(
            tool_name="create_digital_employee", arguments={"blueprint_id": str(draft_id)}, context=context,
        ))
    assert error.value.code == "source_execute_denied"
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, draft_id)
        assert draft.status == "confirmed"
        assert draft.claim_token is None
        assert draft.created_agent_id is None

    # A retired source must not hide recoverable drafts from other work.
    from dataclasses import replace
    other_agent_id, other_session_id = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=other_agent_id, tenant_id=user.tenant_id, creator_id=user.id, owner_user_id=user.id,
                     name="Other assistant", role_description="Other work", status="idle",
                     agent_type="native", agent_class="internal_tenant"))
        await db.flush()
        db.add(ChatSession(id=other_session_id, tenant_id=user.tenant_id, agent_id=other_agent_id, user_id=user.id))
        await db.commit()
    other_draft = await _draft(owner_sessionmaker, replace(context, agent_id=other_agent_id, session_id=str(other_session_id)))
    async with _client(user, app_user_sessionmaker) as client:
        mine = await client.get("/api/agents/hr-creation-drafts/mine")
        assert mine.status_code == 200, mine.text
        assert [row["blueprint_id"] for row in mine.json()] == [str(other_draft)]


@pytest.mark.parametrize("platform", [False, True])
async def test_original_work_drives_real_native_provisioning_and_reopens_the_receipt(
    origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch, platform,
):
    from app.models.llm import LLMModel
    from app.models.tenant_setting import TenantSetting
    from app.models.trigger import AgentTrigger
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning
    from app.services.agent_manager import agent_manager

    user, context = origin
    if platform:
        async with owner_sessionmaker() as db:
            principal = await db.get(User, user.id)
            principal.role = "platform_admin"
            principal.tenant_id = None
            await db.commit()
        user.role = "platform_admin"
    model_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(LLMModel(id=model_id, tenant_id=user.tenant_id, label="Approved synthetic model",
                        provider="openai", model="synthetic-no-network", api_key_encrypted="synthetic", enabled=True))
        await db.flush()
        db.add(TenantSetting(tenant_id=user.tenant_id, key="default_model_id", value={"model_id": str(model_id)}))
        await db.commit()

    async def no_refinement_model(*_args, **_kwargs):
        return None, "synthetic_transport"

    refinement_calls = []

    async def synthetic_refinement(**kwargs):
        refinement_calls.append(kwargs["name"])
        return {"role_description": kwargs["role_description"], "boundaries": kwargs["boundaries"]}

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement_model)
    monkeypatch.setattr("app.tools.handlers.hr._refine_soul_inputs", synthetic_refinement)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    preview = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", arguments=_arguments(), context=context,
    )))
    assert preview["status"] == "preview", preview
    path = f"/api/agents/{context.agent_id}/hr-creation-drafts/{preview['blueprint_id']}"
    async with _client(user, app_user_sessionmaker) as client:
        response = await client.post(path + "/confirm", json={"blueprint_version": preview["blueprint_version"]})
        assert response.status_code == 200, response.text
        confirmed = response.json()
        task_id = uuid.UUID(confirmed["provisioning_task_id"])
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, task_id)
            task.status = "running"
            task.claimed_by = "synthetic-worker"
            task.claim_version = 1
            await db.commit()
        assert await execute_claimed_hr_provisioning(task_id) == "completed"
        assert await execute_claimed_hr_provisioning(task_id) == "completed"
        completed_response = await client.get(path)
        assert completed_response.status_code == 200, completed_response.text
        completed = completed_response.json()
        mine = await client.get("/api/agents/hr-creation-drafts/mine")
        assert mine.status_code == 200 and mine.json() == [], mine.text
    assert completed["draft_status"] == "completed"
    assert completed["session_id"] == context.session_id
    assert completed["hr_agent_id"] == str(context.agent_id)
    created_id = uuid.UUID(completed["created_agent_id"])
    async with owner_sessionmaker() as db:
        employee = await db.get(Agent, created_id)
        assert employee.owner_user_id == user.id
        assert employee.agent_type == "native"
        assert employee.primary_model_id == model_id
        assert (agent_manager._agent_dir(created_id) / "soul.md").is_file()
        boot = (await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == created_id,
                                                           AgentTrigger.name == "first_task_boot"))).all()
        assert boot == []
        assert len((await db.scalars(select(Agent).where(Agent.tenant_id == user.tenant_id))).all()) == 2
    assert refinement_calls == ["Research colleague"]
    if destination := os.environ.get("HIVE_TEST_WIRE_DIR"):
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / ("originated-creation-platform.json" if platform else "originated-creation.json")).write_text(json.dumps({
            "scope": {"user_id": str(user.id), "tenant_id": str(user.tenant_id)},
            "preview": preview, "confirmed": confirmed, "completed": completed,
        }, indent=2))


def test_preview_is_discoverable_by_native_employees_but_creation_stays_worker_owned():
    from types import SimpleNamespace
    from app.services.agent_tools import _core_tools_for_agent

    agent = SimpleNamespace(agent_class="internal_tenant", name="My Assistant", agent_type="native")
    names = {tool["function"]["name"] for tool in _core_tools_for_agent(agent)}
    assert "preview_agent_blueprint" in names
    assert "create_digital_employee" not in names
