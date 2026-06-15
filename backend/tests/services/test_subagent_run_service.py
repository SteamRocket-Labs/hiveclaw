"""Tests for durable background-subagent run records (Step 8)."""

from __future__ import annotations

import uuid

import pytest

from app.agents.subagent import SUBAGENT_TYPE_WORKER, SubagentResult
from app.services import subagent_run_service as svc


@pytest.mark.asyncio
async def test_start_subagent_run_records_running_subagent_task(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    run_id = await svc.start_subagent_run(
        parent_agent_id=parent, spec_name="scout", spec_type=SUBAGENT_TYPE_WORKER, task="do x"
    )
    assert run_id == captured["task_id"]
    assert captured["task_type"] == svc.SUBAGENT_RUN_TASK_TYPE == "subagent"
    assert captured["status"] == "running"
    assert captured["parent_agent_id"] == parent
    assert captured["child_agent_name"] == "scout"
    assert captured["metadata_json"]["subagent_type"] == SUBAGENT_TYPE_WORKER
    assert captured["metadata_json"]["resumable_subagent"] is True
    assert captured["metadata_json"]["resume_after_restart"] is True
    assert captured["metadata_json"]["side_effect_risk"] == "mutating"
    assert captured["metadata_json"]["restart_replay_contract"]["schema"] == "runtime_restart_replay_contract.v1"
    assert captured["metadata_json"]["restart_replay_contract"]["idempotency_key"] == f"subagent:{run_id}:restart"
    assert "restart_resume_blocker" not in captured["metadata_json"]


@pytest.mark.asyncio
async def test_start_subagent_run_marks_readonly_types_restart_resumable(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    await svc.start_subagent_run(parent_agent_id=parent, spec_name="scout", spec_type="explorer", task="read x")

    assert captured["metadata_json"]["subagent_type"] == "explorer"
    assert captured["metadata_json"]["resumable_subagent"] is True
    assert captured["metadata_json"]["resume_after_restart"] is True
    assert captured["metadata_json"]["subagent_name"] == "scout"


@pytest.mark.asyncio
async def test_run_completer_maps_ok_to_completed(monkeypatch):
    captured: dict = {}

    async def _fake_update(run_id, **fields):
        captured["run_id"] = run_id
        captured.update(fields)
        return True

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)
    completer = svc.make_run_completer("run-1")
    await completer(SubagentResult(name="scout", type="worker", status="completed", content="done", tokens_used=42))
    assert captured["run_id"] == "run-1"
    assert captured["status"] == "completed"
    assert captured["result_summary"] == "done"
    assert captured["token_usage"] == {"total_tokens": 42}
    assert captured["metadata_json"]["completion_journal"][-1]["status"] == "completed"
    assert captured["metadata_json"]["completion_journal"][-1]["idempotency_key"] == "subagent:run-1:completed"


@pytest.mark.asyncio
async def test_run_completer_maps_failure_to_failed(monkeypatch):
    captured: dict = {}

    async def _fake_update(run_id, **fields):
        captured.update(fields)
        return True

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)
    completer = svc.make_run_completer("run-2")
    await completer(SubagentResult(name="scout", type="worker", status="failed", error="boom"))
    assert captured["status"] == "failed"
    assert "boom" in captured["result_summary"]
    assert captured["metadata_json"]["completion_journal"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_get_subagent_run_is_ownership_scoped(monkeypatch):
    owner = uuid.uuid4()
    other = uuid.uuid4()

    async def _fake_get(_run_id):
        return {"task_type": "subagent", "parent_agent_id": str(owner), "status": "completed", "result": "r"}

    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get)
    assert await svc.get_subagent_run("rid", owner) is not None
    assert await svc.get_subagent_run("rid", other) is None  # another agent cannot read it


@pytest.mark.asyncio
async def test_get_subagent_run_rejects_non_subagent_task(monkeypatch):
    owner = uuid.uuid4()

    async def _fake_get(_run_id):
        return {"task_type": "web_chat_turn", "parent_agent_id": str(owner), "status": "running"}

    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get)
    assert await svc.get_subagent_run("rid", owner) is None


def test_spawn_schema_exposes_run_in_background_and_check_tool_registered():
    from app.tools.handlers.subagent import _SPAWN_PARAMETERS, check_subagent, spawn_subagent_tool  # noqa: F401

    assert "run_in_background" in _SPAWN_PARAMETERS["properties"]
    # check_subagent is a registered @tool (callable handler).
    assert callable(check_subagent)


def test_subagent_task_type_uses_metadata_resumability():
    # Restart-resumable subagent records are preserved for the restart pump; old
    # records without explicit resumability still fail closed.
    from app.services.runtime_task_service import _is_restart_resumable_runtime_task

    resumable = type(
        "RuntimeTaskStub",
        (),
        {
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "metadata_json": {"resume_after_restart": True, "resumable_subagent": True},
        },
    )()
    unsafe = type(
        "RuntimeTaskStub",
        (),
        {
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "metadata_json": {"resume_after_restart": False, "resumable_subagent": False},
        },
    )()

    assert _is_restart_resumable_runtime_task(resumable) is True
    assert _is_restart_resumable_runtime_task(unsafe) is False


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_rehydrates_readonly_worker(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(parent),
                "child_agent_name": "scout",
                "prompt": "read x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "scout",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            }
        ]

    async def fake_resolve_parent_runtime(parent_agent_id):
        calls["resolved_parent"] = parent_agent_id
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent,
                "parent_user_id": uuid.uuid4(),
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": uuid.uuid4(),
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return object()

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["resolved_parent"] == parent
    assert calls["spec"].type == "explorer"
    assert calls["task"] == "read x"
    assert calls["kwargs"]["run_in_background"] is True
    assert callable(calls["kwargs"]["on_complete"])


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_marks_mutating_record_for_reconciliation(monkeypatch):
    run_id = uuid.uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(uuid.uuid4()),
                "child_agent_name": "worker",
                "prompt": "write x",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not be replayed")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["side_effect_risk"] == "mutating"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_rehydrates_mutating_worker_with_replay_contract(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(parent),
                "child_agent_name": "worker",
                "prompt": "write x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": "subagent",
                    },
                },
            }
        ]

    async def fake_resolve_parent_runtime(parent_agent_id):
        calls["resolved_parent"] = parent_agent_id
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent,
                "parent_user_id": uuid.uuid4(),
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": uuid.uuid4(),
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return object()

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["resolved_parent"] == parent
    assert calls["spec"].type == "worker"
    assert calls["task"] == "write x"
    assert calls["kwargs"]["run_in_background"] is True
    assert callable(calls["kwargs"]["on_complete"])
