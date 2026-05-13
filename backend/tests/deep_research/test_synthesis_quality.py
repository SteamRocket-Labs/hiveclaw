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

    async def synthesize_report(self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None):
        source_ids = list(ledger.sources)
        return f"""# RWA Pre-IPO Launchpad Deep Research

## Executive Thesis

The investable opportunity is not generic RWA tokenization; it is the narrower ability to combine issuer diligence,
transfer restrictions, compliant distribution, and secondary liquidity into one launchpad workflow. Comparable
platforms — Republic Forge, CartaX, Securitize, and INX Securities — already exceeded $4.2B in cumulative volume
across 28 issuances by Q4 2026. Evidence from {source_ids[0]} and {source_ids[1]} supports the market infrastructure
requirement, while {source_ids[2]} and {source_ids[3]} show the liquidity and compliance constraints (SEC Reg D 506(c),
MAS Securities Act exemption, SFC Type 1 license) that decide whether the product can scale beyond 17 jurisdictions.

## Market Map

| Segment | What matters | Evidence |
|---|---|---|
| Issuer onboarding | Verified asset ownership, disclosure package, cap-table or SPV mapping | {source_ids[0]} |
| Compliance rail | Investor qualification, transfer controls, SEC/MAS/SFC policy | {source_ids[1]} |
| Liquidity rail | Auction/RFQ/ATS style liquidity rather than unrestricted AMM liquidity | {source_ids[2]} |
| Investor workflow | Research, subscription, custody by BNY Mellon, reporting, and exit | {source_ids[3]} |

## Key Findings

1. A Pre-IPO RWA launchpad is structurally closer to a regulated issuance and liquidity workflow than a DeFi token
   sale. The key product question is whether legal ownership, disclosure, and transfer restrictions survive the token
   wrapper. Republic Forge and CartaX took 18-24 months to clear US Reg D before live trading.
   Sources: {source_ids[0]}, {source_ids[1]}.
2. The main adoption wedge is institutional familiarity with tokenized funds (BlackRock BUIDL grew from $250M in Q1
   2026 to $1.7B by Q4 2026) and private-market access via Carta. The hard bottleneck is secondary liquidity design.
   Sources: {source_ids[2]}, {source_ids[3]}.
3. The defensible product surface is a bundled due-diligence, issuance, compliance, and reporting system. Of the top
   12 platforms, only 3 (Securitize, Republic Forge, INX Securities) closed the full workflow loop in 2026.
   Sources: {source_ids[0]}, {source_ids[3]}.

## Strategic Implications

- Build compliance and disclosure as first-class workflow primitives, not as static documents.
- Treat liquidity as controlled matching/RFQ/ATS-style routing before considering open AMM rails.
- Benchmark against Securitize, Ondo Finance, Republic Forge, and on-chain transfer-control providers.

## Contradictions And Gaps

- The available source set is sufficient for a directional product thesis but not enough for jurisdiction-specific
  legal advice. Next checks should compare US Reg D, Singapore SFA, and Hong Kong SFC transfer restrictions across
  the 5 most relevant product categories.
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


class _PastedListReasoner:
    """Mirrors the orchestrator._fallback_analyst_report shape: an evidence-list dump that
    cites enough source ids and has Executive/Findings/Source sections, but contains no
    analytical narrative, no numbers, and no named entities.

    Pre Tier-1 the synthesis gate accepts this (length + section + citation checks all pass);
    post Tier-1 the dump-pattern + digit + entity checks reject it as not analyst-grade."""

    async def refine_plan(self, request, plan):
        return plan

    async def extract_claims(self, request, source):
        return [
            {
                "text": f"{source.publisher} reports an adoption signal worth tracking later.",
                "status": "verified",
                "source_ids": [source.source_id],
                "evidence": source.content[:200],
            }
        ]

    async def synthesize_report(self, request, plan, ledger, evaluation):
        ids = list(ledger.sources)
        lines = [
            "# Deep Research Report",
            "",
            "## Executive Thesis",
            "",
            (
                "This report is an evidence packet generated from fetched sources. "
                "No conclusion below should be treated as stronger than the cited source ledger allows."
            ),
            "",
            "## Source-Grounded Findings",
            "",
        ]
        for index, src in enumerate(ids, start=1):
            lines.append(f"{index}. The publisher discusses tokenized assets. Sources: {src}.")
            lines.append(f"{index + len(ids)}. The publisher addresses compliance pathways. Sources: {src}.")
        lines.extend(["", "## Source Ledger", ""])
        for src in ids:
            lines.append(f"- `{src}` Working paper — Generic Publisher — https://example.invalid/{src}")
        return "\n".join(lines)


@pytest.mark.asyncio
async def test_orchestrator_rejects_evidence_list_dump_as_synthesis(tmp_path):
    """Regression: a pasted-list markdown that cites enough source ids and has the
    required headings but lacks any analytical content must be rejected by the gate."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    result = await DeepResearchOrchestrator(_research_tool, reasoner=_PastedListReasoner()).run(
        ResearchRequest(
            question="Deep research the RWA Pre-IPO Launchpad opportunity.",
            mode="industry_research",
            depth="standard",
            max_rounds=1,
            max_sources=4,
        ),
        artifact_dir=tmp_path,
    )

    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))

    assert result.status == "failed", "Evidence-list dump must not be promoted to a completed report."
    assert final["quality_gates"]["synthesis"] == "failed"
    assert any("synthesis" in gap.lower() for gap in final["gaps"])


