from __future__ import annotations

import uuid

import pytest


@pytest.mark.asyncio
async def test_runtime_reasoner_invokes_agent_with_tools_disabled(monkeypatch):
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner

    captured = {}

    class _Model:
        provider = "openai"
        model = "gpt-test"
        api_key = "test"
        base_url = None
        max_output_tokens = None

    class _Agent:
        id = uuid.uuid4()
        name = "Researcher"
        tenant_id = uuid.uuid4()
        primary_model_id = uuid.uuid4()
        fallback_model_id = None

    async def fake_resolve_models(self):
        return _Model(), None, _Agent()

    async def fake_invoke_agent(request):
        captured["initial_tools"] = request.initial_tools
        captured["expand_tools"] = request.expand_tools
        captured["max_tool_rounds"] = request.max_tool_rounds
        captured["source"] = request.session_context.source
        return type("Result", (), {"content": '{"lanes":[]}'})()

    monkeypatch.setattr(RuntimeDeepResearchReasoner, "_resolve_models", fake_resolve_models)
    monkeypatch.setattr("app.services.deep_research.reasoner.invoke_agent", fake_invoke_agent)

    reasoner = RuntimeDeepResearchReasoner(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    await reasoner._invoke("plan", "return json")

    assert captured == {
        "initial_tools": [],
        "expand_tools": False,
        "max_tool_rounds": 1,
        "source": "deep_research",
    }
