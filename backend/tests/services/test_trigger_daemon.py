from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_terminal_trigger_update_forwards_completion_outbox_atomically(monkeypatch):
    import app.services.trigger_daemon as daemon
    from app.services.runtime_notification_outbox import CompletionNotification

    captured = {}

    async def fake_update_runtime_task_record(task_id, **fields):
        captured["task_id"] = task_id
        captured["fields"] = fields
        return True

    monkeypatch.setattr(daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    notification = CompletionNotification(
        tenant_id=uuid4(),
        source_kind="trigger",
        source_run_id=str(uuid4()),
        parent_session_id=uuid4(),
        parent_agent_id=uuid4(),
        parent_user_id=uuid4(),
        terminal_status="completed",
        task_type="trigger",
        summary="trigger completed",
        delivery_mode="session_projection",
    )

    await daemon._update_trigger_runtime_task(
        notification.source_run_id,
        status="completed",
        result_summary="trigger completed",
        session_id=str(notification.parent_session_id),
        completion_notification=notification,
    )

    assert captured["task_id"] == notification.source_run_id
    assert captured["fields"]["status"] == "completed"
    assert captured["fields"]["completion_notification"] is notification


@pytest.mark.asyncio
async def test_terminal_trigger_update_preserves_full_result_summary(monkeypatch):
    import app.services.trigger_daemon as daemon

    captured = {}

    async def fake_update_runtime_task_record(task_id, **fields):
        captured["task_id"] = task_id
        captured["fields"] = fields
        return True

    monkeypatch.setattr(daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    full_result = "trigger evidence\n" + ("T" * 3000) + "\nEND_OF_TRIGGER_EVIDENCE"

    await daemon._update_trigger_runtime_task(
        "trigger-run-full",
        status="completed",
        result_summary=full_result,
    )

    assert captured["fields"]["result_summary"] == full_result


@pytest.mark.asyncio
@pytest.mark.parametrize("update_outcome", [True, False, RuntimeError("terminal write failed")])
async def test_terminal_trigger_update_reports_only_a_committed_transition(monkeypatch, update_outcome):
    import app.services.trigger_daemon as daemon

    order = []

    async def fake_update_runtime_task_record(_task_id, **_fields):
        order.append("terminal_commit")
        if isinstance(update_outcome, Exception):
            raise update_outcome
        return update_outcome

    monkeypatch.setattr(daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    committed = await daemon._update_trigger_runtime_task(
        "trigger-run-terminal",
        status="completed",
        result_summary="done",
    )

    assert committed is (update_outcome is True)
    assert order == ["terminal_commit"]


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_claimed_trigger_terminal_retry_settles_budget_exactly_once(
    monkeypatch,
    owner_sessionmaker,
    app_user_sessionmaker,
):
    from sqlalchemy import select

    from app import database as database_module
    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.trigger import AgentTrigger
    from app.models.user import User
    from app.services import direct_invocation_terminal_boundary_processor as terminal_processor
    from app.services import runtime_budget_service as budget_module
    from app.services import runtime_task_service as task_module
    from app.services import trigger_daemon as daemon
    from app.services.runtime_budget_service import (
        RuntimeBudgetReservation,
        RuntimeBudgetRunCreate,
        RuntimeBudgetService,
    )
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task

    tenant_id, user_id, agent_id, trigger_id, task_id = (uuid4() for _ in range(5))
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(Tenant(id=tenant_id, name="Claimed trigger", slug=f"claimed-trigger-{tenant_id.hex[:10]}"))
        db.add(
            User(
                id=user_id,
                username=f"claimed-{user_id.hex[:10]}",
                email=f"claimed-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Claimed Trigger Owner",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Claimed trigger agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
    budget_service = RuntimeBudgetService(session_factory=owner_sessionmaker)
    budget_run = await budget_service.create_run(
        RuntimeBudgetRunCreate(
            tenant_id=tenant_id,
            root_run_kind="trigger_test",
            root_run_key=f"trigger-test:{task_id}",
            source="trigger",
            profile="scheduled",
            max_background_tasks=2,
            enforcement_mode="enforce",
        )
    )
    reservation_key = f"trigger:{task_id}:start"
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add(
            AgentTrigger(
                id=trigger_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                name="Claimed once",
                type="once",
                config={
                    "at": "2026-09-01T00:00:00+00:00",
                    "_fire_inflight": {
                        "event_key": "once:claimed",
                        "runtime_task_id": str(task_id),
                        "started_at": "2026-09-01T00:00:00+00:00",
                    },
                },
                reason="Prove claimed budget retry",
                is_enabled=True,
                fire_count=0,
                cooldown_seconds=0,
            )
        )
        db.add(
            RuntimeTask(
                id=task_id,
                tenant_id=tenant_id,
                task_type="trigger",
                status="pending",
                parent_agent_id=agent_id,
                root_user_id=user_id,
                budget_run_id=budget_run.id,
                budget_reservation_key=reservation_key,
                budget_admission_status="reserved",
                metadata_json={
                    "delivery": "workflow",
                    "trigger_ids": [str(trigger_id)],
                    "trigger_names": ["Claimed once"],
                    "trigger_types": ["once"],
                },
            )
        )
    await budget_service.reserve(
        RuntimeBudgetReservation(
            budget_run_id=budget_run.id,
            reservation_key=reservation_key,
            background_tasks=1,
            runtime_task_id=task_id,
            metadata={"work_type": "trigger"},
        )
    )

    monkeypatch.setattr(database_module, "async_session", owner_sessionmaker)
    monkeypatch.setattr(task_module, "async_session", owner_sessionmaker)
    monkeypatch.setattr(budget_module, "async_session", owner_sessionmaker)

    async def claim(worker_id: str):
        async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
            claims = await RuntimeTaskClaimService(
                db=db,
                worker_id=worker_id,
                task_types=("trigger",),
                lease_seconds=60,
            ).claim_available(batch_size=1)
        assert len(claims) == 1
        return claims[0]

    worker_a = await claim("trigger-worker-a")
    original_enqueue = terminal_processor.enqueue_direct_terminal_boundary_for_task

    async def fail_terminal_outbox(*_args, **_kwargs):
        raise RuntimeError("terminal outbox unavailable")

    monkeypatch.setattr(terminal_processor, "enqueue_direct_terminal_boundary_for_task", fail_terminal_outbox)
    committed = await run_claimed_runtime_task(
        daemon._update_trigger_runtime_task(
            str(task_id),
            status="completed",
            result_summary="Trigger completed.",
        ),
        task_id=task_id,
        claim_version=worker_a.claim_version,
        worker_id="trigger-worker-a",
        lease_seconds=30,
    )
    assert committed is False

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        stored_budget = await db.get(RuntimeBudgetRun, budget_run.id)
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            ).scalars()
        )
        assert task.status == "running"
        assert task.budget_admission_status == "reserved"
        assert stored_budget.reserved_background_tasks == 1
        assert stored_budget.used_background_tasks == 0
        assert settlements == []
        task.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    monkeypatch.setattr(terminal_processor, "enqueue_direct_terminal_boundary_for_task", original_enqueue)
    worker_b = await claim("trigger-worker-b")
    assert worker_b.claim_version == worker_a.claim_version + 1
    committed = await run_claimed_runtime_task(
        daemon._update_trigger_runtime_task(
            str(task_id),
            status="completed",
            result_summary="Trigger completed.",
        ),
        task_id=task_id,
        claim_version=worker_b.claim_version,
        worker_id="trigger-worker-b",
        lease_seconds=30,
    )
    assert committed is True

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        trigger = await db.get(AgentTrigger, trigger_id)
        stored_budget = await db.get(RuntimeBudgetRun, budget_run.id)
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            ).scalars()
        )
    assert task.status == "completed"
    assert task.claim_version == worker_b.claim_version
    assert task.metadata_json["runtime_budget_actuals"] == {"background_tasks": 1}
    assert task.budget_admission_status == "reserved"
    assert trigger.fire_count == 1
    assert stored_budget.reserved_background_tasks == 1
    assert stored_budget.used_background_tasks == 0
    assert settlements == []

    assert await budget_service.reconcile_orphaned_reservations() >= 1
    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, task_id)
        stored_budget = await db.get(RuntimeBudgetRun, budget_run.id)
        settlements = list(
            (
                await db.execute(
                    select(RuntimeBudgetEvent).where(
                        RuntimeBudgetEvent.budget_run_id == budget_run.id,
                        RuntimeBudgetEvent.event_type == "settlement",
                    )
                )
            ).scalars()
        )
    assert task.budget_admission_status == "settled"
    assert stored_budget.reserved_background_tasks == 0
    assert stored_budget.used_background_tasks == 1
    assert len(settlements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "killed", "skipped", "needs_reconciliation"])
