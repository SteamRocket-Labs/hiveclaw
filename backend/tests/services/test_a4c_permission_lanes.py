"""Member-authored permission details remain source-bound on every entry lane."""
import json
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("lane", ["web", "trigger", "agent_task", "agent_web"])
@pytest.mark.parametrize("detail", ["pending_skill_handoffs", "skill_execution_plans", "writable_roots", "denied_reads", "denied_actions"])
async def test_permission_details_are_withheld_without_losing_computed_policy(monkeypatch, lane, detail):
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.kernel.contracts import ExecutionIdentityRef
    from app.runtime.context import SessionContext
    from app.services.llm_client import LLMResponse
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from tests.integration.test_a4c_one_item_withheld import _bindings

    tenant, agent, session_id, run_id = (uuid.uuid4() for _ in range(4))
    marker = "PERMISSION_HANDOFF_SECRET"
    handoff = {"skill": "Research", "execution_tool": "spawn_subagent", "tool_arguments": {"prompt": marker}}
    metadata = {"allowed_tools": ["search_memory"], detail: [handoff] if detail in {
        "pending_skill_handoffs", "skill_execution_plans"} else [marker]}
    session = SessionContext(session_id=str(session_id), source={"agent_task": "task", "agent_web": "web"}.get(lane, lane),
        metadata=metadata)
    monkeypatch.setattr("app.services.shared_group_authority.validate_shared_group_model_inputs", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", AsyncMock())
    captured = []

    class Client:
        async def stream(self, *, messages, **kwargs):
            bindings = _bindings(len(messages), {})
            for index, message in enumerate(messages):
                origin = message.context_origin
                if origin is not None and origin.unbound:
                    bindings["messages"][index]["dependency_status"] = "incomplete"
            wire = {"messages": [{"role": message.role, "content": message.content} for message in messages]}
            await _validate_or_withhold_model_sources(NS(get=AsyncMock(return_value=None)), tenant_id=tenant, agent_id=agent,
                session_id=session_id, run_id=run_id, input_bindings=bindings, messages=messages, wire_request=wire)
            captured.append(wire)
            return LLMResponse(content="ready", tool_calls=[], usage={"total_tokens": 1})

        async def close(self):
            pass

    kernel = AgentKernel(KernelDependencies(
        resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(tenant_id=tenant, max_tool_rounds=1),
        resolve_current_user_name=lambda *_args, **_kwargs: "",
        build_system_prompt=lambda *_args, **_kwargs: "",
        resolve_memory_context=lambda *_args, **_kwargs: "",
        get_tools=lambda *_args, **_kwargs: [],
        maybe_compress_messages=lambda messages, **_kwargs: messages,
        create_client=lambda _model: Client(), execute_tool=AsyncMock(),
        persist_memory=lambda **_kwargs: None, record_token_usage=lambda *_args, **_kwargs: None,
        get_max_tokens=lambda *_args, **_kwargs: 2048,
        extract_usage_tokens=lambda usage: usage.get("total_tokens"), estimate_tokens_from_chars=lambda chars: chars // 4))
    result = await kernel.handle(InvocationRequest(model=NS(provider="local", model="a4c", max_input_tokens=100000),
        messages=[], agent_name="Reader", role_description="Reader", agent_id=agent, user_id=uuid.uuid4(),
        session_context=session, allowed_tool_names=["search_memory"],
        execution_identity=ExecutionIdentityRef(identity_type="agent_bot", identity_id=agent, label="Reader")
        if lane.startswith("agent_") else None))
    assert result.content == "ready"
    assert captured
    assert marker not in json.dumps(captured)
    assert "有一项上下文暂时无法核实" in str(captured)
    if lane != "web":
        assert "approval_policy: platform_gate" in str(captured)
        assert "network_access: governed" in str(captured)
        if detail != "skill_execution_plans":
            assert "search_memory" in str(captured)
