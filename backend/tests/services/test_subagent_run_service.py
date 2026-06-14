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


def test_subagent_task_type_is_non_resumable():
    # A crashed background subagent must be reconciled to failed on restart, not
    # treated as restart-resumable (which would leave the parent's poll hanging).
    from app.services.runtime_task_service import _RESTART_RESUMABLE_TASK_TYPES

    assert svc.SUBAGENT_RUN_TASK_TYPE not in _RESTART_RESUMABLE_TASK_TYPES
