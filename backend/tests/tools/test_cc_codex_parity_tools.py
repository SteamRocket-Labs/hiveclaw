from __future__ import annotations

import importlib
import json
from pathlib import Path
from uuid import uuid4

import pytest


def _collect():
    from app.tools.collector import HANDLER_MODULES, collect_tools
    from app.tools.decorator import clear_registry

    clear_registry()
    for module_name in HANDLER_MODULES:
        importlib.reload(importlib.import_module(module_name))
    return collect_tools()


def _request(
    tool_name: str,
    arguments: dict,
    tmp_path: Path,
    *,
    agent_id=None,
    user_id=None,
    tenant_id=None,
    session_id=None,
):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name=tool_name,
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent_id or uuid4(),
            user_id=user_id or uuid4(),
            tenant_id=str(tenant_id or uuid4()),
            workspace=tmp_path,
            session_id=str(session_id or uuid4()),
        ),
    )


def test_cc_codex_parity_tools_are_registered():
    collected = _collect()
    names = {tool["function"]["name"] for tool in collected.openai_tools}

    assert {
        "task_create",
        "task_list",
        "task_get",
        "task_update",
        "task_output",
        "task_stop",
        "goal_start",
        "team_create",
        "advanced_plan",
        "verify_plan",
    } <= names

    assert "task_create" not in collected.sensitive_tools
    assert "task_create" not in collected.read_only_names
    assert "task_list" in collected.read_only_names
    assert "verify_plan" in collected.read_only_names


