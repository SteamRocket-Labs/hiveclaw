from __future__ import annotations

import inspect
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


async def _fake_resolve_models(self):
    return _StubModel(), None, _StubAgent()


@pytest.mark.asyncio
async def test_runtime_reasoner_invokes_agent_with_tools_disabled(monkeypatch):
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner

    captured = {}

    async def fake_invoke_agent(request):
        captured["initial_tools"] = request.initial_tools
        captured["expand_tools"] = request.expand_tools
        captured["max_tool_rounds"] = request.max_tool_rounds
        captured["source"] = request.session_context.source
        return type("Result", (), {"content": '{"lanes":[]}'})()

    monkeypatch.setattr(RuntimeDeepResearchReasoner, "_resolve_models", _fake_resolve_models)
    monkeypatch.setattr("app.services.deep_research.reasoner.invoke_agent", fake_invoke_agent)

    reasoner = RuntimeDeepResearchReasoner(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    await reasoner._invoke("plan", "return json")

    assert captured == {
        "initial_tools": [],
        "expand_tools": False,
        "max_tool_rounds": 1,
        "source": "deep_research",
    }


def test_build_system_prompt_suffix_includes_universal_persona():
    """T2-4: every internal reasoning pass carries the expert-researcher persona."""
    from app.services.deep_research.reasoner import _build_system_prompt_suffix

    suffix = _build_system_prompt_suffix(mode=None)
    assert "EXPERT RESEARCH PERSONA" in suffix
    assert "highly experienced analyst" in suffix
    assert "Tools are disabled" in suffix


def test_build_system_prompt_suffix_adds_mode_specific_role():
    """T2-3: mode-specific persona is appended on top of the universal one."""
    from app.services.deep_research.reasoner import _build_system_prompt_suffix

    industry = _build_system_prompt_suffix(mode="industry_research")
    audit = _build_system_prompt_suffix(mode="source_ledger_audit")
    deep_dive = _build_system_prompt_suffix(mode="topic_deep_dive")

    assert "market analyst" in industry.lower()
    assert "auditor" in audit.lower() or "fact-checker" in audit.lower()
    assert "topic-deep-dive" in deep_dive.lower() or "topic deep dive" in deep_dive.lower()


def test_build_system_prompt_suffix_stacks_role_persona_on_top_of_mode():
    """T3-2: role persona is appended after mode persona, before the tools-disabled tail."""
    from app.services.deep_research.reasoner import _build_system_prompt_suffix

    planner = _build_system_prompt_suffix(mode="industry_research", role="planner")
    researcher = _build_system_prompt_suffix(mode="industry_research", role="researcher")
    critic = _build_system_prompt_suffix(mode="industry_research", role="critic")
    writer_persona = _build_system_prompt_suffix(mode="industry_research", role="writer")

    assert "Planner" in planner
    assert "Researcher" in researcher
    assert "Critic" in critic
    assert "Writer" in writer_persona
    # Mode persona is still present
    for prompt in (planner, researcher, critic, writer_persona):
        assert "market analyst" in prompt.lower()
        assert "Tools are disabled" in prompt


def test_persona_for_role_returns_empty_for_unknown_role():
    """T3-2: unknown role yields empty persona (no error)."""
    from app.services.deep_research.reasoner import _persona_for_role

    assert _persona_for_role("unknown") == ""
    assert _persona_for_role(None) == ""
    assert "Planner" in _persona_for_role("planner")


@pytest.mark.asyncio
async def test_decide_controller_action_invokes_planner_persona(monkeypatch, tmp_path):
    """T3-2 + T3-1: reasoner.decide_controller_action calls _invoke with role=planner."""
    from app.services.deep_research.planner import build_research_plan
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner
    from app.services.deep_research.schemas import ResearchRequest

    captured: dict[str, object] = {}

    async def fake_invoke_agent(request):
        captured["suffix"] = request.system_prompt_suffix
        captured["metadata"] = request.session_context.metadata
        captured["content"] = request.messages[0]["content"]
        return type("Result", (), {"content": '{"type":"search","queries":["foo"],"rationale":"go"}'})()

    monkeypatch.setattr(RuntimeDeepResearchReasoner, "_resolve_models", _fake_resolve_models)
    monkeypatch.setattr("app.services.deep_research.reasoner.invoke_agent", fake_invoke_agent)

    reasoner = RuntimeDeepResearchReasoner(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    request = ResearchRequest(question="research X", mode="industry_research", max_sources=4)
    plan = build_research_plan(request)

    decision = await reasoner.decide_controller_action(
        request=request,
        plan=plan,
        step_index=2,
        token_used=2500,
        token_budget=10000,
        current_question="What is the regulator stance?",
        open_gaps=["regulator stance"],
        ledger_summary={"source_count": 1, "claim_count": 1, "lanes_covered": ["regulatory"]},
        source_notes=[],
    )

    assert decision == {"type": "search", "queries": ["foo"], "rationale": "go"}
    suffix = captured.get("suffix") or ""
    assert "Planner" in suffix, "Planner persona must be present in system_prompt_suffix"
    assert "market analyst" in suffix.lower(), "Industry mode persona must still be stacked"
    metadata = captured.get("metadata") or {}
    assert metadata.get("role") == "planner"


def test_sections_for_mode_picks_mode_specific_template():
    """T2-3: each mode has its own section template."""
    from app.services.deep_research.reasoner import _sections_for_mode

    industry = _sections_for_mode("industry_research")
    audit = _sections_for_mode("source_ledger_audit")
    deep_dive = _sections_for_mode("topic_deep_dive")
    default = _sections_for_mode("unknown_mode")

    assert "Market Map" in industry
    assert "Claim Audit Table" in audit
    assert "Mechanism And Workflow" in deep_dive
    assert default == industry  # unknown falls back to industry default


def test_reasoner_exposes_summarize_source():
    """T1-1: RuntimeDeepResearchReasoner must expose summarize_source so per-source
    structured notes (key_entities, key_numbers, key_dates, mechanisms, limitations,
    source_bound_summary) can be extracted before synthesis."""
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner

    assert hasattr(RuntimeDeepResearchReasoner, "summarize_source"), (
        "Tier 1-1 requires RuntimeDeepResearchReasoner.summarize_source for source_notes extraction"
    )


def test_reasoner_synthesize_report_accepts_structured_notes_kwargs():
    """T1-1: synthesize_report must accept source_notes and lane_summaries kwargs so
    the synthesis LLM gets per-source facts and per-lane evidence, not just 1.8K excerpts.
    Asserted at the signature level so the contract failure is unambiguous."""
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner

    sig = inspect.signature(RuntimeDeepResearchReasoner.synthesize_report)
    assert "source_notes" in sig.parameters, (
        "Tier 1-1 requires synthesize_report to accept a source_notes parameter"
    )
    assert "lane_summaries" in sig.parameters, (
        "Tier 1-1 requires synthesize_report to accept a lane_summaries parameter"
    )


@pytest.mark.asyncio
async def test_reasoner_synthesize_report_payload_serializes_structured_notes(monkeypatch, tmp_path):
    """T1-1: when source_notes/lane_summaries are supplied, they must appear in the
    JSON payload handed to invoke_agent (not silently dropped)."""
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner
    from app.services.deep_research.schemas import EvaluationResult, ResearchPlan, ResearchRequest

    sig = inspect.signature(RuntimeDeepResearchReasoner.synthesize_report)
    if "source_notes" not in sig.parameters or "lane_summaries" not in sig.parameters:
        pytest.fail(
            "Tier 1-1 contract: synthesize_report must accept source_notes and lane_summaries kwargs"
        )

    captured: dict[str, str] = {}

    async def fake_invoke_agent(request):
        captured["content"] = request.messages[0]["content"]
        return type("Result", (), {"content": "# Final\n\n## Executive\n\nbody"})()

    monkeypatch.setattr(RuntimeDeepResearchReasoner, "_resolve_models", _fake_resolve_models)
    monkeypatch.setattr("app.services.deep_research.reasoner.invoke_agent", fake_invoke_agent)

    reasoner = RuntimeDeepResearchReasoner(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    ledger = EvidenceLedger(tmp_path)
    plan = ResearchPlan(question="q", mode="topic_deep_dive", lanes=[])
    evaluation = EvaluationResult(quality_gates={"attribution": "passed"})

    kwargs = {
        "source_notes": [
            {
                "source_id": "src_aaaaaaaaaaaa",
                "key_entities": ["Issuer A", "Regulator B"],
                "key_numbers": ["35% growth"],
                "source_bound_summary": "Issuer A reports a 35% growth in 2026.",
            }
        ],
        "lane_summaries": [
            {
                "lane_id": "primary",
                "covered_questions": ["adoption signals"],
                "evidence_strength": "high",
                "key_findings": ["growth pattern confirmed"],
            }
        ],
    }
    await reasoner.synthesize_report(
        ResearchRequest(question="q"),
        plan,
        ledger,
        evaluation,
        **kwargs,
    )

    content = captured.get("content", "")
    assert "source_notes" in content
    assert "lane_summaries" in content
    assert "src_aaaaaaaaaaaa" in content
    assert "evidence_strength" in content


@pytest.mark.asyncio
async def test_reasoner_synthesize_from_digests_uses_worker_reports_not_raw_source_text(monkeypatch, tmp_path):
    """V2 contract: final synthesis consumes compressed worker digests and source metadata.
    It must not pass 8K raw source excerpts back into the writer call."""
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.planner import build_research_plan
    from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner
    from app.services.deep_research.schemas import EvaluationResult, ResearchRequest, SourceRecord, SourceType, WorkerResult

    captured: dict[str, str] = {}

    async def fake_invoke_agent(request):
        captured["content"] = request.messages[0]["content"]
        captured["suffix"] = request.system_prompt_suffix
        return type("Result", (), {"content": "# Final\n\n## Executive Thesis\n\nbody"})()

    monkeypatch.setattr(RuntimeDeepResearchReasoner, "_resolve_models", _fake_resolve_models)
    monkeypatch.setattr("app.services.deep_research.reasoner.invoke_agent", fake_invoke_agent)

    request = ResearchRequest(question="q", mode="topic_deep_dive")
    plan = build_research_plan(request)
    ledger = EvidenceLedger(tmp_path)
    ledger.add_source(
        url="https://issuer.example/rwa",
        title="Issuer Disclosure",
        publisher="issuer.example",
        source_type=SourceType.PRIMARY,
        content="RAW_SOURCE_TEXT_SHOULD_NOT_BE_IN_FINAL_SYNTHESIS_PAYLOAD " * 400,
        lane_id="official",
    )
    worker_source = SourceRecord(
        source_id="src_worker",
        url="https://worker.example/rwa",
        title="Worker Source",
        publisher="worker.example",
        source_type=SourceType.SECONDARY,
        content="WORKER_RAW_SOURCE_TEXT_SHOULD_NOT_BE_IN_FINAL_SYNTHESIS_PAYLOAD " * 400,
        lane_id="market",
        fetch_tool="web_fetch",
    )
    worker_results = [
        WorkerResult(
            topic="market map",
            intermediate_report="Worker found 35% growth and 12 jurisdiction constraints with cited evidence.",
            sources=[worker_source],
            status="ok",
            tokens_used=77,
        )
    ]

    reasoner = RuntimeDeepResearchReasoner(agent_id=uuid.uuid4(), user_id=uuid.uuid4())
    await reasoner.synthesize_from_digests(
        request,
        plan,
        ledger,
        EvaluationResult(quality_gates={"attribution": "passed"}),
        worker_results=worker_results,
    )

    content = captured.get("content", "")
    assert "worker_digests" in content
    assert "intermediate_report" in content
    assert "Worker found 35% growth" in content
    assert "RAW_SOURCE_TEXT_SHOULD_NOT_BE_IN_FINAL_SYNTHESIS_PAYLOAD" not in content
    assert "WORKER_RAW_SOURCE_TEXT_SHOULD_NOT_BE_IN_FINAL_SYNTHESIS_PAYLOAD" not in content
    assert "Writer" in captured.get("suffix", "")
