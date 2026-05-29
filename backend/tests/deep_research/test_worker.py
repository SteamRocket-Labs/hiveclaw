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
        return type(
            "Result", (), {"content": "## Worker digest\n\nFinding A with citation [src_pending].", "tokens_used": 321}
        )()

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


@pytest.mark.asyncio
async def test_research_worker_drops_unparsed_pdf_and_binary_sources():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import RuntimeResearchWorker

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "status": "done",
                "args": {"url": "https://issuer.example/report.pdf"},
                # Unparsed PDF leaked through as text: %PDF magic + FlateDecode binary stream.
                "result": "%PDF-1.4\n%\xe2\xe3\xcf\xd3\n4 0 obj<</Length 999/Filter/FlateDecode>>stream\n"
                + ("\x00\x01\x02\x03\x04\x05\x06\x07\x08" * 40),
            }
        )
        return type("Result", (), {"content": "digest", "tokens_used": 5})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("pdf lane", request=ResearchRequest(question="research pdf lane"))

    assert result.sources == [], "unparsed PDF / binary payloads must not be captured as sources"


@pytest.mark.asyncio
async def test_research_worker_caps_source_content_length():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import RuntimeResearchWorker

    huge_body = "Issuer A disclosed 35% growth across 12 jurisdictions in 2026. " * 2000

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "status": "done",
                "args": {"url": "https://issuer.example/huge"},
                "result": f"Title: Huge Page\n{huge_body}",
            }
        )
        return type("Result", (), {"content": "digest", "tokens_used": 5})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("huge lane", request=ResearchRequest(question="research huge lane"))

    assert len(result.sources) == 1
    assert len(result.sources[0].content) <= 12000, "worker must cap captured source content to bound token spend"


@pytest.mark.asyncio
async def test_research_worker_caps_number_of_captured_sources():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import RuntimeResearchWorker

    async def fake_invoke(request):
        for i in range(20):
            await request.on_tool_call(
                {
                    "tool_name": "web_fetch",
                    "status": "done",
                    "args": {"url": f"https://issuer.example/source-{i}"},
                    "result": (
                        f"Title: Source {i}\n"
                        f"Issuer A disclosed 35% growth across 12 jurisdictions in 2026 for item {i}. "
                        "Custody, transfer controls, and reporting obligations apply."
                    ),
                }
            )
        return type("Result", (), {"content": "digest", "tokens_used": 9})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("flood lane", request=ResearchRequest(question="research flood lane"))

    assert len(result.sources) <= 8, "a single worker must not hoard sources (production: worker #3 grabbed 18)"


@pytest.mark.asyncio
async def test_research_worker_infers_source_type_from_authoritative_url():
    from app.services.deep_research.schemas import ResearchRequest, SourceType
    from app.services.deep_research.worker import RuntimeResearchWorker

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "status": "done",
                "args": {"url": "https://www.sec.gov/rules/final/2026/rwa.htm"},
                "result": (
                    "Title: SEC RWA Disclosure Rule\n"
                    "The Commission adopted disclosure rules for tokenized assets in 2026 covering custody, "
                    "transfer controls, and reporting obligations across 12 jurisdictions."
                ),
            }
        )
        return type("Result", (), {"content": "digest", "tokens_used": 3})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("regulator lane", request=ResearchRequest(question="q"))

    assert len(result.sources) == 1
    assert result.sources[0].source_type in {
        SourceType.REGULATORY,
        SourceType.PRIMARY,
    }, "a .gov source must not stay UNKNOWN (all-tier3 production bug)"


@pytest.mark.asyncio
async def test_research_worker_title_skips_envelope_and_page_noise():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import RuntimeResearchWorker

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "tool_name": "web_fetch",
                "status": "done",
                "args": {"url": "https://issuer.example/report.pdf"},
                # web_fetch envelope + extracted-PDF page marker precede the real title.
                "result": (
                    "📄 **Fetched content from: https://issuer.example/report.pdf**\n\n"
                    "--- 第1页 ---\n"
                    "RWA Custody Report 2026\n"
                    "Issuer A disclosed 35% growth across 12 jurisdictions with custody and reporting controls."
                ),
            }
        )
        return type("Result", (), {"content": "digest", "tokens_used": 3})()

    worker = RuntimeResearchWorker(
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model=_StubModel(),
        fallback_model=None,
        agent=_StubAgent(),
        invoke=fake_invoke,
    )

    result = await worker.run("issuer lane", request=ResearchRequest(question="q"))

    assert len(result.sources) == 1
    title = result.sources[0].title
    assert "%PDF" not in title and "📄" not in title and not title.startswith("---")
    assert "RWA Custody Report" in title