async def test_each_committed_trigger_terminal_status_persists_budget_actuals(monkeypatch, status):
    import app.services.trigger_daemon as daemon

    captured = {}

    async def fake_update_runtime_task_record(_task_id, **fields):
        captured.update(fields)
        return True

    monkeypatch.setattr(daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    committed = await daemon._update_trigger_runtime_task(
        "trigger-run-terminal",
        status=status,
        result_summary="done",
    )

    assert committed is True
    assert captured["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}


def test_trigger_completion_targets_reflection_projection_without_parent_rerun():
    import app.services.trigger_daemon as daemon

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    runtime_task_id = uuid4()
    notification = daemon._trigger_completion_notification(
        runtime_task_id=runtime_task_id.hex,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        status="completed",
        summary="daily brief ready",
        trigger_names=["daily brief"],
        trigger_types=["cron"],
        artifacts=[{"path": "runtime_artifacts/triggers/result.json"}],
    )

    assert notification is not None
    assert notification.source_kind == "trigger"
    assert notification.source_run_id == str(runtime_task_id)
    assert notification.parent_session_id == session_id
    assert notification.delivery_mode == "session_projection"
    assert notification.metadata["trigger_names"] == ["daily brief"]


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsCollection:
    def __init__(self, values):
        self._values = list(values)

    def first(self):
        return self._values[0] if self._values else None

    def all(self):
        return list(self._values)


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _ScalarsCollection(self._values)


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _SequenceSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        # RLS GUC statements (SET LOCAL app.current_tenant_id = ...) emitted by
        # tenant_scoped_session / enter_rls_bypass before the business query must
        # not consume a result from the configured sequence.
        if "app.current_tenant_id" in str(_stmt):
            return _ScalarResult(None)
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    async def commit(self):
        self.commits += 1


def _route_scoped_session(monkeypatch, trigger_daemon, session_provider, *, tenant_id=None):
    """Route tenant_scoped_session + resolve_tenant_for_agent to the test's fake.

    Group-A RLS migration moved daemon accessors onto ``tenant_scoped_session``
    (RLS-GUC aware) and the ``resolve_tenant_for_agent`` bypass read. Tests that
    previously mocked only ``async_session`` must also mock these so the scoped
    sites use the fake session instead of the real engine.
    """
    if not callable(session_provider):
        session = session_provider
        session_provider = lambda *a, **k: session  # noqa: E731

    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: session_provider())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)

    async def _fake_admit_tenant(agent_id, *, source, **_kwargs):
        from app.runtime.tenant_admission import RuntimeTenantAdmission, blocked_runtime_tenant_admission

        if tenant_id is None:
            return blocked_runtime_tenant_admission(
                reason_code="agent_tenant_missing",
                message=f"{source} runtime is blocked because agent {agent_id} has no tenant.",
                source=source,
                agent_id=agent_id,
            )
        return RuntimeTenantAdmission(
            ok=True,
            tenant_id=tenant_id,
            status="allowed",
            reason_code="tenant_resolved",
            message=f"{source} runtime tenant resolved.",
            agent_id=agent_id,
            source=source,
        )

    monkeypatch.setattr(trigger_daemon, "admit_agent_runtime_tenant", _fake_admit_tenant)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_outcome", "terminal_reason", "mixed_workflow_hold"),
    [
        (True, "turn_stop", False),
        (False, "turn_stop", False),
        (RuntimeError("terminal write failed"), "turn_stop", False),
        (True, "provider_error", False),
        (True, "turn_stop", True),
    ],
)
async def test_trigger_terminal_learning_waits_for_committed_outbox(
    monkeypatch,
    terminal_outcome,
    terminal_reason,
    mixed_workflow_hold,
):
    import app.api.websocket as websocket_api
    import app.runtime.hooks as runtime_hooks
    import app.services.auto_dream as auto_dream
    import app.services.dream_runtime as dream_runtime
    import app.services.heartbeat as heartbeat
    import app.services.trigger_artifacts as trigger_artifacts
    import app.services.trigger_daemon as trigger_daemon
    import app.services.workflow_trigger as workflow_trigger

    tenant_id = uuid4()
    agent_id = uuid4()
    creator_id = uuid4()
    participant = SimpleNamespace(id=uuid4())
    agent = SimpleNamespace(
        id=agent_id,
        name="Terminal Order Agent",
        role_description="",
        status="running",
        creator_id=creator_id,
        tenant_id=tenant_id,
        primary_model_id=uuid4(),
        fallback_model_id=None,
    )
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily",
        type="cron",
        reason="Run daily",
        config={},
        reply_context=None,
    )
    workflow_trigger_row = SimpleNamespace(
        id=uuid4(),
        name="held workflow",
        type="cron",
        reason="Run held workflow",
        config={"workflow_ref": {"definition_name": "held-workflow"}},
        reply_context=None,
    )
    workflow_run_id = uuid4()
    workflow_session_id = uuid4()
    model = SimpleNamespace(id=uuid4(), name="model")
    order: list[str] = []
    emitted: list[object] = []
    terminal_updates: list[dict] = []

    class _InvocationSession(_SequenceSession):
        def add(self, _value):
            return None

        async def flush(self):
            return None

        async def commit(self):
            order.append("transcript_commit")
            await super().commit()

    sessions = [
        _InvocationSession([_ScalarResult(agent), _ScalarResult(participant)]),
        _InvocationSession([_ScalarResult(participant)]),
    ]

    def scoped_session(*_args, **_kwargs):
        if not sessions:
            raise AssertionError("Unexpected tenant_scoped_session call")
        return sessions.pop(0)

    async def fake_admit(*_args, **_kwargs):
        return SimpleNamespace(ok=True, tenant_id=tenant_id)

    async def fake_select_model(*_args, **_kwargs):
        return model, {}, None

    async def fake_append_session_event(**_kwargs):
        return SimpleNamespace(message_id=None)

    async def fake_call_llm(**_kwargs):
        assert _kwargs["return_result"] is True
        return SimpleNamespace(
            content="trigger result",
            terminal_reason=terminal_reason,
            response_complete_payload=(
                {
                    "agent_id": agent_id,
                    "session_id": _kwargs["session_id"],
                    "messages": _kwargs["messages"],
                    "source": "trigger",
                    "metadata": {
                        "tenant_id": str(tenant_id),
                        "final_response": "trigger result",
                    },
                }
                if terminal_reason == "turn_stop"
                else None
            ),
        )

    async def fake_update_runtime_task_record(_task_id, **fields):
        terminal_updates.append(fields)
        order.append(f"runtime:{fields.get('status')}")
        if fields.get("status") != "completed":
            return True
        if isinstance(terminal_outcome, Exception):
            raise terminal_outcome
        return terminal_outcome

    async def fake_emit_hook(event, **_kwargs):
        emitted.append(event)
        order.append(f"hook:{getattr(event, 'value', event)}")

    async def noop_async(*_args, **_kwargs):
        return None

    async def fake_fire_workflow(**_kwargs):
        if mixed_workflow_hold and _kwargs["trigger_id"] == workflow_trigger_row.id:
            return workflow_trigger.WorkflowTriggerFireResult(
                status="needs_reconciliation",
                run_id=workflow_run_id,
                run_status="completed",
                reason="workflow_asset_usage_evidence_commit_failed",
                session_id=workflow_session_id,
            )
        return None

    def fake_record_session_end(_agent_id, **_kwargs):
        order.append("dream:record")

    async def fake_enqueue_due_dream(*_args, **_kwargs):
        order.append("dream:enqueue")

    def fake_write_trigger_output_artifact(**_kwargs):
        order.append("artifact:prepared")
        return None

    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(trigger_daemon, "admit_agent_runtime_tenant", fake_admit)
    monkeypatch.setattr(trigger_daemon, "select_trigger_model", fake_select_model)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(trigger_daemon, "_recover_reply_target_from_session", noop_async)
    monkeypatch.setattr(workflow_trigger, "fire_workflow_for_trigger", fake_fire_workflow)
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", fake_append_session_event)
    monkeypatch.setattr(websocket_api, "call_llm", fake_call_llm)
    monkeypatch.setattr(auto_dream, "record_session_end", fake_record_session_end)
    monkeypatch.setattr(dream_runtime, "enqueue_due_dream", fake_enqueue_due_dream)
    monkeypatch.setattr(heartbeat, "_parse_heartbeat_outcome", lambda _reply: ("unknown", None))
    monkeypatch.setattr(trigger_artifacts, "write_trigger_output_artifact", fake_write_trigger_output_artifact)
    monkeypatch.setattr(runtime_hooks, "emit_hook", fake_emit_hook)

    await trigger_daemon._invoke_agent_for_triggers(
        agent_id,
        [workflow_trigger_row, trigger] if mixed_workflow_hold else [trigger],
        runtime_task_id=str(uuid4()),
    )

    if mixed_workflow_hold:
        terminal_update = next(update for update in terminal_updates if update.get("completion_notification"))
        notification = terminal_update["completion_notification"]
        assert terminal_update["status"] == "needs_reconciliation"
        assert terminal_update["metadata_json"]["trigger_artifact_input"]["triggers"] == [
            {
                "id": str(trigger.id),
                "name": trigger.name,
                "type": trigger.type,
                "config": {"trigger_class": ""},
            }
        ]
        assert terminal_update["metadata_json"]["trigger_artifact_input"]["final_reply"] == "trigger result"
        assert notification.terminal_status == "needs_reconciliation"
        assert notification.metadata["terminal_reason"] == "turn_stop"
        assert notification.metadata["trigger_settlement_overrides"] == {
            str(workflow_trigger_row.id): "hold",
            str(trigger.id): "success",
        }
        assert notification.metadata["workflow_trigger_results"] == [
            {
                "trigger_id": str(workflow_trigger_row.id),
                "trigger_name": workflow_trigger_row.name,
                "status": "needs_reconciliation",
                "run_id": str(workflow_run_id),
                "run_status": "completed",
                "session_id": str(workflow_session_id),
                "reason": "workflow_asset_usage_evidence_commit_failed",
            }
        ]
        assert "dream:record" not in order
    elif terminal_reason != "turn_stop":
        assert "runtime:completed" not in order
        assert "runtime:failed" in order
    elif terminal_outcome is True:
        assert "runtime:completed" in order
        assert "artifact:prepared" not in order
        assert "dream:record" not in order
        assert "dream:enqueue" not in order
    else:
        assert "artifact:prepared" not in order
        assert "dream:record" not in order
        assert "dream:enqueue" not in order
    assert runtime_hooks.HookEvent.TRIGGER_END not in emitted
    assert runtime_hooks.HookEvent.RESPONSE_COMPLETE not in emitted


