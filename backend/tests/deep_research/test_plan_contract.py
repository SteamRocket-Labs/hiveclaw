from __future__ import annotations

import json

import pytest


def test_plan_fill_contains_runtime_native_contract_for_deep_research():
    from app.services.deep_research.plan_mode import build_deep_research_plan_fill
    from app.services.deep_research.plan_contract import (
        research_plan_from_contract,
        validate_runtime_contract,
    )
    from app.services.deep_research.schemas import ResearchRequest

    request = ResearchRequest(
        question="Evaluate the RWA launchpad opportunity.",
        depth="full",
        max_sources=30,
        output_format="xlsx",
        output_language="zh",
        worker_topics=["confirmed official lane", "confirmed market lane"],
    )
    preview = {
        "worker_topics": ["confirmed official lane", "confirmed market lane"],
        "clarifying_questions": ["确认受众和用途"],
        "plan": {
            "lanes": [
                {
                    "lane_id": "official",
                    "label": "Official evidence",
                    "goal": "Verify primary issuer claims.",
                    "queries": [{"query": "RWA launchpad official documentation"}],
                    "preferred_source_types": ["primary", "technical"],
                },
                {
                    "lane_id": "market",
                    "label": "Market evidence",
                    "goal": "Quantify adoption and market size.",
                    "queries": [{"query": "RWA launchpad market data"}],
                    "preferred_source_types": ["dataset", "secondary"],
                },
            ]
        },
    }

    fill = build_deep_research_plan_fill(request, preview)
    contract = fill["deep_research"]["runtime_contract"]

    validate_runtime_contract(contract)
    assert contract["schema"] == "deep_research_runtime_contract.v1"
    assert contract["output"]["requested_formats"] == ["xlsx"]
    assert contract["output"]["format_briefs"]["xlsx"]["purpose"] == "evidence workbook"
    assert contract["research"]["lanes"][0]["worker_topic"] == "confirmed official lane"

    plan = research_plan_from_contract(contract)
    assert [lane.lane_id for lane in plan.lanes] == ["official", "market"]
    assert plan.lanes[0].queries[0].query == "RWA launchpad official documentation"


def test_research_request_accepts_approved_plan_contract_from_tool_arguments():
    from app.services.deep_research.schemas import ResearchRequest

    contract = {
        "schema": "deep_research_runtime_contract.v1",
        "research": {"lanes": [{"id": "official", "worker_topic": "Official evidence"}]},
        "output": {"requested_formats": ["pptx"], "primary_format": "pptx"},
    }

    request = ResearchRequest.from_arguments(
        {
            "question": "Research RWA custody.",
            "plan_confirmed": True,
            "approved_plan": json.dumps(contract),
        }
    )

    assert request.approved_plan["schema"] == "deep_research_runtime_contract.v1"
    assert request.approved_plan["output"]["requested_formats"] == ["pptx"]
    assert request.output_format == "pptx"
    assert request.worker_topics == ["Official evidence"]


@pytest.mark.asyncio
async def test_orchestrator_hydrates_confirmed_contract_without_refining_plan(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest, SourceRecord, SourceType, WorkerResult

    class ContractReasoner:
        async def refine_plan(self, *_args, **_kwargs):
            raise AssertionError("confirmed runtime contract must not be refined")

        async def synthesize_from_digests(self, request, plan, ledger, evaluation, *, worker_results, **_kwargs):
            ids = list(ledger.sources)
            second = ids[1] if len(ids) > 1 else ids[0]
            return f"""# Contract-Bound Report

## Executive Thesis

The confirmed contract was followed. Evidence from 2026 shows 35% growth, 12 jurisdictions,
18 controls, and 7 reporting checkpoints. This supports a moderate-confidence thesis with
explicit limits and a clear so-what for the user. Sources: {ids[0]}.

## Method And Source Standard

The report uses the confirmed runtime contract and source ledger. Sources: {ids[0]}, {second}.

## Cross-Cutting Analysis

The warrant is that the primary source ties the adoption claim to concrete controls, not merely
marketing language, while regulator guidance verifies reporting checkpoints. Sources: {ids[0]}, {second}.

## Key Findings

### Official evidence
The official lane produced 35% growth and 18 controls across 12 jurisdictions. Sources: {ids[0]}, {second}.

## Contradictions And Gaps

The main gap is independent verification. Sources: {ids[0]}, {second}.

## Strategic Implications

The user should treat the opportunity as promising but diligence-heavy. Sources: {ids[0]}, {second}.

## Source Ledger

- `{ids[0]}` official source
- `{second}` regulator source
"""

    class Runner:
        def __init__(self):
            self.topics: list[str] = []

        async def run(self, topic, *, request, cancel_event=None):
            self.topics.append(topic)
            return WorkerResult(
                topic=topic,
                intermediate_report="Digest with 35% growth, 12 jurisdictions, 18 controls, 7 checkpoints.",
                sources=[
                    SourceRecord(
                        source_id="src_contract1",
                        url="https://example.com/official",
                        title="Official source",
                        publisher="example.com",
                        source_type=SourceType.PRIMARY,
                        content="Official source says 35% growth, 12 jurisdictions, 18 controls, 7 checkpoints.",
                        fetch_tool="web_fetch",
                    ),
                    SourceRecord(
                        source_id="src_contract2",
                        url="https://regulator.example/guidance",
                        title="Regulator source",
                        publisher="regulator.example",
                        source_type=SourceType.REGULATORY,
                        content="Regulator guidance confirms 7 reporting checkpoints and 12 jurisdictions in 2026.",
                        fetch_tool="web_fetch",
                    )
                ],
                status="ok",
            )

    contract = {
        "schema": "deep_research_runtime_contract.v1",
        "question": "Research RWA custody.",
        "mode": "source_ledger_audit",
        "scope": {"in_scope": ["custody"], "out_of_scope": []},
        "research": {
            "source_policy": "primary_preferred",
            "time_window": "2026",
            "lanes": [
                {
                    "id": "official",
                    "label": "Official evidence",
                    "goal": "Verify primary claims.",
                    "worker_topic": "confirmed official contract lane",
                    "queries": [{"query": "RWA custody official evidence", "rationale": "primary evidence"}],
                    "preferred_source_types": ["primary"],
                }
            ],
        },
        "output": {"requested_formats": ["markdown"], "language": "en"},
    }
    runner = Runner()

    async def no_linear(*_args, **_kwargs):
        raise AssertionError("worker path should be used")

    result = await DeepResearchOrchestrator(no_linear, reasoner=ContractReasoner(), worker_runner=runner).run(
        ResearchRequest(
            question="Research RWA custody.",
            mode="source_ledger_audit",
            depth="quick",
            plan_confirmed=True,
            approved_plan=contract,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "completed"
    assert runner.topics == ["confirmed official contract lane"]
