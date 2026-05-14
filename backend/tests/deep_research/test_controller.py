"""Tier 3-1 DeepResearchController tests.

The controller is opt-in via ResearchRequest.controller_mode. These tests verify:
- orchestrator dispatches to controller when controller_mode is set
- controller persists controller_trace.jsonl with one entry per step
- LLM-decided actions (search/visit/reflect/answer) drive state machine
- Beast mode triggers when token_used >= 85% of budget
- Fallback (no reasoner / failing reasoner) uses deterministic action picker
"""

from __future__ import annotations

import json

import pytest


def _stub_tool_factory(web_fetch_body: str | None = None):
    body = web_fetch_body or (
        "Title: Issuer A 2026 disclosure\n"
        "Issuer A reports $1.24B AUM across 14 tokenized treasury vehicles in 2026, "
        "backed by SEC Reg D and MAS Securities Act disclosures filed in Q2 2026. "
        "Custody is provided by State Street and BNY Mellon."
    )

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            # Two independent hosts so the plurality gate clears.
            return "https://issuer.example/rwa\nhttps://regulator.example/rwa-rules"
        if tool_name == "web_fetch":
            return body
        raise AssertionError(f"unexpected tool: {tool_name}")

    return fake_tool


class _AnalystReasoner:
    """Tier 1/2-compliant reasoner used for controller end-to-end tests."""

    async def extract_claims(self, request, source):
        return [
            {
                "text": f"{source.publisher} confirms tokenized treasury growth of 35% YoY.",
                "status": "verified",
                "source_ids": [source.source_id],
                "evidence": source.content[:200],
            }
        ]

    async def summarize_source(self, request, source):
        return {
            "source_id": source.source_id,
            "relevance_score": 0.85,
            "credibility_score": 0.8,
            "key_entities": ["BlackRock", "Securitize", "SEC", "MAS"],
            "key_numbers": ["$1.24B AUM", "35% YoY", "14 vehicles"],
            "key_dates": ["Q2 2026"],
            "mechanisms": ["transfer restrictions", "Reg D 506(c)"],
            "limitations": ["US-centric"],
            "source_bound_summary": f"{source.publisher} grew tokenized treasury issuance to $1.24B.",
        }

    async def synthesize_report(
        self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None
    ):
        ids = list(ledger.sources)
        first = ids[0]
        return f"""# RWA Treasury Adoption Brief

## Executive Thesis

In 2026 BlackRock BUIDL, Securitize, Ondo Finance, and JPMorgan Onyx captured combined
AUM of $1.24B across 14 tokenized treasury vehicles, backed by SEC Reg D, MAS Securities
Act, and SFC Type 1 disclosures filed in Q2 2026. Sources: [{first}].

## Method And Source Standard

Primary issuer filings from BlackRock, Securitize, Ondo Finance, and JPMorgan were
prioritised over secondary analyst commentary from Bernstein and Bloomberg.

## Market Map

| Lane | Players | Evidence |
|---|---|---|
| Issuance | BlackRock BUIDL, Securitize, Ondo Finance, JPMorgan Onyx | [{first}] |
| Regulatory | SEC, MAS, SFC, FINRA | [{first}] |
| Custody | State Street, BNY Mellon | [{first}] |

## Key Findings

- Tokenized treasuries grew 35% YoY to $1.24B AUM across 14 vehicles via BlackRock BUIDL
  and Securitize, with SEC Reg D coverage in 17 filings. Sources: [{first}].
- Custody handled by State Street and BNY Mellon remains the binding constraint; 18 of the
  top 20 institutional buyers cite custody concerns.

## Strategic Implications

- Bundle issuance, compliance, and reporting into one workflow via Republic Forge or CartaX.

## Contradictions And Gaps

- Hong Kong SFC vs Singapore MAS transfer-restriction rules diverge for 5 product types.

## Source Ledger

- `{first}` Issuer A 2026 disclosure
"""


