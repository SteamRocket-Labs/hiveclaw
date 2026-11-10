"""Member summaries preserve the complete observation sent to the provider."""

import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest


def test_write_summary_names_the_file_and_preserves_model_output(tmp_path):
    from app.kernel.engine import _normalize_tool_result_for_llm
    from app.services.agent_tool_domains.workspace import _write_file

    result = _write_file(tmp_path, "workspace/report.md", "报告")
    assert str(result) == "已写入文件「report.md」。"
    assert _normalize_tool_result_for_llm(result) == "✅ Written to workspace/report.md (2 chars)"
    assert (tmp_path / "workspace/report.md").read_text() == "报告"


@pytest.mark.parametrize("recovery", [False, True])
async def test_memory_summary_keeps_the_observation_and_source_hash(monkeypatch, tmp_path, recovery):
    from app.kernel.engine import _normalize_tool_result_for_llm
    from app.tools.handlers.memory import reconcile_memory_write, save_memory
    from app.tools.runtime import ToolExecutionContext

    result = NS(status="saved", category="user", target_hint="user", content="我的代号是白鹭-8", entry_id="saved")
    monkeypatch.setattr("app.config.get_settings", lambda: NS(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr("app.services.explicit_memory_authority.save_scoped_explicit_memory", AsyncMock(return_value=result))
    monkeypatch.setattr("app.services.explicit_memory_operations.reconcile_memory_operation", AsyncMock(return_value=result))
    monkeypatch.setattr("app.tools.handlers.memory._scoped_explicit_entries", AsyncMock(return_value=[NS(id="saved")]))
    record = AsyncMock()
    monkeypatch.setattr("app.services.explicit_memory_tool_sources.record_memory_result", record)
    context = ToolExecutionContext(agent_id=uuid4(), user_id=uuid4(), tenant_id=str(uuid4()), workspace=tmp_path)
    handler = reconcile_memory_write if recovery else save_memory
    output = await handler(context.agent_id, {"content": result.content, "category": "user", "action": "retry"}, execution_context=context)
    assert str(output) == "已记住：我的代号是白鹭-8"
    assert _normalize_tool_result_for_llm(output) == "Saved memory [user] (user): 我的代号是白鹭-8"
    assert record.call_args.kwargs["content"] == str(output)
    assert record.call_args.kwargs["entries"][0].id == "saved"


async def test_member_instructions_reach_the_provider_wire(monkeypatch):
    from app.runtime.prompt_builder import build_frozen_prompt_prefix
    from app.services.agent_context import build_agent_context
    from app.services.llm_client import LLMMessage, create_llm_client_from_config
    from app.tools.decorator import get_all_registered_tools
    from app.tools.handlers import work_ledger  # noqa: F401

    async def resource(**kwargs):
        return NS(content="", status="ok", ref=kwargs["resource_ref"])

    monkeypatch.setattr("app.services.agent_context.load_agent_context_resource", resource)
    monkeypatch.setattr("app.services.agent_context._load_company_intro", AsyncMock(return_value=("", "")))
    monkeypatch.setattr("app.runtime.prompt_sections.build_subagent_listing_section", lambda **kwargs: "")
    context = await build_agent_context(uuid4(), "我的助理", tenant_id=uuid4(), include_skill_catalog=False)
    prompt = build_frozen_prompt_prefix(agent_context=context)
    registered = get_all_registered_tools()
    tools = [{"type": "function", "function": {"name": name, "description": registered[name][0].description,
        "parameters": registered[name][0].parameters}} for name in ("report_progress", "track_todo")]
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "收到"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    client = create_llm_client_from_config({"api_key": "local-fake", "provider": "custom", "model": "local-fake", "base_url": "http://127.0.0.1:1/v1"})
    client._inner._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await client.complete(messages=[LLMMessage(role="system", content=prompt), LLMMessage(role="user", content="请记住我的代号，并跟进这三件事")], tools=tools)
    finally:
        await client.close()
    system = captured[0]["messages"][0]["content"]
    assert "Progress notes, including report_progress messages, must use the member's language" in system
    assert "Use file names, never absolute sandbox paths or shell commands" in system
    assert "keep those items one-to-one" in system and "not a todo" in system
    schemas = {tool["function"]["name"]: tool["function"]["description"] for tool in captured[0]["tools"]}
    assert "Write the message in the member's language, even if tool results are English" in schemas["report_progress"]
    assert "A request to remember a fact needs save_memory, not a todo" in schemas["track_todo"]