def _disable_completed_focus_reconciler(monkeypatch, trigger_daemon):
    async def fake_preflight_trigger_group(_agent_id, _triggers, _now):
        return True, None, "", {}

    monkeypatch.setattr(
        trigger_daemon,
        "_preflight_trigger_group",
        fake_preflight_trigger_group,
    )


def test_trigger_invocation_uses_replayable_transcript_writer_not_direct_chat_messages() -> None:
    import ast

    source_path = Path(__file__).resolve().parents[2] / "app" / "services" / "trigger_daemon.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    invoke_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_invoke_agent_for_triggers"
    )

    direct_chat_message_calls = [
        node
        for node in ast.walk(invoke_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ChatMessage"
    ]
    append_session_event_calls = [
        node
        for node in ast.walk(invoke_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "append_session_event"
    ]

    assert direct_chat_message_calls == []
    assert append_session_event_calls


def test_trigger_daemon_terminal_status_writes_use_single_helper() -> None:
    import ast

    source_path = Path(__file__).resolve().parents[2] / "app" / "services" / "trigger_daemon.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    terminal_statuses = {"completed", "failed", "killed", "skipped", "needs_reconciliation"}
    direct_terminal_updates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "update_runtime_task_record":
            continue
        status_keyword = next((keyword for keyword in node.keywords if keyword.arg == "status"), None)
        if (
            status_keyword is not None
            and isinstance(status_keyword.value, ast.Constant)
            and status_keyword.value.value in terminal_statuses
        ):
            direct_terminal_updates.append(node.lineno)

    assert direct_terminal_updates == []


@pytest.mark.asyncio
async def test_on_message_config_accepts_from_user_identity():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    error = _validate_trigger_config(
        "set_trigger",
        "on_message",
        {"from_user_identity": "wecom:zhangsan"},
    )

    assert error is None


@pytest.mark.asyncio
async def test_on_message_config_accepts_from_agent_id():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    error = _validate_trigger_config(
        "set_trigger",
        "on_message",
        {"from_agent_id": str(uuid4())},
    )

    assert error is None


@pytest.mark.asyncio
async def test_evaluate_trigger_respects_backoff_until():
    import app.services.trigger_daemon as trigger_daemon

    now = datetime.now(timezone.utc)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="daily_report",
        type="once",
        config={
            "at": (now - timedelta(minutes=5)).isoformat(),
            "trigger_class": "scheduled_job",
            "backoff_until": (now + timedelta(minutes=30)).isoformat(),
        },
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=60,
        created_at=now - timedelta(hours=1),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is False


@pytest.mark.asyncio
async def test_evaluate_trigger_skips_fresh_inflight_fire():
    import app.services.trigger_daemon as trigger_daemon

    now = datetime.now(timezone.utc)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="one_shot",
        type="once",
        config={
            "at": (now - timedelta(minutes=1)).isoformat(),
            "_fire_inflight": {
                "event_key": "once:event",
                "runtime_task_id": "runtime-task-1",
                "started_at": now.isoformat(),
            },
        },
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=None,
        cooldown_seconds=0,
        created_at=now - timedelta(minutes=5),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is False


@pytest.mark.asyncio
async def test_trigger_fire_lease_failure_fails_closed(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    async def fake_get_redis():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(trigger_daemon, "get_redis", fake_get_redis)

    acquired = await trigger_daemon._acquire_trigger_fire_lease(uuid4(), "event-1")

    assert acquired is False


@pytest.mark.asyncio
async def test_poll_trigger_respects_five_minute_configured_interval(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    now = datetime.now(timezone.utc)
    checked = []

    async def fake_poll_check(trigger):
        checked.append(trigger.id)
        return True

    monkeypatch.setattr(trigger_daemon, "_poll_check", fake_poll_check)
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        name="fast_poll",
        type="poll",
        config={"interval_min": 5},
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=now - timedelta(minutes=6),
        cooldown_seconds=0,
        created_at=now - timedelta(hours=1),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is True
    assert checked == [trigger.id]


@pytest.mark.asyncio
async def test_interval_trigger_respects_configured_active_hours(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    now = datetime(2026, 5, 29, 1, 30, tzinfo=timezone.utc)  # 09:30 Asia/Shanghai
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="settings_patrol",
        type="interval",
        config={
            "minutes": 30,
            "active_hours": "09:00-10:00",
            "timezone": "Asia/Shanghai",
            "source": "settings_patrol",
            "trigger_class": "scheduled_job",
        },
        is_enabled=True,
        expires_at=None,
        max_fires=None,
        fire_count=0,
        last_fired_at=now - timedelta(minutes=45),
        cooldown_seconds=0,
        created_at=now - timedelta(hours=2),
    )

    assert await trigger_daemon._evaluate_trigger(trigger, now) is True

    outside_hours = now + timedelta(hours=2)
    assert await trigger_daemon._evaluate_trigger(trigger, outside_hours) is False


def test_interval_trigger_config_accepts_interval_alias():
    from app.services.agent_tool_domains.triggers import _validate_trigger_config

    config = {"interval": 5}

    error = _validate_trigger_config("set_trigger", "interval", config)

    assert error is None
    assert config["minutes"] == 5


@pytest.mark.asyncio
async def test_check_new_agent_messages_from_user_name_has_no_latest_message_fallback(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    tenant_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="wait_alice",
        type="on_message",
        config={"from_user_name": "Alice"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_fired_at=None,
        fire_count=0,
        reply_context=None,
    )
    fallback_message = SimpleNamespace(content="latest unrelated message")
    session = _SequenceSession(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id)),
            _ScalarResult(None),
            _ScalarResult(fallback_message),
        ]
    )

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=tenant_id)

    matched = await trigger_daemon._check_new_agent_messages(trigger)

    assert matched is False


@pytest.mark.asyncio
async def test_check_new_agent_messages_from_agent_name_rejects_ambiguous_agent_names(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    tenant_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="wait_ops_bot",
        type="on_message",
        config={"from_agent_name": "Ops Bot"},
        created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        last_fired_at=None,
        fire_count=0,
        reply_context=None,
    )
    ambiguous_agents = [
        SimpleNamespace(id=uuid4(), tenant_id=tenant_id, name="Ops Bot"),
        SimpleNamespace(id=uuid4(), tenant_id=tenant_id, name="Ops Bot"),
    ]
    participant_id = uuid4()
    matched_message = SimpleNamespace(content="status update")
    session = _SequenceSession(
        [
            _ScalarResult(SimpleNamespace(id=agent_id, tenant_id=tenant_id)),
            _ScalarsResult(ambiguous_agents),
            _ScalarResult(participant_id),
            _ScalarResult(matched_message),
        ]
    )

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=tenant_id)

    matched = await trigger_daemon._check_new_agent_messages(trigger)

    assert matched is False