def _seed_ledger(tmp_path, source_ids: list[str]):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceRecord, SourceType

    ledger = EvidenceLedger(tmp_path)
    for sid in source_ids:
        ledger.sources[sid] = SourceRecord(
            source_id=sid,
            url=f"https://example.invalid/{sid}",
            title=f"Title {sid}",
            publisher=f"Publisher {sid}",
            source_type=SourceType.UNKNOWN,
            content="",
        )
    return ledger


_SOURCE_IDS = ["src_aaaaaaaaaaaa", "src_bbbbbbbbbbbb", "src_cccccccccccc", "src_dddddddddddd"]


def _low_digit_industry_report(source_ids: list[str]) -> str:
    """Long analytical-looking prose with refs but no concrete numbers, dates, or amounts."""
    return f"""# Industry Brief

## Executive Thesis

The investable opportunity centers on regulated workflows for issuance and liquidity,
not on generic tokenization narratives. Evidence from {source_ids[0]} and {source_ids[1]}
confirms compliance and disclosure are decisive, while the structural shape of the market
is governed by transfer restrictions and controlled secondary venues.

## Method And Source Standard

The review draws on issuer disclosures, regulatory filings, secondary market data,
and operator interviews. Sources are weighted by primary status and recency.

## Market Map

The landscape splits into issuer-side, compliance-side, and liquidity-side providers,
each with overlapping but distinct moats. Sources {source_ids[2]} and {source_ids[3]}
describe how these segments interact in practice and where defensibility accumulates.

## Key Findings

- A launchpad must own issuance, compliance, and liquidity to be structurally defensible.
  Sources: {source_ids[0]}, {source_ids[1]}.
- The hardest engineering surface is controlled secondary liquidity, not the storefront.
  Sources: {source_ids[2]}, {source_ids[3]}.
- The defensible product surface is a bundled diligence, issuance, and reporting system.
  Sources: {source_ids[0]}, {source_ids[3]}.

## Strategic Implications

- Treat compliance and disclosure as first-class workflow primitives.
- Route liquidity through controlled matching rather than open AMM rails.
- Benchmark against tokenized fund issuers and private market platforms.

## Contradictions And Gaps

- Jurisdictional coverage remains incomplete and demands targeted follow-up.

## Source Ledger

References for the analytical claims above are recorded in sources.jsonl.
"""


def _low_entity_industry_report(source_ids: list[str]) -> str:
    """Quantitatively rich prose using generic nouns only — no named companies, regulators,
    or products. After T1-5 the named-entity gate must reject this for industry_research."""
    return f"""# generic adoption brief

## executive thesis

in 2026 the sector saw 35 distinct deals across 12 jurisdictions, with combined volume of
4.2 billion across 240 institutional buyers. the market scaled by a factor of 3.5 against
the 2025 baseline of 1.2 billion. sources {source_ids[0]} and {source_ids[1]} describe the
macro pattern. 18 of the deals occurred in the first 6 months of the year.

## method and source standard

we reviewed 12 disclosures, 8 issuer reports, and 5 secondary venue summaries.

## market map

the segments include providers with different approaches. about 45% target one channel,
55% target another. there were 9 strategic transactions in 2026 and 14 in 2025.
sources {source_ids[2]} and {source_ids[3]} cover the operator perspective.

## key findings

- institutions allocated 850 million in 2026 against 420 million in 2025.
  sources: {source_ids[0]}, {source_ids[1]}.
- the middle-tier operator captured 28% of total volume.
  sources: {source_ids[2]}, {source_ids[3]}.
- 73% of issuers used a single regulatory exemption type across 17 filings.
  sources: {source_ids[0]}, {source_ids[2]}.

## strategic implications

- bundle compliance and issuance to capture the structural margin.
- treat secondary venue routing as the durable moat.

## contradictions and gaps

- jurisdictional rule differences not yet mapped across 14 product types.

## source ledger

references for findings above are recorded in sources.jsonl.
"""


