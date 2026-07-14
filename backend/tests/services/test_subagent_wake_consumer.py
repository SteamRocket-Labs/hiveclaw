from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask
from app.services.runtime_task_authority import RuntimeTaskRequesterUnavailable

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


async def _send_completion_signal(
    owner_sessionmaker,
    tenant_id: uuid.UUID,
    parent_agent_id: uuid.UUID,
    *,
    metadata: dict | None = None,
) -> uuid.UUID:
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
                metadata_json=dict(metadata or {}),
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


async def test_subagent_completion_keeps_signal_when_outbox_enqueue_fails(owner_sessionmaker, tenant_id):
    from app.services.subagent_wake_consumer import drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    await _send_completion_signal(owner_sessionmaker, tenant_id, parent_agent_id)

    async def failing_enqueue(_request):
        raise RuntimeError("outbox unavailable")

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=failing_enqueue,
    )

    assert result[0].status == "failed"
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1


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


async def test_subagent_completion_wake_budget_denial_consumes_signal_without_parent_wake(
    monkeypatch,
    owner_sessionmaker,
    tenant_id,
):
    from app.services import subagent_wake_consumer as swc
    from app.services.runtime_budget_service import RuntimeBudgetDenied
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    budget_run_id = uuid.uuid4()
    signal_id = await _send_completion_signal(
        owner_sessionmaker,
        tenant_id,
        parent_agent_id,
        metadata={"budget_run_id": str(budget_run_id)},
    )
    captured: dict = {}

    class DenyingBudgetService:
        def __init__(self, **_kwargs):
            # Accepts the production session_factory DI keyword.
            pass

        async def evaluate_wake_breaker(self, **_kwargs):
            # No breaker opinion: the denial under test comes from reserve().
            return None

        async def reserve(self, reservation):
            captured["reservation"] = reservation
            raise RuntimeBudgetDenied("budget exhausted", budget_run_id=reservation.budget_run_id)

        async def settle(self, _settlement):
            raise AssertionError("denied wake must not settle a reservation")

    async def invoke_parent(_request: SubagentWakeRequest) -> str:
        raise AssertionError("budget-denied wake must not invoke parent")

    monkeypatch.setattr(swc, "RuntimeBudgetService", DenyingBudgetService)

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert len(result) == 1
    assert result[0].signal_id == signal_id
    assert result[0].status == "denied"
    assert captured["reservation"].budget_run_id == budget_run_id
    assert captured["reservation"].continuation_wakes == 1
    assert await _signal_count(owner_sessionmaker, tenant_id) == 0


async def test_subagent_completion_wake_budget_approval_wait_keeps_signal_for_resume(
    monkeypatch,
    owner_sessionmaker,
    tenant_id,
):
    from app.services import subagent_wake_consumer as swc
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    budget_run_id = uuid.uuid4()
    signal_id = await _send_completion_signal(
        owner_sessionmaker,
        tenant_id,
        parent_agent_id,
        metadata={"budget_run_id": str(budget_run_id)},
    )

    class WaitingBudgetService:
        def __init__(self, **_kwargs):
            pass

        async def evaluate_wake_breaker(self, **_kwargs):
            return None

        async def reserve(self, reservation):
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["continuation_wakes"],
            )

    async def invoke_parent(_request: SubagentWakeRequest) -> str:
        raise AssertionError("approval-waiting wake must not invoke parent")

    monkeypatch.setattr(swc, "RuntimeBudgetService", WaitingBudgetService)

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    assert len(result) == 1
    assert result[0].signal_id == signal_id
    assert result[0].status == "waiting_budget_approval"
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1


