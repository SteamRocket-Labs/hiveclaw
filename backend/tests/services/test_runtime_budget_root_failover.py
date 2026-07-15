from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_interactive_budget_root_failure_keeps_direct_reply_and_removes_amplification() -> None:
    from app.services.runtime_budget_failover import (
        apply_runtime_budget_root_binding,
        unavailable_runtime_budget_root_binding,
    )
    from app.tools.registry import work_amplifying_tool_exclusion_names

    binding = unavailable_runtime_budget_root_binding(
        source="web",
        interactive=True,
        error=RuntimeError("budget database unavailable"),
    )
    metadata = apply_runtime_budget_root_binding(
        {"excluded_tool_names": ["write_file"]},
        binding,
    )

    assert binding.fail_open is True
    assert binding.fail_closed is False
    assert binding.budget_run_id is None
    assert metadata["runtime_budget"] == {
        "schema": "hive.runtime_budget_binding.v1",
        "status": "unavailable",
        "reason": "interactive_direct_response_budget_service_unavailable",
        "retryable": True,
        "interactive": True,
        "work_amplifying_tools_disabled": True,
        "error_class": "RuntimeError",
        "recovery": "retry_next_independent_turn",
    }
    assert set(work_amplifying_tool_exclusion_names()) <= set(metadata["excluded_tool_names"])
    assert "schedule_wakeup" not in metadata["excluded_tool_names"]
    assert "write_file" in metadata["excluded_tool_names"]
    assert metadata["budget_observability_degraded"] is True


def test_noninteractive_budget_root_failure_is_fail_closed() -> None:
    from app.services.runtime_budget_failover import unavailable_runtime_budget_root_binding

    binding = unavailable_runtime_budget_root_binding(
        source="goal_continuation",
        interactive=False,
        error=ConnectionError("budget service unavailable"),
    )

    assert binding.fail_open is False
    assert binding.fail_closed is True
    assert binding.payload["status"] == "unavailable"
    assert binding.payload["reason"] == "goal_continuation_budget_service_unavailable"
    assert binding.payload["interactive"] is False


def test_budget_model_notice_is_typed_and_does_not_claim_semantic_failure() -> None:
    from app.services.runtime_budget_failover import runtime_budget_model_notice

    notice = runtime_budget_model_notice(
        {
            "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1",
                "status": "unavailable",
                "reason": "interactive_direct_response_budget_service_unavailable",
                "retryable": True,
                "interactive": True,
                "work_amplifying_tools_disabled": True,
            }
        }
    )

    assert "budget_service_unavailable" in notice
    assert "reason and answer directly" in notice
    assert "not available in this turn" in notice
    assert "failed" not in notice.lower()


@pytest.mark.asyncio
async def test_budget_root_creation_exception_becomes_observable_typed_unavailable(monkeypatch) -> None:
    from app.services import web_chat_runtime as runtime
    from app.services.runtime_budget_failover_metrics import (
        render_runtime_budget_failover_prometheus,
        reset_runtime_budget_failover_metrics,
    )

    class FakeAsyncSession:
        pass

    class FailingBudgetService:
        async def resolve_policy(self, _lookup):
            raise ConnectionError("budget store offline")

    monkeypatch.setattr(runtime, "AsyncSession", FakeAsyncSession)
    monkeypatch.setattr(runtime, "RuntimeBudgetService", FailingBudgetService)
    reset_runtime_budget_failover_metrics()

    binding = await runtime._create_runtime_budget_root_run_for_chat(
        db=FakeAsyncSession(),  # type: ignore[arg-type]
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        user=SimpleNamespace(id=uuid4()),
        session=SimpleNamespace(id=uuid4()),
        run_uuid=uuid4(),
        source="web",
        profile="web_chat_turn",
        interactive=True,
    )

    assert binding.status == "unavailable"
    assert binding.payload["error_class"] == "ConnectionError"
    assert binding.fail_open is True
    assert 'decision="interactive_degraded"' in render_runtime_budget_failover_prometheus()


