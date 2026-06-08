"""E (real PG): a plan-born trigger fires for a confirmed plan -> the agent's
trigger wake context carries the approved plan body as marching orders.

Exercises ``_load_confirmed_plan_for_trigger`` (fail-closed gates) and
``_build_confirmed_plan_context`` (dedup + injection) against real rows, so the
plan -> scheduled-execution link is proven through the actual DB path the
trigger daemon uses — not a stub.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import tenant_scoped_session
from app.services import trigger_daemon

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="plan-ctx", slug=f"pc-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
async def agent_id(owner_sessionmaker, tenant_id) -> uuid.UUID:
    from app.models.agent import Agent
    from app.models.user import User

    aid, uid = uuid.uuid4(), uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=uid,
                username=f"u-{uid.hex[:10]}",
                email=f"{uid.hex[:10]}@test.local",
                password_hash="x",
                display_name="Plan Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(Agent(id=aid, tenant_id=tenant_id, name="plan-agent", role_description="s", creator_id=uid))
    return aid


def _trigger(plan_id: uuid.UUID | str | None = None, name: str = "t"):
    from app.models.trigger import AgentTrigger

    config = {"plan_id": str(plan_id)} if plan_id else {}
    return AgentTrigger(name=name, config=config)


async def _add_plan(
    session,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    plan_markdown: str = "",
    objective: str = "",
    original_request: str = "orig",
    status: str = "confirmed",
) -> uuid.UUID:
    from app.models.plan_request import AgentPlanRequest

    pid = uuid.uuid4()
    session.add(
        AgentPlanRequest(
            id=pid,
            tenant_id=tenant_id,
            agent_id=agent_id,
            source="web_chat",
            intent_type="long_task",
            original_request=original_request,
            status=status,
            plan_version=2,
            plan_json={"schema": "hive_plan.v1", "plan_markdown": plan_markdown, "objective": objective},
        )
    )
    await session.flush()
    return pid


# --- _load_confirmed_plan_for_trigger: fail-closed gates -------------------


async def test_confirmed_plan_loads_for_trigger(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pid = await _add_plan(session, agent_id=agent_id, tenant_id=tenant_id, plan_markdown="## Do it")
        plan = await trigger_daemon._load_confirmed_plan_for_trigger(session, _trigger(pid), agent_id)
        assert plan is not None
        assert plan.id == pid


async def test_no_plan_id_returns_none(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        assert await trigger_daemon._load_confirmed_plan_for_trigger(session, _trigger(None), agent_id) is None


async def test_unconfirmed_plan_fails_closed(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pid = await _add_plan(session, agent_id=agent_id, tenant_id=tenant_id, status="awaiting_confirmation")
        assert await trigger_daemon._load_confirmed_plan_for_trigger(session, _trigger(pid), agent_id) is None


async def test_agent_mismatch_fails_closed(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pid = await _add_plan(session, agent_id=agent_id, tenant_id=tenant_id)
        other_agent = uuid.uuid4()
        assert await trigger_daemon._load_confirmed_plan_for_trigger(session, _trigger(pid), other_agent) is None


async def test_missing_plan_fails_closed(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        assert await trigger_daemon._load_confirmed_plan_for_trigger(session, _trigger(uuid.uuid4()), agent_id) is None


# --- _build_confirmed_plan_context: injection + dedup ----------------------


async def test_build_context_injects_plan_markdown(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pid = await _add_plan(
            session,
            agent_id=agent_id,
            tenant_id=tenant_id,
            plan_markdown="## Weekly digest\nGather the AI-infra funding rounds.",
        )
        ctx = await trigger_daemon._build_confirmed_plan_context(session, [_trigger(pid)], agent_id)
        assert "Gather the AI-infra funding rounds." in ctx  # authored body is the substance
        assert "已确认的计划" in ctx
        assert "定时唤醒" in ctx  # trigger-source delivery clause


async def test_build_context_dedups_same_plan_across_triggers(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pid = await _add_plan(session, agent_id=agent_id, tenant_id=tenant_id, plan_markdown="## Unique body marker")
        ctx = await trigger_daemon._build_confirmed_plan_context(
            session, [_trigger(pid, name="a"), _trigger(pid, name="b")], agent_id
        )
        # Two triggers fire for the SAME plan -> the body appears exactly once.
        assert ctx.count("Unique body marker") == 1


async def test_build_context_empty_when_no_confirmed_plan(owner_sessionmaker, tenant_id, agent_id):
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        assert await trigger_daemon._build_confirmed_plan_context(session, [_trigger(None)], agent_id) == ""


# --- production wiring: the real fire path injects the plan into the agent ---


async def test_invoke_agent_for_triggers_injects_confirmed_plan_into_first_message(
    owner_sessionmaker, tenant_id, agent_id, monkeypatch
):
    """Drive the REAL ``_invoke_agent_for_triggers`` (not a stub): a confirmed
    plan-born trigger fire must land the approved plan body in the agent's first
    (user) message — proving the helper is wired into the production path, not
    dead code."""
    import types

    from sqlalchemy import select

    import app.services.audit_logger as audit_logger
    from app.models.audit import ChatMessage
    from app.models.trigger import AgentTrigger

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        pid = await _add_plan(
            session,
            agent_id=agent_id,
            tenant_id=tenant_id,
            plan_markdown="## Marching orders\nShip the weekly funding digest.",
        )

    # The daemon uses the trigger objects it is handed (it does not re-query them);
    # an in-memory row with the plan_id is exactly what _tick would pass.
    trig = AgentTrigger(
        agent_id=agent_id, name="weekly", type="interval", config={"plan_id": str(pid)}, focus_ref=None
    )

    # Mock only the true external boundaries; the trigger-context assembly + the
    # message commit run for real against the test DB.
    fake_model = types.SimpleNamespace(id=uuid.uuid4(), name="m")

    async def _fake_select_model(db, agent, triggers):
        return fake_model, {}, None

    async def _fake_call_llm(**kwargs):
        return "ok"

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(trigger_daemon, "async_session", owner_sessionmaker)
    monkeypatch.setattr(trigger_daemon, "select_trigger_model", _fake_select_model)
    monkeypatch.setattr(trigger_daemon, "_update_trigger_runtime_task", _noop)
    monkeypatch.setattr(audit_logger, "write_audit_log", _noop)
    monkeypatch.setattr("app.api.websocket.call_llm", _fake_call_llm)

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [trig])

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        user_messages = (
            (
                await session.execute(
                    select(ChatMessage).where(ChatMessage.agent_id == agent_id, ChatMessage.role == "user")
                )
            )
            .scalars()
            .all()
        )
    assert any("Ship the weekly funding digest." in (m.content or "") for m in user_messages)