async def test_subagent_completion_wake_trips_child_reconciliation_breaker(owner_sessionmaker, tenant_id):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="trigger_fire",
            root_run_key=f"trigger:{uuid.uuid4()}",
            source="scheduled",
            profile="scheduled",
            max_subagents=10,
            max_background_tasks=10,
            max_continuation_wakes=10,
            # §10: breaker thresholds are policy-driven (None = unlimited), so the
            # reconciliation trip under test must be an explicit run threshold.
            max_needs_reconciliation=3,
        )
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        for _ in range(3):
            session.add(
                RuntimeTask(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="needs_reconciliation",
                    parent_agent_id=parent_agent_id,
                    budget_run_id=budget_run.id,
                )
            )
    signal_id = await _send_completion_signal(
        owner_sessionmaker,
        tenant_id,
        parent_agent_id,
        metadata={"budget_run_id": str(budget_run.id)},
    )

    async def invoke_parent(_request: SubagentWakeRequest) -> str:
        raise AssertionError("breaker must stop parent wake")

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        stored_run = (
            await session.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_run.id))
        ).scalar_one()

    assert len(result) == 1
    assert result[0].signal_id == signal_id
    assert result[0].status == "breaker"
    # §10 policy-driven breaker reason format: <event>:<dimension>:<observed>>=<threshold>
    assert "runtime_budget_circuit_break:needs_reconciliation:3>=3" == result[0].detail
    assert stored_run.status == "hard_stopped"
    assert stored_run.terminal_reason == "runtime_budget_circuit_break:needs_reconciliation:3>=3"
    assert await _signal_count(owner_sessionmaker, tenant_id) == 0


async def test_confirmation_breaker_freezes_parent_wake_signal_instead_of_consuming(
    owner_sessionmaker,
    tenant_id,
):
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.services.runtime_budget_service import RuntimeBudgetRunCreate, RuntimeBudgetService
    from app.services.subagent_wake_consumer import SubagentWakeRequest, drain_subagent_completion_wakes

    parent_agent_id = uuid.uuid4()
    service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="interactive",
            root_run_key=f"interactive:{uuid.uuid4()}",
            source="web_chat",
            profile="interactive",
            fail_mode="require_confirmation",
            max_failures=1,
            max_continuation_wakes=10,
        )
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                task_type="subagent",
                status="failed",
                parent_agent_id=parent_agent_id,
                budget_run_id=budget_run.id,
            )
        )
    signal_id = await _send_completion_signal(
        owner_sessionmaker,
        tenant_id,
        parent_agent_id,
        metadata={"budget_run_id": str(budget_run.id)},
    )

    async def invoke_parent(_request: SubagentWakeRequest) -> str:
        raise AssertionError("confirmation breaker must not wake parent before approval")

    result = await drain_subagent_completion_wakes(
        session_factory=owner_sessionmaker,
        invoke_parent=invoke_parent,
    )

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        stored_run = (
            await session.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_run.id))
        ).scalar_one()
    assert result[0].signal_id == signal_id
    assert result[0].status == "waiting_budget_approval"
    assert stored_run.status == "waiting_budget_approval"
    assert await _signal_count(owner_sessionmaker, tenant_id) == 1


async def test_production_parent_wake_invoker_routes_wake_context_to_outbox(monkeypatch):
    """B2 core: the real production invoker routes the completed child result
    through parent-session task-notification continuation."""
    from app.services import subagent_wake_consumer as swc
    from app.services.subagent_wake_consumer import SubagentWakeRequest, build_production_parent_wake_invoker

    parent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    tid = uuid.uuid4()
    creator_user_id = uuid.uuid4()
    requester_user_id = uuid.uuid4()
    run_id = uuid.uuid4().hex
    agent = SimpleNamespace(
        id=parent_id,
        tenant_id=tid,
        status="active",
        primary_model_id=uuid.uuid4(),
        fallback_model_id=None,
        name="Researcher",
        role_description="explores topics",
        creator_id=creator_user_id,
        max_tool_rounds=40,
    )
    parent_session = SimpleNamespace(
        id=parent_session_id,
        agent_id=parent_id,
        tenant_id=tid,
        user_id=requester_user_id,
        parent_session_id=None,
        root_session_id=None,
        transcript_metadata_json={},
        visibility_scope="team",
        listed_surface="chat",
        session_kind="human_chat",
        runtime_source="web_chat",
    )
    user = SimpleNamespace(id=requester_user_id, tenant_id=tid)
    monkeypatch.setattr(swc, "tenant_scoped_session", lambda *a, **k: _FakeAgentSession([agent, parent_session, user]))

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": "subagent",
            "tenant_id": str(tid),
            "parent_agent_id": str(parent_id),
            "root_user_id": str(requester_user_id),
            "root_session_id": str(parent_session_id),
            "delegation_chain": [f"agent:{parent_id}", f"subagent:{run_id}"],
            "metadata": {"root_user_id": str(requester_user_id)},
        }

    monkeypatch.setattr(swc, "get_runtime_task_record", fake_get_runtime_task_record, raising=False)

    captured: dict = {}

    outbox_id = uuid.uuid4()

    async def fake_enqueue_completion_notification(db, notification):
        captured.update({"db": db, "notification": notification})
        return outbox_id

    monkeypatch.setattr(
        swc,
        "enqueue_completion_notification",
        fake_enqueue_completion_notification,
        raising=False,
    )

    invoker = build_production_parent_wake_invoker()
    result = await invoker(
        SubagentWakeRequest(
            tenant_id=tid,
            parent_agent_id=parent_id,
            signal_id=uuid.uuid4(),
            from_agent_id="subagent:researcher",
            thread_id=f"subagent:{parent_session_id}:trace-1",
            content="found 3 sources",
            metadata={
                "budget_run_id": "budget-run-1",
                "subagent_run_id": run_id,
                "parent_user_id": str(requester_user_id),
            },
        )
    )

    assert result == {"ok": True, "status": "queued", "outbox_id": str(outbox_id)}
    notification = captured["notification"]
    assert notification.source_kind == "subagent"
    assert notification.source_run_id == run_id
    assert notification.parent_agent_id == agent.id
    assert notification.parent_user_id == user.id
    assert notification.parent_session_id == parent_session.id
    assert notification.summary == "found 3 sources"
    assert notification.metadata["budget_run_id"] == "budget-run-1"


