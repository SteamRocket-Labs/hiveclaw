from __future__ import annotations

import json

import pytest


class _MinimalReasoner:
    """Smallest viable reasoner: returns an analyst-grade report that passes the Tier 1-5
    gate (numbers + named entities + non-dump structure).

    Tier 1-2 removed the Python string-concat fallback, so orchestrator tests that want
    status=completed must supply a reasoner that yields analyst-grade output."""

    async def synthesize_report(self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None):
        ids = list(ledger.sources)
        if len(ids) < 2:
            ids = ids + ["src_pad_a", "src_pad_b"]
        return f"""# RWA Adoption Brief

## Executive Thesis

In 2026, tokenized treasury products from BlackRock BUIDL, Securitize, Ondo Finance,
and JPMorgan Onyx captured a measurable share of institutional RWA exposure. The four
issuers report combined AUM of $1.24B across 14 tokenized treasury vehicles, backed by
SEC, MAS, and SFC disclosures filed in Q2 2026. Sources: {ids[0]}, {ids[1]}.

## Method And Source Standard

Primary filings from BlackRock, Securitize, Ondo Finance, and JPMorgan were prioritised
over secondary analyst commentary from Bernstein and Bloomberg.

## Market Map

| Lane | Players | Evidence |
|---|---|---|
| Issuance | BlackRock, Securitize, Ondo Finance | {ids[0]} |
| Regulatory | SEC, MAS, SFC | {ids[1]} |

## Key Findings

- Tokenized treasuries remain the clearest RWA category with $1.24B traceable AUM
  and 14 distinct vehicles across BlackRock BUIDL and Securitize in 2026.
  Sources: {ids[0]}, {ids[1]}.
- Custody handled by State Street and BNY Mellon remains the binding constraint;
  18 of the top 20 institutional buyers cite custody concerns.

## Strategic Implications

- Bundle issuance, compliance, and reporting into one workflow.
- Treat controlled secondary liquidity via Republic Forge or CartaX as the moat.

## Contradictions And Gaps

- Hong Kong SFC vs Singapore MAS transfer-restriction rules diverge for 5 product types.

## Source Ledger

- `{ids[0]}` Issuer A 2026 disclosure
- `{ids[1]}` Regulator B Q2 2026 filing
"""