@pytest.mark.asyncio
async def test_team_create_tool_persists_through_agent_team_runtime(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity

    calls = []

    async def fake_create(request, *, name, members):
        calls.append((request, name, members))
        return {
            "ok": True,
            "requires_api_persist": False,
            "id": "team-1",
            "name": name,
            "status": "active",
            "transcript_truth": "chat_session_t0",
            "parent_session_id": request.context.session_id,
            "members": [
                {
                    "id": "member-1",
                    "member_name": members[0]["name"],
                    "member_role": members[0].get("role") or None,
                    "chat_session_id": "child-session-1",
                    "runtime_task_type": "team_member",
                    "status": "idle",
                }
            ],
        }

    monkeypatch.setattr(command_parity, "create_agent_team_from_tool_request", fake_create, raising=False)

    result = await command_parity.team_create(
        _request(
            "team_create",
            {"name": "research", "members": [{"name": "critic", "role": "Review"}]},
            tmp_path,
        )
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["requires_api_persist"] is False
    assert payload["members"][0]["member_name"] == "critic"
    assert calls and calls[0][1] == "research"


@pytest.mark.asyncio
async def test_task_create_tool_writes_work_ledger_without_execution(tmp_path, monkeypatch):
    from app.runtime.hooks import HookEvent
    from app.tools.handlers import command_parity

    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    emitted = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    monkeypatch.setattr(command_parity, "emit_hook", fake_emit_hook, raising=False)
    request = _request("task_create", {"subject": "Inspect hooks", "owner": "critic"}, tmp_path)
    result = await command_parity.task_create(request)

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["starts_execution"] is False
    assert payload["task"]["title"] == "Inspect hooks"
    assert payload["task"]["owner"] == "critic"
    assert emitted[0][0] == HookEvent.TASK_CREATED
    assert emitted[0][1]["session_id"] == request.context.session_id
    assert emitted[0][1]["metadata"]["task_id"] == payload["task"]["id"]


@pytest.mark.asyncio
async def test_task_update_completed_emits_task_completed_hook(tmp_path, monkeypatch):
    from app.runtime.hooks import HookEvent
    from app.tools.handlers import command_parity

    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    emitted = []

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    monkeypatch.setattr(command_parity, "emit_hook", fake_emit_hook, raising=False)
    agent_id = uuid4()
    session_id = uuid4()
    create_request = _request("task_create", {"subject": "Inspect hooks"}, tmp_path, agent_id=agent_id, session_id=session_id)
    created = json.loads(await command_parity.task_create(create_request))
    emitted.clear()
    request = _request(
        "task_update",
        {"task_id": created["task"]["id"], "subject": "Inspect hooks", "status": "completed"},
        tmp_path,
        agent_id=agent_id,
        session_id=session_id,
    )

    result = await command_parity.task_update(request)

    payload = json.loads(result)
    assert payload["ok"] is True
    assert emitted[0][0] == HookEvent.TASK_COMPLETED
    assert emitted[0][1]["session_id"] == request.context.session_id
    assert emitted[0][1]["metadata"]["task_id"] == created["task"]["id"]


@pytest.mark.asyncio
async def test_task_output_requires_runtime_task_handle(tmp_path):
    from app.tools.handlers.command_parity import task_output

    result = await task_output(_request("task_output", {}, tmp_path))

    payload = json.loads(result)
    assert payload["ok"] is False
    assert "runtime_task_id" in payload["error"]


@pytest.mark.asyncio
async def test_task_output_reads_runtime_task_record(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity

    runtime_task_id = str(uuid4())

    async def fake_get_runtime_task_record(task_id: str):
        assert task_id == runtime_task_id
        return {
            "task_id": runtime_task_id,
            "task_type": "team_member",
            "status": "completed",
            "result": "member summary",
            "metadata": {"artifact_paths": ["team/member.md"]},
            "parent_agent_id": None,
        }

    monkeypatch.setattr(command_parity, "get_runtime_task_record", fake_get_runtime_task_record)

    result = await command_parity.task_output(_request("task_output", {"runtime_task_id": runtime_task_id}, tmp_path))

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["task"]["status"] == "completed"
    assert payload["task"]["result"] == "member summary"
    assert payload["task"]["metadata"]["artifact_paths"] == ["team/member.md"]


@pytest.mark.asyncio
async def test_task_stop_cancels_runtime_task_record(tmp_path, monkeypatch):
    from app.tools.handlers import command_parity

    runtime_task_id = str(uuid4())
    calls: list[tuple[str, dict]] = []

    async def fake_get_runtime_task_record(task_id: str):
        assert task_id == runtime_task_id
        return {
            "task_id": runtime_task_id,
            "task_type": "goal_continuation",
            "status": "running",
            "parent_agent_id": None,
        }

    async def fake_update_runtime_task_record(task_id: str, **fields):
        calls.append((task_id, fields))
        return True

    monkeypatch.setattr(command_parity, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(command_parity, "update_runtime_task_record", fake_update_runtime_task_record)

    result = await command_parity.task_stop(
        _request("task_stop", {"runtime_task_id": runtime_task_id, "reason": "no longer needed"}, tmp_path)
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["status"] == "killed"
    assert calls == [
        (
            runtime_task_id,
            {
                "status": "killed",
                "result_summary": "no longer needed",
                "metadata_json": {"cancel_reason": "no longer needed"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_advanced_plan_tool_returns_runtime_handoff_payload(tmp_path):
    from app.tools.handlers.command_parity import advanced_plan

    result = await advanced_plan(
        _request("advanced_plan", {"objective": "Design parity rollout", "context": {"source": "freecode"}}, tmp_path)
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["requires_api_persist"] is True
    assert payload["runtime_task_type"] == "advanced_plan"
    assert payload["objective"] == "Design parity rollout"
    assert payload["context"] == {"source": "freecode"}


@pytest.mark.asyncio
async def test_verify_plan_tool_checks_criteria_and_evidence(tmp_path):
    from app.tools.handlers.command_parity import verify_plan

    result = await verify_plan(
        _request(
            "verify_plan",
            {
                "plan_json": {"success_criteria": ["tests pass"]},
                "evidence_refs": ["pytest://backend/tests/tools/test_cc_codex_parity_tools.py"],
                "completed_criteria": ["tests pass"],
            },
            tmp_path,
        )
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["verification"]["status"] == "passed"