async def test_production_parent_wake_invoker_holds_creator_drift_against_runtime_requester(monkeypatch):
    from app.services import subagent_wake_consumer as swc
    from app.services.subagent_wake_consumer import SubagentWakeRequest, build_production_parent_wake_invoker

    parent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    creator_user_id = uuid.uuid4()
    requester_user_id = uuid.uuid4()
    run_id = uuid.uuid4().hex
    agent = SimpleNamespace(id=parent_id, tenant_id=tenant_id, status="active", creator_id=creator_user_id)
    drifted_session = SimpleNamespace(
        id=parent_session_id,
        agent_id=parent_id,
        tenant_id=tenant_id,
        user_id=creator_user_id,
    )
    monkeypatch.setattr(swc, "tenant_scoped_session", lambda *a, **k: _FakeAgentSession([agent, drifted_session]))

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": "subagent",
            "tenant_id": str(tenant_id),
            "parent_agent_id": str(parent_id),
            "root_user_id": str(requester_user_id),
            "root_session_id": str(parent_session_id),
            "delegation_chain": [f"agent:{parent_id}", f"subagent:{run_id}"],
            "metadata": {"root_user_id": str(requester_user_id)},
        }

    async def unexpected_enqueue(*_args, **_kwargs):  # pragma: no cover - identity drift must hold
        raise AssertionError("drifted creator identity must not receive a parent wake")

    monkeypatch.setattr(swc, "get_runtime_task_record", fake_get_runtime_task_record, raising=False)
    monkeypatch.setattr(swc, "enqueue_completion_notification", unexpected_enqueue)

    with pytest.raises(RuntimeTaskRequesterUnavailable) as exc_info:
        await build_production_parent_wake_invoker()(
            SubagentWakeRequest(
                tenant_id=tenant_id,
                parent_agent_id=parent_id,
                signal_id=uuid.uuid4(),
                from_agent_id="subagent:critic",
                thread_id=f"subagent:{parent_session_id}:trace-1",
                content="done",
                metadata={"subagent_run_id": run_id},
            )
        )

    assert exc_info.value.reason_code == "parent_session_user_mismatch"


async def test_production_parent_wake_invoker_holds_when_runtime_task_identity_is_missing(monkeypatch):
    from app.services import subagent_wake_consumer as swc
    from app.services.subagent_wake_consumer import SubagentWakeRequest, build_production_parent_wake_invoker

    parent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    requester_user_id = uuid.uuid4()
    agent = SimpleNamespace(id=parent_id, tenant_id=tenant_id, status="active", creator_id=uuid.uuid4())
    parent_session = SimpleNamespace(
        id=parent_session_id,
        agent_id=parent_id,
        tenant_id=tenant_id,
        user_id=requester_user_id,
    )
    monkeypatch.setattr(swc, "tenant_scoped_session", lambda *a, **k: _FakeAgentSession([agent, parent_session]))

    async def unexpected_runtime_lookup(*_args, **_kwargs):  # pragma: no cover - no canonical id exists
        raise AssertionError("a completion signal without a RuntimeTask id must not guess authority")

    async def unexpected_enqueue(*_args, **_kwargs):  # pragma: no cover - identity must hold
        raise AssertionError("an unbound completion signal must not wake the parent")

    monkeypatch.setattr(swc, "get_runtime_task_record", unexpected_runtime_lookup, raising=False)
    monkeypatch.setattr(swc, "enqueue_completion_notification", unexpected_enqueue)

    with pytest.raises(RuntimeTaskRequesterUnavailable) as exc_info:
        await build_production_parent_wake_invoker()(
            SubagentWakeRequest(
                tenant_id=tenant_id,
                parent_agent_id=parent_id,
                signal_id=uuid.uuid4(),
                from_agent_id="subagent:critic",
                thread_id=f"subagent:{parent_session_id}:trace-1",
                content="done",
                metadata={},
            )
        )

    assert exc_info.value.reason_code == "runtime_task_id_missing"
    assert exc_info.value.evidence["authority_source"] == "runtime_tasks.root_user_id"


