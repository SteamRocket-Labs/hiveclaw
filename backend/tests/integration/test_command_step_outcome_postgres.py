"""Command exit facts survive the native tool pipeline and canonical transcript."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages,
    _prepare_write,
    native_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("tool_name", ["run_command", "execute_code"])
@pytest.mark.parametrize("exit_code,timed_out,error,expected_exit,approved", [
    (0, False, None, 0, False),
    (7, False, None, 7, False),
    (7, False, "Sandbox command failed.", 7, False),
    (0, True, "Command timed out", None, False),
    (0, False, "Command result lost", None, False),
    pytest.param(0, False, None, 0, True, id="approved-zero"),
    pytest.param(7, False, None, 7, True, id="approved-nonzero"),
])
async def test_command_exit_is_stored_and_served_without_changing_model_output(
    native_workspace,  # noqa: F811
    app_user_sessionmaker, owner_sessionmaker, monkeypatch,
    tool_name, exit_code, timed_out, error, expected_exit, approved,
):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionEventOutbox, SessionToolInvocation
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.web_chat_runtime import _persist_tool_call

    n = native_workspace
    async with owner_sessionmaker() as db:
        if approved:
            await db.execute(delete(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant))
            assert await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == n.tenant)) is None
        else:
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=(
                "workspace.command.execute" if tool_name == "run_command" else "workspace.code.execute"
            ), allowed=True, requires_approval=False))
        await db.commit()
    command = {"command": "wc -l report.md"} if tool_name == "run_command" else {
        "language": "python", "code": "print(75)"
    }
    provider = AsyncMock(
        return_value=CodeExecutionResult(stdout="75 report.md\n", exit_code=exit_code,
            timed_out=timed_out, error=error)
    )
    monkeypatch.setattr("app.services.code_execution.service._execute_selected_command" if approved
        else "app.services.agent_tool_domains.code_exec.execute_agent_command", provider)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", AsyncMock(return_value=None))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        messages = await _admitted_write_messages(db, n, content="Count report lines using a command")
        invocation, arguments, turn = await _prepare_write(
            db, n, messages=messages, tool_name=tool_name, arguments=command,
        )

    async def execute(name, args, _request, _emit, *, trace_metadata_sink, **_kwargs):
        return await n.service.execute(name, args, agent_id=n.agent, user_id=n.user,
            session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
            tool_call_id=invocation.provider_tool_use_id, emit_runtime_hooks=False,
            trace_metadata_sink=trace_metadata_sink)

    side_effects = {}
    result, effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=execute,
        request=InvocationRequest(model=SimpleNamespace(provider="openai", model="local-fake"),
            messages=messages, agent_name="Counter", role_description="Count report lines",
            agent_id=n.agent, user_id=n.user, memory_session_id=str(n.session)),
        runtime_config=RuntimeConfig(tenant_id=n.tenant, max_tool_rounds=1), tool_name=tool_name,
        tool_args=arguments, tool_call_id=invocation.provider_tool_use_id,
        emit_event=AsyncMock(), record_span=AsyncMock(), side_effect_sink=side_effects,
    )
    assert executed is (not approved)
    if approved:
        provider.assert_not_awaited()
        assert side_effects["tool_execution_evidence"]["tool_decision"]["outcome"] == "require_approval"
    else:
        assert error in result if error else "75 report.md\n" in result
    envelopes = await _persist_tool_call(agent_id=n.agent, user_id=n.user, session_id=str(n.session),
        data={"name": tool_name, "args": effective_args, "status": "done", "result": result,
            "tool_call_id": invocation.provider_tool_use_id,
            "provider_request_id": invocation.provider_request_id, "runtime_task_id": str(n.run),
            "tool_execution_evidence": side_effects["tool_execution_evidence"]})
    if approved:
        from tests.integration.test_session_permission_execution_once import _mounted_client

        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, invocation.id)
            assert invocation.permission_item_id is not None
            task = await db.get(RuntimeTask, n.run)
            task.status = "suspended"
            task.metadata_json = {**task.metadata_json, "interactive_pause": "awaiting_session_permission"}
            await db.commit()
        monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", AsyncMock())
        seed = SimpleNamespace(tenant_id=n.tenant, user_id=n.user)
        async with _mounted_client(app_user_sessionmaker, seed) as client:
            response = await client.post(
                f"/api/agents/{n.agent}/sessions/{n.session}/permissions/{invocation.permission_item_id}/resolve",
                json={"action": "allow_once"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["run_id"] == str(n.run)
        provider.assert_awaited_once()
        async with owner_sessionmaker() as db:
            rows = (await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.invocation_id == invocation.id,
                ChatTranscriptEvent.item_kind.in_(["tool_call", "tool_result"]),
                ChatTranscriptEvent.lifecycle == "completed",
            ))).all()
            envelopes = [(await db.scalar(select(SessionEventOutbox).where(
                SessionEventOutbox.event_id == row.id))).envelope_json for row in rows]
            assert (await db.get(RuntimeTask, n.run)).status == "resumable"
            result = next(row.content for row in rows if row.item_kind == "tool_result")
            assert "75 report.md\n" in result
    from tests.integration.test_session_permission_execution_once import _mounted_client

    seed = SimpleNamespace(tenant_id=n.tenant, user_id=n.user)
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        served = await client.get(f"/api/agents/{n.agent}/sessions/{n.session}/transcript?schema_version=2")
        assert served.status_code == 200, served.text
    expected = {"exit_code": expected_exit, "timed_out": timed_out}
    terminal = [e for e in envelopes if e["item_kind"] in {"tool_call", "tool_result"}]
    assert len(terminal) == 2
    for envelope in terminal:
        assert envelope["payload"].get("code_execution_result") == expected
        replay = next(item for item in served.json() if item["event_id"] == envelope["event_id"])
        assert replay["payload"]["code_execution_result"] == expected
    async with owner_sessionmaker() as db:
        row = await db.scalar(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.invocation_id == invocation.id,
            ChatTranscriptEvent.item_kind == "tool_result"))
        assert row.content == result
        assert row.metadata_json["v2_payload"]["code_execution_result"] == expected
        outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == row.id))
        assert outbox.envelope_json["payload"]["code_execution_result"] == expected