@pytest.mark.asyncio
async def test_orchestrator_dispatches_to_controller_when_controller_mode_set(tmp_path):
    """T3-1: ResearchRequest.controller_mode=True routes the run through DeepResearchController."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    result = await DeepResearchOrchestrator(
        _stub_tool_factory(), reasoner=_AnalystReasoner()
    ).run(
        ResearchRequest(
            question="Research RWA treasury adoption.",
            mode="industry_research",
            depth="standard",
            max_rounds=2,
            max_sources=2,
            controller_mode=True,
        ),
        artifact_dir=tmp_path,
    )

    assert (tmp_path / "controller_trace.jsonl").is_file(), (
        "controller_trace.jsonl must exist when controller_mode is enabled"
    )
    # The non-controller path doesn't write controller_trace.jsonl, so this is the proof of dispatch.
    assert result.status in {"completed", "failed"}


@pytest.mark.asyncio
async def test_controller_records_one_trace_entry_per_step(tmp_path):
    """T3-1: every step the controller takes is persisted to controller_trace.jsonl."""
    from app.services.deep_research.controller import DeepResearchController
    from app.services.deep_research.schemas import ResearchRequest

    decisions = iter(
        [
            {"type": "search", "rationale": "kick off search", "queries": ["RWA tokenized treasury 2026"]},
            {"type": "reflect", "rationale": "check coverage"},
            {"type": "answer", "rationale": "good enough, synthesize"},
        ]
    )

    class _StepReasoner(_AnalystReasoner):
        async def decide_controller_action(self, *, request, plan, **payload):
            return next(decisions)

    result = await DeepResearchController(
        _stub_tool_factory(), reasoner=_StepReasoner()
    ).run(
        ResearchRequest(
            question="Research RWA treasury adoption.",
            mode="industry_research",
            depth="standard",
            max_rounds=2,
            max_sources=6,
            controller_mode=True,
        ),
        artifact_dir=tmp_path,
    )

    trace_lines = [
        line for line in (tmp_path / "controller_trace.jsonl").read_text("utf-8").splitlines() if line.strip()
    ]
    assert len(trace_lines) == 3, "controller must emit exactly one trace entry per step"

    parsed = [json.loads(line) for line in trace_lines]
    assert [entry["action_type"] for entry in parsed] == ["search", "reflect", "answer"]
    assert all("rationale" in entry for entry in parsed)
    assert all(entry["step_index"] == i + 1 for i, entry in enumerate(parsed))
    assert result.status == "completed", result.gaps


@pytest.mark.asyncio
async def test_controller_falls_back_to_deterministic_actions_when_reasoner_missing(tmp_path):
    """T3-1: with no reasoner, controller still completes a run using deterministic actions."""
    from app.services.deep_research.controller import DeepResearchController
    from app.services.deep_research.schemas import ResearchRequest

    result = await DeepResearchController(_stub_tool_factory()).run(
        ResearchRequest(
            question="Research RWA",
            mode="industry_research",
            depth="standard",
            max_rounds=2,
            max_sources=2,
            controller_mode=True,
        ),
        artifact_dir=tmp_path,
    )

    trace_lines = [
        line for line in (tmp_path / "controller_trace.jsonl").read_text("utf-8").splitlines() if line.strip()
    ]
    assert trace_lines, "controller must persist trace even with no reasoner"
    # No reasoner means synthesis fails and run is marked failed — but artifacts still exist.
    assert result.status == "failed"
    assert (tmp_path / "report.md").read_text("utf-8").startswith("# Deep Research — Synthesis Failed")


@pytest.mark.asyncio
async def test_controller_enters_beast_mode_when_token_budget_exhausted(tmp_path):
    """T3-1: when token_used crosses 85% of budget, the loop terminates with beast_mode."""
    from app.services.deep_research.controller import DeepResearchController
    from app.services.deep_research.schemas import ResearchRequest

    class _LoopingReasoner(_AnalystReasoner):
        async def decide_controller_action(self, *, request, plan, **payload):
            # Force a long sequence of low-yield reflect steps so the budget runs out.
            return {"type": "reflect", "rationale": "stretch the loop"}

    # token_budget=5000 → 85% threshold = 4250 → each reflect step costs ~750 → 6 steps trip it.
    result = await DeepResearchController(
        _stub_tool_factory(), reasoner=_LoopingReasoner()
    ).run(
        ResearchRequest(
            question="Research RWA",
            mode="industry_research",
            depth="standard",
            max_rounds=2,
            max_sources=2,
            token_budget=5000,
            controller_mode=True,
        ),
        artifact_dir=tmp_path,
    )

    trace_lines = [
        line for line in (tmp_path / "controller_trace.jsonl").read_text("utf-8").splitlines() if line.strip()
    ]
    assert trace_lines
    parsed = [json.loads(line) for line in trace_lines]
    action_types = [entry["action_type"] for entry in parsed]
    assert "beast_mode" in action_types, "tight budget must trigger beast_mode trace entry"
    assert result.status == "failed", (
        "beast mode with no fetched sources should still surface as failed synthesis"
    )


@pytest.mark.asyncio
async def test_controller_visit_action_dispatches_direct_url_fetch(tmp_path):
    """T3-1: visit action fetches the URLs the LLM names directly, bypassing web_search."""
    from app.services.deep_research.controller import DeepResearchController
    from app.services.deep_research.schemas import ResearchRequest

    direct_url = "https://issuer.example/rwa-direct"

    decisions = iter(
        [
            {"type": "visit", "rationale": "we already know the canonical URL", "urls": [direct_url]},
            {"type": "answer", "rationale": "synthesize"},
        ]
    )

    class _VisitReasoner(_AnalystReasoner):
        async def decide_controller_action(self, *, request, plan, **payload):
            return next(decisions)

    visited: list[str] = []

    async def stub_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return ""
        if tool_name in {"web_fetch", "firecrawl_fetch", "xcrawl_scrape"}:
            visited.append(arguments["url"])
            return (
                "Title: Issuer A 2026 disclosure\n"
                "Issuer A reports $1.24B AUM across 14 tokenized treasury vehicles in 2026, "
                "backed by SEC Reg D and MAS Securities Act disclosures filed in Q2 2026. "
                "Custody is provided by State Street and BNY Mellon."
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    result = await DeepResearchController(stub_tool, reasoner=_VisitReasoner()).run(
        ResearchRequest(
            question="Research RWA treasury adoption.",
            mode="industry_research",
            depth="standard",
            max_rounds=2,
            max_sources=2,
            controller_mode=True,
        ),
        artifact_dir=tmp_path,
    )

    assert direct_url in visited, "visit action must fetch the LLM-named URL"
    trace = [
        json.loads(line)
        for line in (tmp_path / "controller_trace.jsonl").read_text("utf-8").splitlines()
        if line.strip()
    ]
    assert trace[0]["action_type"] == "visit"
    assert trace[0]["role"] == "researcher"
    assert result.status in {"completed", "failed"}