def test_synthesis_gate_rejects_low_digit_industry_research(tmp_path):
    """T1-5: industry_research / full reports must carry concrete numbers; prose with
    almost no digits should be rejected even when sections and refs are present."""
    from app.services.deep_research.orchestrator import _evaluate_synthesis_quality
    from app.services.deep_research.schemas import ResearchRequest

    ledger = _seed_ledger(tmp_path, _SOURCE_IDS)
    report = _low_digit_industry_report(_SOURCE_IDS)
    request = ResearchRequest(
        question="industry deep dive",
        mode="industry_research",
        depth="full",
    )

    state, gap = _evaluate_synthesis_quality(report, request=request, ledger=ledger)

    assert state == "failed"
    assert any(token in gap.lower() for token in ("number", "digit", "quantitative"))


def test_synthesis_gate_rejects_low_entity_industry_research(tmp_path):
    """T1-5: industry_research reports must reference named entities (companies, regulators,
    products). Generic noun prose, however quantitative, should be rejected."""
    from app.services.deep_research.orchestrator import _evaluate_synthesis_quality
    from app.services.deep_research.schemas import ResearchRequest

    ledger = _seed_ledger(tmp_path, _SOURCE_IDS)
    report = _low_entity_industry_report(_SOURCE_IDS)
    request = ResearchRequest(
        question="industry deep dive",
        mode="industry_research",
        depth="full",
    )

    state, gap = _evaluate_synthesis_quality(report, request=request, ledger=ledger)

    assert state == "failed"
    assert any(token in gap.lower() for token in ("entit", "named", "actor"))


def test_synthesis_gate_audit_mode_uses_lower_digit_threshold(tmp_path):
    """T1-5: source_ledger_audit mode operates on a relaxed digit threshold (~8) because
    audit-style reports are about claim provenance, not market quantification."""
    from app.services.deep_research.orchestrator import _evaluate_synthesis_quality
    from app.services.deep_research.schemas import ResearchRequest

    ledger = _seed_ledger(tmp_path, _SOURCE_IDS)
    request = ResearchRequest(
        question="audit prospectus claims",
        mode="source_ledger_audit",
        depth="standard",
    )
    report = f"""# Audit Report

## Executive Thesis

The prospectus claims hold up against the 4 cited filings dated between 2023 and 2026.
Sources {_SOURCE_IDS[0]} and {_SOURCE_IDS[1]} are primary disclosures from the issuer;
{_SOURCE_IDS[2]} and {_SOURCE_IDS[3]} provide secondary corroboration. The customer
concentration claim, the revenue recognition claim, and the segment classification claim
were each cross-checked against the underlying ledger references.

## Method And Source Standard

Audit was performed against the issuer's S-1 (Item 7), 10-K, and the auditor opinion.
Primary disclosures take precedence; secondary corroboration is used only when primary
is silent. Where evidence is mixed or partial, the claim is downgraded to inferred or
unsupported per the source standard policy.

## Findings

- The customer concentration claim is supported by primary source {_SOURCE_IDS[0]} but the
  jurisdiction breakdown is not disclosed in the same filing and requires follow-up against
  Form D filings or the most recent 8-K.
- The revenue recognition claim is partially supported by {_SOURCE_IDS[1]}; the segment
  allocation methodology is described but the underlying ledger references are redacted.
- The segment classification claim is corroborated by {_SOURCE_IDS[2]} and {_SOURCE_IDS[3]}
  but the two sources disagree on the secondary segment cutoff date.

## Source Ledger

- `{_SOURCE_IDS[0]}` Issuer S-1 disclosure (primary)
- `{_SOURCE_IDS[1]}` Annual 10-K cross-checked disclosure (primary)
- `{_SOURCE_IDS[2]}` Independent analyst note (secondary)
- `{_SOURCE_IDS[3]}` Industry register cross-reference (secondary)
"""

    state, gap = _evaluate_synthesis_quality(report, request=request, ledger=ledger)

    assert state == "passed", gap
