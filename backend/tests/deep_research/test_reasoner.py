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