@pytest.mark.asyncio
async def test_orchestrator_writes_source_claim_report_artifacts(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://issuer.example/rwa\nhttps://regulator.example/rwa"
        if tool_name == "web_fetch":
            url = arguments["url"]
            return (
                f"Title: {url}\n"
                "Tokenized treasury products are a visible RWA adoption lane in 2026. "
                "Issuers still face custody, liquidity, and regulatory disclosure risks."
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    result = await DeepResearchOrchestrator(fake_tool, reasoner=_MinimalReasoner()).run(
        ResearchRequest(
            question="Do a deep research brief on RWA adoption.",
            mode="topic_deep_dive",
            max_rounds=1,
            max_sources=2,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "completed"
    assert (tmp_path / "sources.jsonl").is_file()
    assert (tmp_path / "claims.jsonl").is_file()
    assert (tmp_path / "steps.jsonl").is_file()
    assert (tmp_path / "report.md").is_file()
    assert (tmp_path / "final.json").is_file()

    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))
    assert final["quality_gates"]["attribution"] == "passed"
    assert final["source_count"] == 2
    assert final["claim_count"] >= 1
    assert "unsupported" not in {claim["status"] for claim in final["claims"]}


@pytest.mark.asyncio
async def test_orchestrator_returns_partial_report_and_gaps_when_sources_fail(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://blocked.example/rwa"
        return ""

    result = await DeepResearchOrchestrator(fake_tool).run(
        ResearchRequest(
            question="Research blocked RWA source.",
            mode="topic_deep_dive",
            max_rounds=1,
            max_sources=1,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "failed"
    assert result.gaps
    report_text = (tmp_path / "report.md").read_text(encoding="utf-8")
    # Tier 1-2: failed runs write a failure notice, never a pasted evidence-list dump
    assert "Synthesis Failed" in report_text
    assert "Source-Grounded Findings" not in report_text
    assert json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))["gaps"]


@pytest.mark.asyncio
async def test_orchestrator_marks_run_failed_when_reasoner_synthesis_raises(tmp_path):
    """Tier 1-2: reasoner.synthesize_report failure must not be papered over by the
    Python string-concat fallback. Run ends with status=failed and a short failure
    notice in report.md; source/claim artifacts still persist for diagnosis."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    class _FailingReasoner:
        async def refine_plan(self, request, plan):
            return plan

        async def extract_claims(self, request, source):
            return [
                {
                    "text": f"{source.publisher} reports a structural adoption shift.",
                    "status": "verified",
                    "source_ids": [source.source_id],
                    "evidence": source.content[:200],
                }
            ]

        async def synthesize_report(self, request, plan, ledger, evaluation):
            raise RuntimeError("simulated LLM outage")

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://a.example/x\nhttps://b.example/y"
        if tool_name == "web_fetch":
            url = arguments["url"]
            return (
                f"Title: Evidence for {url}\n"
                "Detailed tokenized issuance discussion with concrete adoption signals "
                "and disclosure workflow considerations."
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    result = await DeepResearchOrchestrator(fake_tool, reasoner=_FailingReasoner()).run(
        ResearchRequest(
            question="test research",
            mode="topic_deep_dive",
            depth="standard",
            max_rounds=1,
            max_sources=2,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "failed"
    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))
    assert final["status"] == "failed"
    assert final["source_count"] >= 1, "Source artifacts must survive synthesis failure"
    assert (tmp_path / "sources.jsonl").is_file()
    assert (tmp_path / "claims.jsonl").is_file()

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert (
        "not a completed" in report.lower() or "synthesis failed" in report.lower()
    ), f"Failed run report.md must be a failure notice, got: {report[:300]}"
    # And must not be the pasted fallback evidence dump
    assert "Source-Grounded Findings" not in report
    assert "Evidence coverage spans" not in report
    # Gaps must explain the synthesis failure
    assert any("synth" in gap.lower() for gap in final["gaps"])


@pytest.mark.asyncio
async def test_orchestrator_persists_source_notes_and_lane_summaries(tmp_path):
    """Tier 1-1: source_notes.jsonl and lane_summaries.jsonl must be written so the
    synthesis stage can read structured per-source and per-lane evidence."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    class _NotesReasoner:
        async def refine_plan(self, request, plan):
            return plan

        async def summarize_source(self, request, source):
            return {
                "source_id": source.source_id,
                "relevance_score": 0.8,
                "credibility_score": 0.7,
                "key_entities": ["Issuer A", "Regulator B"],
                "key_numbers": ["35% growth"],
                "key_dates": ["2026 Q2"],
                "mechanisms": ["transfer restrictions"],
                "limitations": ["jurisdiction limited to US"],
                "source_bound_summary": f"{source.publisher} describes issuance workflow.",
            }

        async def extract_claims(self, request, source):
            return [
                {
                    "text": f"{source.publisher} confirms a structural shift in issuance.",
                    "status": "verified",
                    "source_ids": [source.source_id],
                    "evidence": source.content[:200],
                }
            ]

        async def synthesize_report(self, request, plan, ledger, evaluation):
            ids = list(ledger.sources)
            return (
                "# Brief\n\n## Executive Thesis\n\nThe issuer market grew 35% across 12 deals "
                "in 2026 backed by Issuer A and Regulator B disclosures.\n\n"
                "## Key Findings\n\nIssuer A and Regulator B disclosed the framework.\n\n"
                f"## Source Ledger\n\n- `{ids[0]}` source A"
            )

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://a.example/x"
        if tool_name == "web_fetch":
            return (
                "Title: Issuer A 2026 disclosure\n"
                "Issuer A discloses a 35% growth across 12 jurisdictions in 2026, "
                "driven by tokenized issuance workflows and controlled secondary venues. "
                "Regulator B reviewed 17 filings under the relevant exemption."
            )
        raise AssertionError(tool_name)

    await DeepResearchOrchestrator(fake_tool, reasoner=_NotesReasoner()).run(
        ResearchRequest(
            question="test",
            mode="topic_deep_dive",
            max_rounds=1,
            max_sources=1,
        ),
        artifact_dir=tmp_path,
    )

    notes_path = tmp_path / "source_notes.jsonl"
    summaries_path = tmp_path / "lane_summaries.jsonl"
    assert notes_path.is_file(), "Tier 1-1 must write source_notes.jsonl"
    assert summaries_path.is_file(), "Tier 1-1 must write lane_summaries.jsonl"

    note_lines = [line for line in notes_path.read_text("utf-8").splitlines() if line.strip()]
    assert note_lines, "source_notes.jsonl must contain at least one record"
    note = json.loads(note_lines[0])
    assert note.get("source_id")
    assert "key_entities" in note

    summary_lines = [line for line in summaries_path.read_text("utf-8").splitlines() if line.strip()]
    assert summary_lines, "lane_summaries.jsonl must contain at least one record"


@pytest.mark.asyncio
async def test_orchestrator_does_not_complete_when_quality_gate_fails(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://issuer.example/rwa"
        if tool_name == "web_fetch":
            return (
                "Tokenized treasury products are a visible RWA adoption lane in 2026. "
                "Issuers still face custody, liquidity, and regulatory disclosure risks."
            )
        return ""

    result = await DeepResearchOrchestrator(fake_tool).run(
        ResearchRequest(
            question="Do a broad deep research brief on RWA adoption.",
            mode="industry_research",
            depth="full",
            max_rounds=1,
            max_sources=8,
        ),
        artifact_dir=tmp_path,
    )

    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))

    assert result.status == "failed"
    assert final["quality_gates"]["plurality"] == "failed"
    assert final["quality_gates"]["completeness"] == "failed"
    assert final["gaps"]