@pytest.mark.asyncio
async def test_start_interactive_run_persists_typed_degraded_budget_state(monkeypatch) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.services import web_chat_runtime as runtime
    from app.services.runtime_budget_failover import unavailable_runtime_budget_root_binding

    agent = SimpleNamespace(id=uuid4(), name="Budget Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), title="Session", last_message_at=None, root_session_id=None)

    class FakeDB:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.commits = 0

        def add(self, value: object) -> None:
            self.added.append(value)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            return None

    async def no_active(*_args, **_kwargs):
        return None

    async def unavailable(**_kwargs):
        return unavailable_runtime_budget_root_binding(
            source="web",
            interactive=True,
            error=RuntimeError("budget store offline"),
        )

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_find_active_run", no_active)
    monkeypatch.setattr(runtime, "_create_runtime_budget_root_run_for_chat", unavailable)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", no_op)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_op)

    db = FakeDB()
    payload = await runtime.start_web_chat_run(
        db=db,  # type: ignore[arg-type]
        agent=agent,
        user=user,
        session=session,
        content="Answer directly while runtime protection recovers.",
        budget_interactive=True,
    )

    task = next(value for value in db.added if isinstance(value, RuntimeTask))
    assert task.budget_run_id is None
    assert task.budget_admission_status == "unavailable"
    assert task.budget_snapshot_json["status"] == "unavailable"
    assert task.metadata_json["runtime_budget"]["retryable"] is True
    assert "spawn_subagent" in task.metadata_json["excluded_tool_names"]
    assert payload["runtime_budget"]["status"] == "unavailable"
    assert payload["runtime_budget"]["work_amplifying_tools_disabled"] is True
    assert db.commits == 1


@pytest.mark.asyncio
async def test_start_noninteractive_run_fails_before_task_or_worker(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.services import web_chat_runtime as runtime
    from app.services.runtime_budget_failover import unavailable_runtime_budget_root_binding

    agent = SimpleNamespace(id=uuid4(), name="Budget Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=uuid4())
    session = SimpleNamespace(id=uuid4(), title="Session", last_message_at=None, root_session_id=None)
    notified: list[dict] = []

    class FakeDB:
        def __init__(self) -> None:
            self.added: list[object] = []

        def add(self, value: object) -> None:
            self.added.append(value)

    async def no_active(*_args, **_kwargs):
        return None

    async def unavailable(**_kwargs):
        return unavailable_runtime_budget_root_binding(
            source="goal_continuation",
            interactive=False,
            error=RuntimeError("budget store offline"),
        )

    async def notify(**kwargs):
        notified.append(kwargs)

    monkeypatch.setattr(runtime, "_find_active_run", no_active)
    monkeypatch.setattr(runtime, "_create_runtime_budget_root_run_for_chat", unavailable)
    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", notify)

    db = FakeDB()
    with pytest.raises(HTTPException) as raised:
        await runtime.start_web_chat_run(
            db=db,  # type: ignore[arg-type]
            agent=agent,
            user=user,
            session=session,
            content="Continue autonomously.",
            append_user_message=False,
            runtime_task_type="goal_continuation",
            budget_interactive=False,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "code": "runtime_budget_service_unavailable",
        "status": "unavailable",
        "reason": "goal_continuation_budget_service_unavailable",
        "message": "运行保护系统暂时不可用，自动任务未启动；请稍后重试。",
        "retryable": True,
        "work_amplifying_execution_started": False,
    }
    assert db.added == []
    assert notified == []


@pytest.mark.asyncio
async def test_legacy_unbound_noninteractive_run_stops_before_model() -> None:
    from app.services import web_chat_run_orchestrator as orchestrator

    finalized: list[dict] = []
    broadcasts: list[dict] = []

    async def finalize_without_assistant(**kwargs):
        finalized.append(kwargs)
        return True

    async def broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def transition(phase, *, detail=None):
        broadcasts.append({"type": "phase", "phase": phase.value, "detail": detail})

    state = SimpleNamespace(
        run_uuid=uuid4(),
        agent=SimpleNamespace(id=uuid4()),
        session_id=str(uuid4()),
        metadata={
            "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1",
                "status": "unavailable",
                "reason": "legacy_budget_unbound",
                "retryable": True,
                "interactive": False,
                "work_amplifying_tools_disabled": True,
                "recovery": "retry_next_independent_turn",
            }
        },
        phase_emitter=SimpleNamespace(transition=transition),
        ports=SimpleNamespace(
            terminal=SimpleNamespace(finalize_without_assistant=finalize_without_assistant),
            events=SimpleNamespace(broadcast=broadcast),
        ),
    )

    stopped = await orchestrator._handle_budget_unavailable_before_model(state)

    assert stopped is True
    assert finalized[0]["status"] == "failed"
    assert finalized[0]["metadata_json"]["runtime_budget"]["reason"] == "legacy_budget_unbound"
    assert finalized[0]["metadata_json"]["terminal_reason"] == "tool_budget"
    assert any(event.get("type") == "runtime_budget_unavailable" for event in broadcasts)
    assert broadcasts[-1]["phase"] == "failed"


@pytest.mark.asyncio
async def test_unavailable_turn_clears_previous_turn_budget_id_from_reused_session_context() -> None:
    from app.services import web_chat_run_orchestrator as orchestrator

    context = SimpleNamespace(
        metadata={"budget_run_id": str(uuid4())},
        source="web",
        channel="web",
        begin_turn=lambda: None,
    )

    class Broker:
        async def get_or_create_runtime_session(self, *_args):
            return context

    state = SimpleNamespace(
        run_uuid=uuid4(),
        agent=SimpleNamespace(id=uuid4(), tenant_id=uuid4()),
        session_id=str(uuid4()),
        runtime_task=SimpleNamespace(
            id=uuid4(),
            budget_run_id=None,
            root_runtime_task_id=None,
            root_session_id=None,
            trace_id=None,
        ),
        metadata={
            "source": "web",
            "runtime_budget": {
                "schema": "hive.runtime_budget_binding.v1",
                "status": "unavailable",
                "reason": "interactive_direct_response_budget_service_unavailable",
                "interactive": True,
                "work_amplifying_tools_disabled": True,
            },
        },
        summary_turn_mode=False,
        history_messages=[],
        session=None,
        ports=SimpleNamespace(
            context=SimpleNamespace(
                broker=Broker(),
                sync_permission_metadata=lambda *_args: None,
                channel_delivery_suffix=lambda *_args, **_kwargs: "",
                clear_stale_plan_mode=lambda *_args, **_kwargs: None,
            )
        ),
    )

    await orchestrator._configure_runtime_session(state)

    assert "budget_run_id" not in context.metadata
    assert context.metadata["runtime_budget"]["status"] == "unavailable"
