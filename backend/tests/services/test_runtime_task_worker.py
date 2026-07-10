from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_worker_claimable_task_types_cover_v3_runtime_planes():
    import app.services.runtime_task_worker as worker

    assert "web_chat_turn" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "workflow" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "delegation" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "business_task" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "subagent" in worker.SUPPORTED_RUNTIME_TASK_TYPES
    assert "trigger" in worker.SUPPORTED_RUNTIME_TASK_TYPES


def test_worker_claim_batch_is_capped_by_active_web_chat_runs(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=4,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 7)

    assert worker._claim_batch_size_for_available_slots() == 1

    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 8)

    assert worker._claim_batch_size_for_available_slots() == 0


def test_worker_claim_batch_uses_configured_batch_when_capacity_allows(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=4,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)

    assert worker._claim_batch_size_for_available_slots() == 4


def test_worker_task_type_limit_parser_caps_claimable_types(monkeypatch):
    import app.services.runtime_task_worker as worker

    monkeypatch.setattr(
        worker,
        "_settings",
        lambda: SimpleNamespace(
            RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS="web_chat_turn=2,workflow=1,delegation=3,subagent=2",
            RUNTIME_TASK_WORKER_MAX_CONCURRENT=8,
            RUNTIME_TASK_WORKER_BATCH_SIZE=8,
        ),
    )
    monkeypatch.setattr(worker, "active_web_chat_run_count", lambda: 2)
    monkeypatch.setattr(
        worker,
        "_active_dispatched_task_type_counts",
        lambda: {"web_chat_turn": 2, "workflow": 0, "subagent": 1},
    )

    assert worker._task_type_capacity_remaining("web_chat_turn") == 0
    assert worker._task_type_capacity_remaining("workflow") == 1
    assert worker._task_type_capacity_remaining("subagent") == 1
    assert worker._claimable_task_types_for_available_capacity() == ("workflow", "delegation", "subagent")


def test_default_worker_capacity_is_not_cc_hostile():
    import app.services.runtime_task_worker as worker
    from app.config import get_settings

    settings = get_settings()
    limits = worker._parse_task_type_limits(settings.RUNTIME_TASK_WORKER_TASK_TYPE_LIMITS)

    assert settings.RUNTIME_TASK_WORKER_MAX_CONCURRENT >= 16
    assert settings.RUNTIME_TASK_WORKER_BATCH_SIZE >= 8
    assert limits["web_chat_turn"] >= 16
    assert limits["workflow"] >= 16
    assert limits["delegation"] >= 16
    assert limits["subagent"] >= 16
    assert limits["trigger"] >= 8


def test_worker_dispatches_claimed_subagent_to_runtime_task_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="subagent")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "subagent",
        "coro_name": "_execute_claimed_subagent_task",
    }


def test_worker_dispatches_claimed_trigger_to_runtime_task_executor(monkeypatch):
    import app.services.runtime_task_worker as worker

    captured = {}

    def fake_dispatch(task, coro, *, task_type):
        captured["task"] = task
        captured["task_type"] = task_type
        captured["coro_name"] = coro.cr_code.co_name
        coro.close()
        return True

    task = SimpleNamespace(id=uuid4(), task_type="trigger")
    monkeypatch.setattr(worker, "_dispatch_async_runtime_task", fake_dispatch)

    assert worker._dispatch_claimed_task(task) is True
    assert captured == {
        "task": task,
        "task_type": "trigger",
        "coro_name": "_execute_claimed_trigger_task",
    }


@pytest.mark.asyncio
async def test_execute_claimed_business_task_marks_failed_on_executor_error(monkeypatch):
    import app.services.runtime_task_worker as worker

    runtime_task_id = uuid4()
    business_task_id = uuid4()
    agent_id = uuid4()
    updates = []

    async def fake_get_runtime_task_record(task_id):
        assert task_id == runtime_task_id.hex
        return {
            "metadata": {"business_task_id": str(business_task_id)},
            "parent_agent_id": str(agent_id),
        }

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    async def fake_execute_task(_business_task_id, _agent_id):
        raise RuntimeError("executor exploded")

    monkeypatch.setattr("app.services.runtime_task_service.get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr("app.services.runtime_task_service.update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)

    await worker._execute_claimed_business_task(runtime_task_id)

    assert any(
        task_id == runtime_task_id.hex
        and payload.get("status") == "failed"
        and "executor exploded" in payload.get("result_summary", "")
        for task_id, payload in updates
    )


@pytest.mark.asyncio
async def test_execute_claimed_subagent_task_dispatches_persisted_run(monkeypatch):
    import app.services.runtime_task_worker as worker

    runtime_task_id = uuid4()
    calls = []

    async def fake_dispatch_persisted_subagent_run(task_id):
        calls.append(task_id)
        return True

    monkeypatch.setattr(
        "app.services.subagent_run_service.dispatch_persisted_subagent_run",
        fake_dispatch_persisted_subagent_run,
        raising=False,
    )

    await worker._execute_claimed_subagent_task(runtime_task_id)

    assert calls == [runtime_task_id.hex]
