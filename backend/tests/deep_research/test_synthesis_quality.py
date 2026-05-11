from __future__ import annotations

import json

import pytest


class _AnalystReasoner:
    async def refine_plan(self, request, plan):
        plan.lanes[0].queries[0].query = "RWA pre-IPO launchpad market map Republic Forge CartaX Securitize"
        return plan

    async def extract_claims(self, request, source):
        return [
            {
                "text": f"{source.publisher} evidence shows a concrete RWA launchpad adoption or risk point.",
                "status": "verified",
                "source_ids": [source.source_id],
                "evidence": source.content[:220],
            }
        ]

    async def synthesize_report(self, request, plan, ledger, evaluation):
        source_ids = list(ledger.sources)
        return f"""# RWA Pre-IPO Launchpad Deep Research

## Executive Thesis

The investable opportunity is not generic RWA tokenization; it is the narrower ability to combine issuer diligence,
transfer restrictions, compliant distribution, and secondary liquidity into one launchpad workflow. Evidence from
{source_ids[0]} and {source_ids[1]} supports the market infrastructure requirement, while {source_ids[2]} and
{source_ids[3]} show the liquidity and compliance constraints that decide whether the product can scale.

## Market Map

| Segment | What matters | Evidence |
|---|---|---|
| Issuer onboarding | Verified asset ownership, disclosure package, cap-table or SPV mapping | {source_ids[0]} |
| Compliance rail | Investor qualification, transfer controls, jurisdiction policy | {source_ids[1]} |
| Liquidity rail | Auction/RFQ/ATS style liquidity rather than unrestricted AMM liquidity | {source_ids[2]} |
| Investor workflow | Research, subscription, custody, reporting, and exit path in one surface | {source_ids[3]} |

## Key Findings

1. A Pre-IPO RWA launchpad is structurally closer to a regulated issuance and liquidity workflow than a DeFi token
   sale. The key product question is whether legal ownership, disclosure, and transfer restrictions survive the token
   wrapper. Sources: {source_ids[0]}, {source_ids[1]}.
2. The main adoption wedge is institutional familiarity with tokenized funds and private-market access, but the hard
   bottleneck is secondary liquidity design. Sources: {source_ids[2]}, {source_ids[3]}.
3. The defensible product surface is a bundled due-diligence, issuance, compliance, and reporting system. A simple
   marketplace page does not solve enough of the workflow to be durable. Sources: {source_ids[0]}, {source_ids[3]}.

## Strategic Implications

- Build compliance and disclosure as first-class workflow primitives, not as static documents.
- Treat liquidity as controlled matching/RFQ/ATS-style routing before considering open AMM rails.
- Benchmark against tokenized fund issuers, private market platforms, and on-chain transfer-control providers.

## Contradictions And Gaps

- The available source set is sufficient for a directional product thesis, but not enough for jurisdiction-specific
  legal advice. Next checks should compare US, Singapore, and Hong Kong transfer restrictions.
"""


class _GenericReasoner:
    async def refine_plan(self, request, plan):
        return plan

    async def extract_claims(self, request, source):
        return []

    async def synthesize_report(self, request, plan, ledger, evaluation):
        return "RWA is a big opportunity. Projects should manage risks and follow compliance."


async def _research_tool(tool_name: str, arguments: dict) -> str:
    if tool_name == "web_search":
        return "\n".join(
            [
                "https://issuer.example/pre-ipo-rwa",
                "https://regulator.example/private-markets-tokenization",
                "https://liquidity.example/rwa-secondary-market",
                "https://investor.example/tokenized-private-assets",
            ]
        )
    if tool_name == "web_fetch":
        url = arguments["url"]
        return (
            f"Title: Evidence for {url}\n"
            "The RWA private-market launchpad model requires verified asset ownership, investor qualification, "
            "transfer restrictions, disclosure controls, and a controlled secondary liquidity route. "
            "Tokenized private-market products face custody, valuation, and jurisdiction-specific compliance risks."
        )
    raise AssertionError(f"unexpected tool: {tool_name}")


@pytest.mark.asyncio
async def test_orchestrator_uses_reasoner_to_write_analyst_grade_synthesis(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    result = await DeepResearchOrchestrator(_research_tool, reasoner=_AnalystReasoner()).run(
        ResearchRequest(
            question="Deep research the RWA Pre-IPO Launchpad opportunity.",
            mode="industry_research",
            depth="full",
            max_rounds=1,
            max_sources=4,
        ),
        artifact_dir=tmp_path,
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))

    assert result.status == "completed"
    assert final["quality_gates"]["synthesis"] == "passed"
    assert "## Executive Thesis" in report
    assert "## Market Map" in report
    assert "## Strategic Implications" in report
    assert report.count("src_") >= 4
    assert "RWA is a big opportunity" not in report
    assert "RWA pre-IPO launchpad market map" in (tmp_path / "plan.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_orchestrator_fails_generic_synthesis_even_with_fetched_sources(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    result = await DeepResearchOrchestrator(_research_tool, reasoner=_GenericReasoner()).run(
        ResearchRequest(
            question="Deep research the RWA Pre-IPO Launchpad opportunity.",
            mode="industry_research",
            depth="full",
            max_rounds=1,
            max_sources=4,
        ),
        artifact_dir=tmp_path,
    )

    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))

    assert result.status == "failed"
    assert final["quality_gates"]["synthesis"] == "failed"
    assert any("synthesis" in gap.lower() for gap in final["gaps"])
