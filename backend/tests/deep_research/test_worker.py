from __future__ import annotations

import uuid

import pytest


class _StubModel:
    provider = "openai"
    model = "gpt-test"
    api_key = "test"
    base_url = None
    max_output_tokens = None


class _StubAgent:
    id = uuid.uuid4()
    name = "Researcher"
    tenant_id = uuid.uuid4()
    primary_model_id = uuid.uuid4()
    fallback_model_id = None


@pytest.mark.asyncio
async def test_research_worker_invokes_agent_with_governed_web_tool_surface():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import (
        RESEARCH_WORKER_ALLOWED_TOOLS,
        RESEARCH_WORKER_EXCLUDED_TOOLS,
        RuntimeResearchWorker,
    )

    captured = {}

    async def fake_invoke(request):
        captured["core_tools_only"] = request.core_tools_only
        captured["allowed_tool_names"] = request.allowed_tool_names
        captured["excluded_tool_names"] = request.excluded_tool_names
        captured["expand_tools"] = request.expand_tools
        captured["max_tool_rounds"] = request.max_tool_rounds
        captured["source"] = request.session_context.source
        captured["metadata"] = request.session_context.metadata
        return type("Result", (), {"content": "## Worker digest\n\nFinding A with citation [src_pending].", "tokens_used": 321})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("RWA custody lane", request=ResearchRequest(question="research RWA"))

    assert result.status == "ok"
    assert result.intermediate_report.startswith("## Worker digest")
    assert result.tokens_used == 321
    assert captured["core_tools_only"] is False
    assert captured["allowed_tool_names"] == RESEARCH_WORKER_ALLOWED_TOOLS
    assert captured["excluded_tool_names"] == RESEARCH_WORKER_EXCLUDED_TOOLS
    assert captured["expand_tools"] is False
    assert captured["max_tool_rounds"] >= 6
    assert captured["source"] == "deep_research_worker"
    assert captured["metadata"]["topic"] == "RWA custody lane"


@pytest.mark.asyncio
async def test_research_worker_captures_only_completed_fetch_tool_events_as_sources():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import RuntimeResearchWorker

    callback_holder = {}

    async def fake_invoke(request):
        callback_holder["on_tool_call"] = request.on_tool_call
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "status": "running",
                "args": {"url": "https://ignore.example/running"},
                "result": "Title: Should Not Capture\nThis is still running.",
            }
        )
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "status": "done",
                "args": {"url": "https://issuer.example/rwa"},
                "result": (
                    "Title: Issuer RWA Disclosure\n"
                    "Issuer A disclosed 35% growth across 12 jurisdictions in 2026. "
                    "The disclosure describes custody, transfer controls, and reporting obligations."
                ),
            }
        )
        await request.on_tool_call(
            {
                "tool_name": "send_message_to_agent",
                "status": "done",
                "args": {"message": "not allowed"},
                "result": "delegated",
            }
        )
        return type("Result", (), {"content": "Worker digest with one fetched source.", "tokens_used": 7})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("issuer evidence", request=ResearchRequest(question="research issuer evidence"))

    assert callback_holder["on_tool_call"] is not None
    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.url == "https://issuer.example/rwa"
    assert source.title == "Issuer RWA Disclosure"
    assert source.publisher == "issuer.example"
    assert source.fetch_tool == "web_fetch"
    assert "35% growth" in source.content