@pytest.mark.asyncio
async def test_tick_does_not_apply_agent_level_dedup_window(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger_one = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="reply_1",
        type="on_message",
        config={},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
    )
    trigger_two = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="reply_2",
        type="on_message",
        config={},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
    )
    trigger_one_db = SimpleNamespace(**trigger_one.__dict__)
    trigger_two_db = SimpleNamespace(**trigger_two.__dict__)

    sessions = [
        _SequenceSession([_RowsResult([trigger_one])]),
        _SequenceSession([_RowsResult([trigger_one_db])]),
        _SequenceSession([_RowsResult([trigger_two])]),
        _SequenceSession([_RowsResult([trigger_two_db])]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    scheduled: list[str] = []

    async def fake_evaluate_trigger(trigger, _now):
        return {"event_key": str(trigger.id)}

    async def fake_create_runtime_task_record(**_kwargs):
        return "runtime-task"

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        scheduled.append(inner.cr_code.co_name)
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    # Stage-2b: the per-agent fire-state update is now tenant-scoped — route it
    # to the same session queue and stub the tenant resolution (no DB hit).
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    async def fake_queue(runtime_task_id, *, reason):
        scheduled.append(reason)

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon, "_queue_trigger_run_for_worker", fake_queue)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()
    await trigger_daemon._tick()

    assert scheduled == ["trigger_fired", "trigger_fired"]


@pytest.mark.asyncio
async def test_tick_creates_trigger_runtime_task_before_invocation(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="daily_brief",
        type="cron",
        config={"expr": "0 9 * * *"},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
        reason="Run daily brief",
    )
    trigger_db = SimpleNamespace(**trigger.__dict__)
    sessions = [
        _SequenceSession([_RowsResult([trigger])]),
        _SequenceSession([_RowsResult([trigger_db])]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": "daily"}

    created = []

    async def fake_create_runtime_task_record(**kwargs):
        created.append(kwargs)
        return "runtime-task-1"

    tenant_id = uuid4()
    budget_run_id = uuid4()
    budget_payloads = []

    class FakeRuntimeBudgetService:
        async def resolve_policy(self, lookup):
            return SimpleNamespace(
                id=uuid4(),
                enforcement_mode="enforce",
                fail_mode="fail_closed",
                max_tokens=1_000_000,
                max_cache_miss_tokens=250_000,
                max_subagents=32,
                max_delegations=32,
                max_background_tasks=32,
                max_continuation_wakes=64,
                max_provider_calls=128,
                default_child_token_reservation=50_000,
                default_llm_call_token_reservation=50_000,
                policy_json={"test": True},
            )

        async def create_run(self, payload):
            budget_payloads.append(payload)
            return SimpleNamespace(id=budget_run_id)

        async def reserve(self, reservation):
            budget_payloads.append(reservation)
            return SimpleNamespace(budget_run_id=reservation.budget_run_id, denied_dimensions=())

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    scheduled_runtime_ids = []

    async def fake_queue(runtime_task_id, *, reason):
        scheduled_runtime_ids.append(runtime_task_id)

    def fake_create_task(coro, *args, **kwargs):
        raise AssertionError("the tick must queue the run for the worker, not spawn it")

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    # Stage-2b: the per-agent fire-state update is now tenant-scoped — route it
    # to the same session queue and stub the tenant resolution (no DB hit).
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return tenant_id

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "RuntimeBudgetService", FakeRuntimeBudgetService, raising=False)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon, "_queue_trigger_run_for_worker", fake_queue)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()

    assert created[0]["task_type"] == "trigger"
    # Born claimable. Persisting ``running`` here kept the row out of the worker
    # claim queue while the daemon fire-and-forgot the coroutine, so a run that
    # died left an unobservable, unreclaimable row.
    assert created[0]["status"] == "pending"
    assert created[0]["parent_agent_id"] == agent_id
    assert created[0]["metadata_json"]["trigger_ids"] == [str(trigger.id)]
    assert created[0]["metadata_json"]["resume_after_restart"] is True
    assert created[0]["metadata_json"]["resumable_trigger"] is True
    assert created[0]["metadata_json"]["restart_replay_contract"]["task_type"] == "trigger"
    assert created[0]["metadata_json"]["restart_replay_journal"][0]["phase"] == "spawn_intent_recorded"
    assert created[0]["budget_run_id"] == budget_run_id
    assert created[0]["budget_admission_status"] == "reserved"
    assert created[0]["budget_reservation_key"] == f"trigger:{created[0]['task_id']}:start"
    assert created[0]["metadata_json"]["budget_run_id"] == str(budget_run_id)
    wake_candidate = created[0]["metadata_json"]["trigger_wake_context_candidate"]
    assert wake_candidate["context_candidate_ref"]["kind"] == "trigger_wake"
    assert wake_candidate["trigger_ids"] == [str(trigger.id)]
    assert wake_candidate["budget_run_id"] == str(budget_run_id)
    assert created[0]["metadata_json"]["context_candidate_refs"] == [wake_candidate["context_candidate_ref"]]
    assert budget_payloads[0].tenant_id == tenant_id
    assert budget_payloads[0].root_run_kind == "trigger_fire"
    assert budget_payloads[0].root_runtime_task_id.hex == created[0]["task_id"]
    assert budget_payloads[0].root_agent_id == agent_id
    assert budget_payloads[1].background_tasks == 1
    assert scheduled_runtime_ids == ["runtime-task-1"]


@pytest.mark.asyncio
async def test_tick_mark_inflight_failure_persists_terminal_budget_actuals(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="mark-inflight-failure",
        type="cron",
        config={"expr": "0 9 * * *"},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
    )
    session = _SequenceSession([_RowsResult([trigger])])

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": "daily"}

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    async def fake_preflight(*_args, **_kwargs):
        return True, None, "", {}

    async def fake_create_runtime_task(*_args, **_kwargs):
        return "runtime-task-mark-failed"

    async def fail_mark(*_args, **_kwargs):
        raise RuntimeError("inflight database unavailable")

    terminal_updates = []

    async def fake_update_runtime_task_record(task_id, **fields):
        terminal_updates.append((task_id, fields))
        return True

    async def fail_queue(*_args, **_kwargs):
        raise AssertionError("mark-inflight failure must not queue the trigger run")

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "_preflight_trigger_group", fake_preflight)
    monkeypatch.setattr(trigger_daemon, "_create_trigger_runtime_task", fake_create_runtime_task)
    monkeypatch.setattr(trigger_daemon, "_mark_trigger_fire_started", fail_mark)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(trigger_daemon, "_queue_trigger_run_for_worker", fail_queue)

    await trigger_daemon._tick()

    assert terminal_updates == [
        (
            "runtime-task-mark-failed",
            {
                "status": "failed",
                "result_summary": "Trigger fire could not be marked in-flight: inflight database unavailable",
                "metadata_json": {
                    "error": "inflight database unavailable",
                    "stage": "mark_inflight",
                    "runtime_budget_actuals": {"background_tasks": 1},
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_trigger_budget_approval_wait_persists_claimable_intent_without_starting(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon
    from app.services.runtime_budget_service import RuntimeBudgetApprovalRequired

    agent_id = uuid4()
    tenant_id = uuid4()
    budget_run_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="approval wake",
        type="cron",
        config={"expr": "0 9 * * *"},
    )
    captured: dict = {}

    async def fake_resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    async def fake_create_runtime_task_record(**kwargs):
        captured["task"] = kwargs
        return kwargs["task_id"]

    class WaitingBudgetService:
        async def resolve_policy(self, _lookup):
            return SimpleNamespace(
                id=uuid4(),
                enforcement_mode="enforce",
                fail_mode="require_confirmation",
                max_tokens=None,
                max_cache_miss_tokens=None,
                max_subagents=None,
                max_team_sessions=None,
                max_delegations=None,
                max_background_tasks=0,
                max_continuation_wakes=None,
                max_provider_calls=None,
                max_failures=None,
                max_needs_reconciliation=None,
                max_child_failure_ratio=None,
                max_parent_invocations=None,
                policy_json={},
            )

        async def create_run(self, _payload):
            return SimpleNamespace(id=budget_run_id)

        async def reserve(self, reservation):
            captured["reservation"] = reservation
            raise RuntimeBudgetApprovalRequired(
                "approval required",
                budget_run_id=reservation.budget_run_id,
                dimensions=["background_tasks"],
            )

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "RuntimeBudgetService", WaitingBudgetService)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)

    task_ref = await trigger_daemon._create_trigger_runtime_task(
        agent_id,
        [trigger],
        metadata_json={"preflight_allowed": True},
    )

    assert str(task_ref) == captured["task"]["task_id"]
    assert task_ref.admission_status == "waiting_budget_approval"
    assert captured["task"]["status"] == "pending"
    assert captured["task"]["budget_admission_status"] == "waiting_budget_approval"
    assert captured["task"]["budget_reservation_key"] == captured["reservation"].reservation_key


@pytest.mark.asyncio
async def test_tick_marks_once_trigger_inflight_without_disabling_before_ack(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="one_shot",
        type="once",
        config={"at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()},
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        last_fired_at=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=None,
        cooldown_seconds=0,
        reason="Run once",
    )
    trigger_db = SimpleNamespace(**trigger.__dict__)
    sessions = [
        _SequenceSession([_RowsResult([trigger])]),
        _SequenceSession([_RowsResult([trigger_db])]),
    ]

    def fake_async_session():
        if not sessions:
            raise AssertionError("Unexpected async_session() call")
        return sessions.pop(0)

    async def fake_evaluate_trigger(_trigger, _now):
        return {"event_key": "once:event"}

    async def fake_create_runtime_task_record(**_kwargs):
        return "runtime-task-1"

    async def fake_acquire_trigger_fire_lease(_trigger_id, _event_key):
        return True

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(trigger_daemon, "async_session", fake_async_session)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", lambda *a, **k: fake_async_session())

    async def _fake_resolve_tenant(_agent_id, *_a, **_k):
        return None

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "_evaluate_trigger", fake_evaluate_trigger)
    monkeypatch.setattr(trigger_daemon, "_acquire_trigger_fire_lease", fake_acquire_trigger_fire_lease)
    monkeypatch.setattr(trigger_daemon, "create_runtime_task_record", fake_create_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)
    _disable_completed_focus_reconciler(monkeypatch, trigger_daemon)
    trigger_daemon._last_invoke.clear()
    trigger_daemon._fire_history.clear()

    await trigger_daemon._tick()

    assert trigger_db.is_enabled is True
    assert trigger_db.fire_count == 0
    assert trigger_db.last_fired_at is None
    assert trigger_db.config["_fire_inflight"]["runtime_task_id"] == "runtime-task-1"


@pytest.mark.asyncio
async def test_invoke_trigger_marks_runtime_task_skipped_when_agent_has_no_model(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(id=uuid4(), name="daily_brief", type="cron", reason="Run")
    agent = SimpleNamespace(
        id=agent_id,
        name="No Model Agent",
        status="running",
        primary_model_id=None,
        tenant_id=uuid4(),
    )
    session = _SequenceSession([_ScalarResult(agent)])
    updates = []

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=agent.tenant_id)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id="runtime-task-1")

    assert updates[-1][0] == "runtime-task-1"
    assert updates[-1][1]["status"] == "skipped"
    assert updates[-1][1]["metadata_json"]["skip_reason"] == "no_model"


@pytest.mark.asyncio
async def test_invoke_trigger_blocks_when_agent_tenant_missing(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    trigger = SimpleNamespace(id=uuid4(), name="daily_brief", type="cron", reason="Run")
    updates = []
    opened_session = False

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    async def fake_resolve_tenant(_agent_id, *_args, **_kwargs):
        return None

    async def fake_admit_tenant(agent_id, *, source, **_kwargs):
        from app.runtime.tenant_admission import blocked_runtime_tenant_admission

        return blocked_runtime_tenant_admission(
            reason_code="agent_tenant_missing",
            message=f"{source} runtime is blocked because agent {agent_id} has no tenant.",
            source=source,
            agent_id=agent_id,
        )

    def fake_tenant_scoped_session(*_args, **_kwargs):
        nonlocal opened_session
        opened_session = True
        raise AssertionError("tenant_scoped_session should not open when runtime tenant admission blocks")

    monkeypatch.setattr(trigger_daemon, "resolve_tenant_for_agent", fake_resolve_tenant)
    monkeypatch.setattr(trigger_daemon, "admit_agent_runtime_tenant", fake_admit_tenant)
    monkeypatch.setattr(trigger_daemon, "tenant_scoped_session", fake_tenant_scoped_session)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)

    await trigger_daemon._invoke_agent_for_triggers(agent_id, [trigger], runtime_task_id="runtime-task-1")

    assert opened_session is False
    assert updates[-1][0] == "runtime-task-1"
    assert updates[-1][1]["status"] == "skipped"
    assert updates[-1][1]["metadata_json"]["skip_reason"] == "agent_tenant_missing"
    assert updates[-1][1]["metadata_json"]["precondition_status"] == "blocked_precondition"


@pytest.mark.asyncio
async def test_resume_persisted_trigger_runs_requeues_unstarted_run(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    run_id = uuid4().hex
    agent_id = uuid4()
    trigger_id = uuid4()
    trigger = SimpleNamespace(id=trigger_id, agent_id=agent_id, name="daily", type="cron")
    updates: list[tuple[str, dict]] = []
    scheduled: list[tuple[object, list[object], str | None]] = []

    async def fake_list_active_runtime_task_records(
        limit=50,
        statuses=("pending", "running"),
        task_types=None,
    ):
        assert task_types == ("trigger",)
        return [
            {
                "task_id": run_id,
                "task_type": "trigger",
                "parent_agent_id": str(agent_id),
                "prompt": "Trigger wake: daily",
                "trace_id": f"trigger:{run_id}",
                "child_session_id": None,
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_trigger": True,
                    "trigger_ids": [str(trigger_id)],
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "task_type": "trigger",
                        "task_id": run_id,
                        "idempotency_key": f"trigger:{run_id}:restart",
                    },
                },
            }
        ]

    async def fake_load_triggers_for_resume(_agent_id, trigger_ids):
        assert _agent_id == agent_id
        assert trigger_ids == [str(trigger_id)]
        return [trigger]

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    def fake_create_task(coro, *args, **kwargs):
        inner = coro.cr_frame.f_locals.get("awaitable", coro)
        frame = inner.cr_frame
        scheduled.append(
            (
                frame.f_locals["agent_id"],
                frame.f_locals["triggers"],
                frame.f_locals["runtime_task_id"],
            )
        )
        inner.close()
        if inner is not coro:
            coro.close()
        return SimpleNamespace()

    queued: list[tuple[str, str]] = []

    async def fake_queue(runtime_task_id, *, reason):
        queued.append((runtime_task_id, reason))

    monkeypatch.setattr(trigger_daemon, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(trigger_daemon, "_load_triggers_for_resume", fake_load_triggers_for_resume)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(trigger_daemon, "_queue_trigger_run_for_worker", fake_queue)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    resumed = await trigger_daemon.resume_persisted_trigger_runs()

    assert resumed == [run_id]
    # A resumed run goes back on the claim queue rather than straight into an
    # unowned background task: re-running it in-process is how the original
    # interruption became invisible in the first place.
    assert scheduled == []
    assert queued == [(run_id, "trigger_resumed_after_restart")]
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "pending"
    assert updates[-1][1]["metadata_json"]["resumed_after_restart"] is True
    assert updates[-1][1]["metadata_json"]["restart_replay_journal"][-1]["phase"] == "resume_intent_recorded"


@pytest.mark.asyncio
async def test_resume_persisted_trigger_runs_requires_reconciliation_after_session_bind(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    run_id = uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(
        limit=50,
        statuses=("pending", "running"),
        task_types=None,
    ):
        assert task_types == ("trigger",)
        return [
            {
                "task_id": run_id,
                "task_type": "trigger",
                "parent_agent_id": str(uuid4()),
                "prompt": "Trigger wake: daily",
                "trace_id": f"trigger:{run_id}",
                "child_session_id": str(uuid4()),
                "metadata": {
                    "resume_after_restart": True,
                    "resumable_trigger": True,
                    "trigger_ids": [str(uuid4())],
                    "side_effect_risk": "mutating",
                },
            }
        ]

    async def fake_update_runtime_task_record(task_id, **fields):
        updates.append((task_id, fields))
        return True

    def fake_create_task(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("session-bound mutating trigger must not be replayed blindly")

    monkeypatch.setattr(trigger_daemon, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(trigger_daemon, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(trigger_daemon.asyncio, "create_task", fake_create_task)

    resumed = await trigger_daemon.resume_persisted_trigger_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "session_bound_mutating_trigger"
    assert updates[-1][1]["metadata_json"]["runtime_budget_actuals"] == {"background_tasks": 1}


@pytest.mark.asyncio
async def test_preflight_group_blocks_autonomous_trigger_without_confirmed_plan(monkeypatch):
    """Plan Mode backstop (§9.0): the daemon preflight wrapper fails closed for an
    enabled autonomous trigger that has no confirmed plan, so the tick skips it
    instead of launching an invocation."""
    import app.services.trigger_daemon as trigger_daemon

    agent_id = uuid4()
    model_id = uuid4()
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="daily_brief",
        type="cron",
        config={"trigger_class": "scheduled_job", "expr": "0 9 * * *"},  # no plan_id
        max_fires=None,
        expires_at=None,
    )
    agent = SimpleNamespace(id=agent_id, name="A", status="running", primary_model_id=model_id, tenant_id=uuid4())
    model = SimpleNamespace(id=model_id, tenant_id=agent.tenant_id)
    # One session, sequenced: Agent lookup -> model pin lookup -> plan lookup (none).
    session = _SequenceSession([_ScalarResult(agent), _ScalarResult(model), _ScalarResult(None)])
    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=agent.tenant_id)

    ok, skip_reason, _summary, metadata = await trigger_daemon._preflight_trigger_group(
        agent_id, [trigger], datetime.now(timezone.utc)
    )

    assert ok is False
    assert skip_reason == "plan_required"
    assert metadata["trigger_id"] == str(trigger.id)


@pytest.mark.asyncio
async def test_preflight_group_allows_autonomous_trigger_with_confirmed_plan(monkeypatch):
    """A scheduled trigger whose config.plan_id points at a confirmed plan clears
    the backstop and the daemon lets it fire."""
    import app.services.trigger_daemon as trigger_daemon
    from app.models.plan_request import AgentPlanRequest

    agent_id = uuid4()
    model_id = uuid4()
    plan = AgentPlanRequest(
        id=uuid4(),
        agent_id=agent_id,
        source="web_chat",
        intent_type="autonomous_wake",
        original_request="每天 9 点帮我整理新闻",
        status="confirmed",
        plan_version=1,
        plan_hash="sha256:abc",
        plan_json={"schema": "hive_plan.v1", "title": "Daily brief"},
    )
    trigger = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        name="daily_brief",
        type="cron",
        config={
            "trigger_class": "scheduled_job",
            "expr": "0 9 * * *",
            "plan_id": str(plan.id),
            "plan_version": 1,
            "plan_hash": "sha256:abc",
            "plan_authorization": {
                "schema": "hive.plan_authorization_evidence.v1",
                "lease_id": str(uuid4()),
            },
        },
        max_fires=None,
        expires_at=None,
    )
    agent = SimpleNamespace(id=agent_id, name="A", status="running", primary_model_id=model_id, tenant_id=uuid4())
    model = SimpleNamespace(id=model_id, tenant_id=agent.tenant_id)
    session = _SequenceSession([_ScalarResult(agent), _ScalarResult(model), _ScalarResult(plan)])
    monkeypatch.setattr(trigger_daemon, "async_session", lambda: session)
    _route_scoped_session(monkeypatch, trigger_daemon, session, tenant_id=agent.tenant_id)

    async def fake_verify_consumed_lease(**_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(
        "app.services.plan_authorization_lease.verify_consumed_plan_authorization_lease",
        fake_verify_consumed_lease,
    )

    ok, skip_reason, _summary, _metadata = await trigger_daemon._preflight_trigger_group(
        agent_id, [trigger], datetime.now(timezone.utc)
    )

    assert ok is True
    assert skip_reason is None


# ── Exec/automation §2: three-bucket classification + event-driven v1 framing ──


def test_trigger_bucket_classifies_into_three_buckets():
    from app.services.agent_tool_domains.triggers import trigger_bucket

    assert trigger_bucket("cron") == "cron"
    assert trigger_bucket("interval") == "cron"  # recurring time-driven → cron bucket
    assert trigger_bucket("once") == "once"
    assert trigger_bucket("poll") == "event_driven"
    assert trigger_bucket("on_message") == "event_driven"
    assert trigger_bucket("webhook") == "event_driven"


def _ctx_trigger(**overrides):
    values = {
        "name": "t1",
        "type": "cron",
        "reason": "r",
        "config": {},
        "reply_context": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_trigger_context_frames_scheduled_run():
    from app.services.trigger_daemon import _build_trigger_context

    ctx, names = _build_trigger_context([_ctx_trigger(type="cron", name="daily")])
    assert "Scheduled trigger: daily (cron)" in ctx
    assert names == ["daily"]


def test_build_trigger_wake_context_candidate_records_budget_and_plan_refs():
    from app.services.trigger_daemon import _build_trigger_wake_context_candidate

    trigger = _ctx_trigger(
        id="trigger-1",
        type="cron",
        name="daily",
        config={
            "trigger_class": "scheduled_job",
            "plan_id": "plan-1",
            "plan_version": 3,
            "plan_hash": "hash-1",
        },
    )

    candidate = _build_trigger_wake_context_candidate(
        [trigger],
        runtime_task_id="run-1",
        budget_run_id="budget-1",
        preflight_decision={"dedup": "acquired", "rate_limit": "admitted"},
    )

    assert candidate["schema"] == "hive.ccplus.trigger_wake_context_candidate.v1"
    assert candidate["context_candidate_ref"]["kind"] == "trigger_wake"
    assert candidate["trigger_ids"] == ["trigger-1"]
    assert candidate["trigger_classes"] == ["scheduled_job"]
    assert candidate["budget_run_id"] == "budget-1"
    assert candidate["confirmed_plan_ref"] == {"plan_id": "plan-1", "plan_version": 3, "plan_hash": "hash-1"}
    assert candidate["preflight_decision"] == {"dedup": "acquired", "rate_limit": "admitted"}


def test_build_trigger_context_frames_event_driven_with_poll_change():
    # event-driven v1: the detected poll change must reach the agent, not just "a trigger fired".
    from app.services.trigger_daemon import _build_trigger_context

    trigger = _ctx_trigger(
        type="poll",
        name="price_watch",
        config={"_last_event": "Polled https://x → value changed from '1' to '2'"},
    )
    ctx, _ = _build_trigger_context([trigger])
    assert "Event from trigger: price_watch (poll)" in ctx
    assert "value changed from '1' to '2'" in ctx


def test_build_trigger_context_injects_on_message_and_webhook_payloads():
    from app.services.trigger_daemon import _build_trigger_context

    msg = _ctx_trigger(type="on_message", config={"_matched_message": "deploy failed", "_matched_from": "ci"})
    hook = _ctx_trigger(type="webhook", config={"_webhook_payload": '{"event":"push"}'})
    ctx, _ = _build_trigger_context([msg, hook])
    assert "deploy failed" in ctx
    assert '{"event":"push"}' in ctx


def test_build_trigger_context_omits_unknown_legacy_fields():
    from app.services.trigger_daemon import _build_trigger_context

    ctx, _ = _build_trigger_context([_ctx_trigger(type="cron", legacy_binding="some_task")])
    assert "legacy_binding" not in ctx
    assert "some_task" not in ctx
