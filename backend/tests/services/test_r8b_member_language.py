"""Member presentation changes preserve the producing runtime's model facts."""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.mark.parametrize("structured", [False, True])
async def test_omitted_files_are_chinese_for_members_and_exact_for_the_model(monkeypatch, tmp_path, structured):
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.result_envelope import ToolContentEnvelope
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService
    from tests.tools.test_service import _FakeRegistry, _FakeRuntimeResolver

    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=tmp_path)
    omitted = "workspace/earlier-private-analysis.md"
    context.workspace_authority_scope = WorkspaceAuthorityScope(agent_id=context.agent_id, user_id=context.user_id,
        root_session_id=uuid4(), allowed_paths=frozenset({omitted}), operator_view=False, authority_source="resource_owner")
    async def select_inputs(request):
        request.context.workspace_authority_scope = replace(request.context.workspace_authority_scope, allowed_paths=frozenset())
    monkeypatch.setattr("app.services.content_input_receipts.authorize_workspace_receipt_read", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.content_input_receipts.authorize_workspace_execution_inputs", select_inputs)
    monkeypatch.setattr("app.services.skill_source_receipts.prepare_skill_input_capture", AsyncMock())
    monkeypatch.setattr("app.services.content_input_receipts.settle_workspace_receipt_read", AsyncMock())
    value = ToolContentEnvelope(text="42", model_visible_text="Exact computation: 42", metadata={"retained": True}) if structured else "42"
    service = ToolRuntimeService(runtime_resolver=_FakeRuntimeResolver(context), governance_resolver=NS(),
        registry=_FakeRegistry(value), ensure_registry=lambda: None, governance_runner=lambda **kwargs: None,
        fallback_executor=lambda *args: None, direct_fallback_executor=lambda *args: None,
        asset_ref_resolver=lambda **kwargs: (), pack_policy_loader=lambda _: {})
    result = await service.execute_with_context("read_file", {"path": "workspace/input.txt"}, context)
    assert isinstance(result, ToolContentEnvelope), str(result)
    assert str(result) == "42\n\n部分文件暂时无法读取，已使用其余可用资料继续处理。"
    assert omitted not in str(result) and "Files omitted" not in str(result)
    assert omitted in result.model_visible_text and "sources could not be verified" in result.model_visible_text
    assert ("Exact computation: 42" if structured else "42") in result.model_visible_text
    from app.kernel.engine import _extract_tool_side_effects, _normalize_tool_result_for_llm
    assert _normalize_tool_result_for_llm(result) == result.model_visible_text
    assert _extract_tool_side_effects(result)["raw_tool_result"] == str(result)
    if structured:
        assert result.metadata == {"retained": True}


async def test_timeout_is_produced_in_chinese_with_typed_failure_facts(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, OrchestrationPolicy, _delegate
    from tests.agents.test_orchestrator import _a2a_authority_kwargs

    target, owner = NS(id=uuid4(), name="周报助手", role_description="Helpful"), uuid4()
    async def timeout(_):
        raise TimeoutError
    monkeypatch.setattr("app.agents.orchestrator.invoke_agent", timeout)
    result = await _delegate(AgentDelegationRequest(target=target, target_model=NS(provider="synthetic", model="local"),
        conversation_messages=[{"role": "user", "content": "本周要点"}], owner_id=owner, session_id="child-session",
        policy=OrchestrationPolicy(timeout_seconds=600),
        **_a2a_authority_kwargs(target=target, owner_id=owner, session_id="child-session")))
    assert result.content == "委派给周报助手的任务超过 600 秒未完成，结果未知。"
    assert result.failed and result.timed_out and result.to_dict()["status"] == "timed_out"


async def test_delegated_session_title_is_chinese_and_instruction_bytes_are_preserved(monkeypatch):
    from app.agents.orchestrator import AgentDelegationRequest, _ensure_peer_delegation_session
    from app.models.chat_session import ChatSession
    from tests.agents.test_orchestrator import _a2a_authority_kwargs

    target, owner, session_id = NS(id=uuid4(), name="周报助手"), uuid4(), str(uuid4())
    instruction = "Goal: 请汇总本周工作。"
    added, events = [], []
    db = NS(execute=AsyncMock(return_value=NS(scalar_one_or_none=lambda: None)),
        add=added.append, flush=AsyncMock(), commit=AsyncMock())
    @asynccontextmanager
    async def scope(*args):
        yield db
    async def append(**kwargs):
        events.append(kwargs)
    monkeypatch.setattr("app.agents.orchestrator._delegation_tenant_session", scope)
    monkeypatch.setattr("app.services.chat_transcript.append_session_event", append)
    request = AgentDelegationRequest(target=target, target_model=NS(provider="synthetic", model="local"),
        conversation_messages=[{"role": "user", "content": instruction}], owner_id=owner, session_id=session_id,
        runtime_task_id=str(uuid4()), interaction_type="delegation",
        **_a2a_authority_kwargs(target=target, owner_id=owner, session_id=session_id))
    assert await _ensure_peer_delegation_session(request, state="running")
    session = next(row for row in added if isinstance(row, ChatSession))
    assert session.title == "委派：周报助手"
    assert events[0]["content"] == instruction


def test_delegation_brief_uses_the_members_language_and_public_assistant_name():
    from app.runtime.prompts.delegation import build_delegation_brief_contract
    prompt = build_delegation_brief_contract()
    assert "Write the brief and its headings in the member's language" in prompt
    assert "localized public employee names" in prompt and "我的助理" in prompt


def test_progress_instruction_names_the_members_language():
    from app.runtime.prompts.behavior import build_behavior_contract_section
    assert "Write progress updates in the member's language." in build_behavior_contract_section()


def test_creation_tool_instructs_public_readiness_without_losing_identifiers():
    from app.tools.handlers.hr import create_employee_from_request
    from app.tools.decorator import get_all_registered_tools
    description = get_all_registered_tools()["create_employee_from_request"][0].description
    assert create_employee_from_request is not None
    assert "do not repeat internal identifiers, status codes, or A2A terminology" in description
    assert "retain them for subsequent tool calls" in description
