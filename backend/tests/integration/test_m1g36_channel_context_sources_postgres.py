"""Native channel context pins the returned version and keeps current reader authority."""

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.database import pin_rls_tenant_context
from app.kernel.engine import _mid_run_items_to_user_messages
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.content_input_receipts import (
    ContentInputReceiptError, validate_browser_input_dependencies, validate_runtime_input_dependencies,
)
from app.services.llm_client import LLMMessage
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input
from tests.integration.test_m1g30_model_dependencies_postgres import _snapshot

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _read(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, *, ref="channels", offset=0, configured=True, unbound=False):
    from app.services import agent_tools
    from app.services.session_model_round import seal_model_response, commit_sealed_model_round
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import prepare_tool_invocation, complete_tool_invocation

    tenant, agent, session, run, bound = await _bound_input(owner_sessionmaker)
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.governance_resolver.async_session", app_user_sessionmaker)
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    async with owner_sessionmaker() as db:
        user = (await db.get(ChatSession, session)).user_id
        (await db.get(User, user)).role = "member"
        channel = ChannelConfig(tenant_id=tenant, agent_id=agent, channel_type="feishu", is_configured=configured)
        db.add(channel)
        await db.commit()
        channel_id = channel.id
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        messages = [{"role": "user", "content": "Legacy input without a source"}] if unbound else _mid_run_items_to_user_messages(bound)
        request_id, _ = await _snapshot(db, tenant, agent, session, run, messages)
        args, call, turn = {"ref": ref, "offset": offset}, f"context-{run.hex}", f"turn-{run.hex}"
        await seal_model_response(db, tenant_id=tenant, agent_id=agent, session_id=session, run_id=run,
            turn_id=turn, round_index=1, provider_request_id=request_id, response={"content": None,
                "tool_calls": [{"id": call, "type": "function", "function": {
                    "name": "read_context_resource", "arguments": json.dumps(args)}}], "finish_reason": "tool_calls", "usage": {}})
        await commit_sealed_model_round(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, turn_id=turn, round_index=1, provider_request_id=request_id)
        invocation = await prepare_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            run_id=run, provider_request_id=request_id, provider_tool_use_id=call, tool_name="read_context_resource", arguments=args)
        await db.commit()
        trace = {}
        raw = str(await agent_tools._get_tool_runtime_service().execute("read_context_resource", args,
            agent_id=agent, user_id=user, session_id=str(session), runtime_task_id=str(run), turn_id=turn,
            tool_call_id=call, emit_runtime_hooks=False, trace_metadata_sink=trace))
        assert json.loads(raw)["status"] == "ok", raw
        await complete_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id, provider_result_content=raw, execution_evidence=_execution_evidence(trace))
        await db.commit()
    request = ToolExecutionRequest(tool_name="read_file", arguments={"path": "workspace/derived.md"},
        context=ToolExecutionContext(agent_id=agent, user_id=user, tenant_id=str(tenant), session_id=str(session), workspace=tmp_path))
    return SimpleNamespace(tenant=tenant, agent=agent, session=session, run=run, user=user, channel=channel_id,
        request=request, raw=raw, message=LLMMessage(role="tool", content=raw, tool_call_id=call,
            source_ref=f"session-tool-result:{invocation.id}"))


@pytest.mark.parametrize("change", [None, "configured_channels", "employee_owner"])
async def test_actual_channel_read_remains_versioned_and_revalidates_current_readers(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change,
):
    s = await _read(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    version = json.loads(s.raw)["sha256"]
    if change:
        async with owner_sessionmaker() as db:
            if change == "configured_channels":
                (await db.get(ChannelConfig, s.channel)).is_configured = False
            else:
                other = User(id=uuid.uuid4(), tenant_id=s.tenant, username=f"owner-{s.agent.hex}",
                    email=f"{s.agent.hex}@fixture.test", password_hash="fixture", display_name="New owner", role="member")
                db.add(other)
                await db.flush()
                employee = await db.get(Agent, s.agent)
                assert (employee.owner_user_id or employee.creator_id) == s.user
                employee.owner_user_id = other.id
            await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        _, snapshot = await _snapshot(db, s.tenant, s.agent, s.session, s.run, [s.message], round_index=2)
        catalog = snapshot["input_bindings"]
        dependency = {"source_kind": "agent_context_resource", "source_id": f"{s.agent}\nchannels", "source_version": version}
        assert dependency in catalog["dependencies"], catalog
        assert catalog["dependency_coverage"]["complete"] is True and catalog["source_authority"] == "not_evaluated"
        assert snapshot["wire_request"]["messages"] == [{"role": "tool", "content": s.raw,
            "tool_call_id": s.message.tool_call_id}]
        user = await db.get(User, s.user)
        for validate in [lambda: validate_runtime_input_dependencies(db, s.request, catalog["dependencies"]),
            lambda: validate_browser_input_dependencies(db, s.tenant, user, catalog["dependencies"])]:
            if change:
                with pytest.raises(ContentInputReceiptError) as error:
                    await validate()
                assert error.value.status_code == 403
            else:
                await validate()


@pytest.mark.parametrize("ref,offset", [("channels", 1), ("company", 0)])
async def test_partial_or_unproved_context_keeps_input_coverage_incomplete(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, ref, offset,
):
    s = await _read(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, ref=ref, offset=offset)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        _, snapshot = await _snapshot(db, s.tenant, s.agent, s.session, s.run, [s.message], round_index=2)
        catalog = snapshot["input_bindings"]
        assert catalog["dependency_coverage"]["complete"] is False
        assert {row["source_kind"] for row in catalog["dependencies"]} == {"session_command", "session_tool_invocation"}


async def test_channel_fact_does_not_certify_unproved_parent_inputs_or_another_employee(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    s = await _read(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, unbound=True)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        _, snapshot = await _snapshot(db, s.tenant, s.agent, s.session, s.run, [s.message], round_index=2)
        catalog = snapshot["input_bindings"]
        assert catalog["dependency_coverage"]["complete"] is False
        dependency = next(row for row in catalog["dependencies"] if row["source_kind"] == "agent_context_resource")
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [{**dependency, "source_id": f"{uuid.uuid4()}\nchannels"}])
        assert error.value.status_code == 403


async def test_unavailable_channel_query_cannot_be_certified_as_a_successful_empty_resource(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.tools.handlers.context_resources import read_context_resource

    s = await _read(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, configured=False)
    assert json.loads(s.raw)["content"] == ""
    dependency = {"source_kind": "agent_context_resource", "source_id": f"{s.agent}\nchannels",
        "source_version": hashlib.sha256(b"").hexdigest()}
    @asynccontextmanager
    async def unavailable(*_args, **_kwargs):
        raise OSError("synthetic channel database outage")
        yield
    monkeypatch.setattr("app.database.tenant_scoped_session", unavailable)
    payload = json.loads(await read_context_resource(ToolExecutionRequest(tool_name="read_context_resource",
        arguments={"ref": "channels"}, context=s.request.context)))
    assert payload["status"] == "unavailable" and payload["retryable"] is True, payload
    assert "content" not in payload and "sha256" not in payload
    from app.services.agent_context import load_agent_context_resource

    index = await load_agent_context_resource(agent_id=s.agent, tenant_id=s.tenant, resource_ref="index")
    channel_line = next(line for line in index.content.splitlines() if line.startswith("- channels:"))
    assert "status=unavailable" in channel_line and "sha256=" not in channel_line
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.tenant)
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [dependency])
        assert error.value.status_code == 503