async def test_production_parent_wake_invoker_never_calls_model_before_outbox(monkeypatch):
    """The daemon fallback must use the same parent-session continuation path as
    direct subagent/A2A completion, instead of bypassing ToolRuntime/web-chat
    bookkeeping with a direct invoke_agent call.
    """

    from app.services import subagent_wake_consumer as swc
    from app.services.subagent_wake_consumer import SubagentWakeRequest, build_production_parent_wake_invoker

    parent_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    tid = uuid.uuid4()
    user_id = uuid.uuid4()
    run_id = uuid.uuid4().hex
    agent = SimpleNamespace(
        id=parent_id,
        tenant_id=tid,
        status="active",
        primary_model_id=uuid.uuid4(),
        fallback_model_id=None,
        name="Researcher",
        role_description="explores topics",
        creator_id=user_id,
        max_tool_rounds=40,
    )
    parent_session = SimpleNamespace(
        id=parent_session_id,
        agent_id=parent_id,
        tenant_id=tid,
        user_id=user_id,
        parent_session_id=None,
        root_session_id=None,
        transcript_metadata_json={},
        visibility_scope="team",
        listed_surface="chat",
        session_kind="human_chat",
        runtime_source="web_chat",
    )
    user = SimpleNamespace(id=user_id, tenant_id=tid)
    monkeypatch.setattr(swc, "tenant_scoped_session", lambda *a, **k: _FakeAgentSession([agent, parent_session, user]))

    async def fake_get_runtime_task_record(task_id):
        assert task_id == run_id
        return {
            "task_id": run_id,
            "task_type": "subagent",
            "tenant_id": str(tid),
            "parent_agent_id": str(parent_id),
            "root_user_id": str(user_id),
            "root_session_id": str(parent_session_id),
            "delegation_chain": [f"agent:{parent_id}", f"subagent:{run_id}"],
            "metadata": {"root_user_id": str(user_id)},
        }

    monkeypatch.setattr(swc, "get_runtime_task_record", fake_get_runtime_task_record, raising=False)

    captured: dict = {}

    outbox_id = uuid.uuid4()

    async def fake_enqueue_completion_notification(db, notification):
        captured.update({"db": db, "notification": notification})
        return outbox_id

    monkeypatch.setattr(
        swc,
        "enqueue_completion_notification",
        fake_enqueue_completion_notification,
        raising=False,
    )

    async def boom_invoke_agent(_request):
        raise AssertionError("daemon wake must not bypass parent-session continuation")

    monkeypatch.setattr("app.runtime.invoker.invoke_agent", boom_invoke_agent)

    invoker = build_production_parent_wake_invoker()
    result = await invoker(
        SubagentWakeRequest(
            tenant_id=tid,
            parent_agent_id=parent_id,
            signal_id=uuid.uuid4(),
            from_agent_id="subagent:researcher",
            thread_id=f"subagent:{parent_session_id}:trace-1",
            content="found 3 sources",
            metadata={"subagent_run_id": run_id, "parent_user_id": str(user_id)},
        )
    )

    assert result == {"ok": True, "status": "queued", "outbox_id": str(outbox_id)}
    notification = captured["notification"]
    assert notification.source_run_id
    assert notification.parent_session_id == parent_session.id
    assert notification.delivery_mode == "parent_continuation"


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
            thread_id=f"subagent:{uuid.uuid4()}:trace-1",
            content="result",
        )
    )
    assert result is None
