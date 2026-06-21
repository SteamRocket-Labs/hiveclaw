from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeAgentSession:
    """Minimal async-session stand-in returning a scripted row sequence — lets
    the production invoker's load logic run without seeding the User→Agent→Model
    FK chain. invoke_agent itself is the real kernel boundary, doubled per test."""

    def __init__(self, rows):
        self._rows = list(rows)

    async def execute(self, _stmt):
        return _FakeResult(self._rows.pop(0) if self._rows else None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
async def _clear_completion_signals(owner_sessionmaker):
    """Isolate each test: drain scans completion signals across ALL tenants
    (correct in production — one daemon serves every tenant), so a prior test's
    leftover signal would otherwise leak into the next test's global wake-count
    and cap assertions. Start each test from a clean signal table."""
    from sqlalchemy import delete

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        await session.execute(delete(CoordinationSignal).where(CoordinationSignal.signal_type == "subagent_completed"))
    yield


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="subagent-wake", slug=f"sw-{tid.hex[:10]}"))
    return tid


async def _send_completion_signal(owner_sessionmaker, tenant_id: uuid.UUID, parent_agent_id: uuid.UUID) -> uuid.UUID:
    signal_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            CoordinationSignal(
                id=signal_id,
                tenant_id=tenant_id,
                from_agent_id="subagent:researcher",
                to_agent_id=str(parent_agent_id),
                content="background result",
                signal_type="subagent_completed",
                thread_id="trace-1",
            )
        )
    return signal_id


async def _signal_count(owner_sessionmaker, tenant_id: uuid.UUID) -> int:
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        return len((await session.execute(select(CoordinationSignal))).scalars().all())


async def test_subagent_completion_wakes_idle_parent_once(owner_sessionmaker, tenant_id):
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    signal_id = await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)
    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "parent resumed"

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )
    again = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert len(result) == 1
    assert result[0].signal_id == signal_id
    assert result[0].status == "woken"
    assert len(invoked) == 1
    assert invoked[0].parent_agent_id == parent_agent_id
    assert invoked[0].tenant_id == tenant_id
    assert "background result" in invoked[0].content
    assert again == []
    assert await _signal_count(owner_sessionmaker, tenant_id) == 0


async def test_subagent_completion_wake_enumerates_signals_under_nonowner_rls(
    owner_sessionmaker,
    app_user_sessionmaker,
    tenant_id,
):
    """Production uses the non-owner app role: the cross-tenant daemon scan must
    use an audited discovery path, then return to tenant-scoped per-signal work.
    """
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    signal_id = await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)
    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "parent resumed"

    result = await drain_subagent_completion_wakes(
        session_factory=app_user_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert [item.signal_id for item in result] == [signal_id]
    assert len(invoked) == 1
    assert invoked[0].tenant_id == tenant_id
    assert await _signal_count(owner_sessionmaker, tenant_id) == 0


async def test_subagent_completion_does_not_wake_parent_with_active_run(owner_sessionmaker, tenant_id):
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=uuid.uuid4(),
                task_type="web_chat_turn",
                status="running",
                parent_agent_id=parent_agent_id,
            )
        )
    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "should not run"

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert result == []
    assert invoked == []
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1


async def test_wake_dedups_multiple_signals_for_one_parent_in_a_tick(owner_sessionmaker, tenant_id):
    """B2 guard: a parent with N completed background children is woken ONCE
    per tick (it reads all its signals on wake), not N times — no wake storm.
    The surplus signal is left in PG for a later tick."""
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)
    await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)

    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "parent resumed"

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert len([r for r in result if r.status == "woken"]) == 1
    assert len(invoked) == 1
    # one signal consumed, the duplicate stays for the next tick
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1


async def test_wake_respects_global_budget_cap(owner_sessionmaker, tenant_id):
    """B2 guard: a single tick wakes at most ``max_wakes`` parents — the rest
    wait for the next tick, so one burst cannot fan out unboundedly."""
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parents = [uuid.uuid4() for _ in range(3)]
    for parent in parents:
        await _send_completion_signal(owner_sessionmaker, tenant_id, parent)

    invoked: list[SubagentWakeRequest] = []

    async def invoke_parent(request: SubagentWakeRequest) -> str:
        invoked.append(request)
        return "parent resumed"

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
        max_wakes=2,
    )

    assert len([r for r in result if r.status == "woken"]) == 2
    assert len(invoked) == 2
    # the third parent's signal is untouched, waiting for the next tick
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1


async def test_production_parent_wake_invoker_invokes_agent_with_wake_context(monkeypatch):
    """B2 core: the real production invoker re-invokes the parent agent with a
    dedicated subagent_wake source and the child's result in the prompt."""
    from app.services import subagent_wake_consumer as swc
    from app.services.subagent_wake_consumer import SubagentWakeRequest, build_production_parent_wake_invoker

    parent_id = uuid.uuid4()
    tid = uuid.uuid4()
    model_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=parent_id,
        tenant_id=tid,
        status="active",
        primary_model_id=model_id,
        fallback_model_id=None,
        name="Researcher",
        role_description="explores topics",
        creator_id=uuid.uuid4(),
        max_tool_rounds=40,
    )
    model = SimpleNamespace(id=model_id, tenant_id=tid)
    monkeypatch.setattr(swc, "tenant_scoped_session", lambda *a, **k: _FakeAgentSession([agent, model]))

    captured: dict = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="parent handled it")

    # invoke_agent is the real kernel boundary — doubled here per Test Double rationale.
    monkeypatch.setattr("app.runtime.invoker.invoke_agent", fake_invoke_agent)

    invoker = build_production_parent_wake_invoker()
    await invoker(
        SubagentWakeRequest(
            tenant_id=tid,
            parent_agent_id=parent_id,
            signal_id=uuid.uuid4(),
            from_agent_id="subagent:researcher",
            thread_id="trace-1",
            content="found 3 sources",
        )
    )

    request = captured["request"]
    assert request.agent_id == parent_id
    assert request.session_context.source == "subagent_wake"
    assert request.core_tools_only is True
    assert "found 3 sources" in request.messages[0]["content"]
    assert request.model is model


async def test_production_parent_wake_invoker_skips_non_runnable_agent(monkeypatch):
    from app.services import subagent_wake_consumer as swc
    from app.services.subagent_wake_consumer import SubagentWakeRequest, build_production_parent_wake_invoker

    stopped_agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="stopped",
        primary_model_id=uuid.uuid4(),
        fallback_model_id=None,
        name="Idle",
        role_description="",
        creator_id=uuid.uuid4(),
        max_tool_rounds=None,
    )
    monkeypatch.setattr(swc, "tenant_scoped_session", lambda *a, **k: _FakeAgentSession([stopped_agent]))

    async def boom_invoke_agent(request):
        raise AssertionError("a non-runnable parent must not be invoked")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", boom_invoke_agent)

    invoker = build_production_parent_wake_invoker()
    result = await invoker(
        SubagentWakeRequest(
            tenant_id=stopped_agent.tenant_id,
            parent_agent_id=stopped_agent.id,
            signal_id=uuid.uuid4(),
            from_agent_id="subagent:x",
            thread_id="t",
            content="result",
        )
    )
    assert result is None
