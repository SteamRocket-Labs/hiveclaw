"""Unit + integration tests for Tier 2-1 ResearchReflector."""

from __future__ import annotations

import json

import pytest


def _make_request_plan(tmp_path):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.planner import build_research_plan
    from app.services.deep_research.schemas import ResearchRequest

    request = ResearchRequest(question="test research", mode="topic_deep_dive", max_rounds=4)
    plan = build_research_plan(request)
    ledger = EvidenceLedger(tmp_path)
    return request, plan, ledger


@pytest.mark.asyncio
async def test_reflector_uses_mechanical_fallback_when_reasoner_missing(tmp_path):
    """T2-1: with no reasoner wired, behavior reverts to evaluator's mechanical signal —
    backward compatible with Tier 1."""
    from app.services.deep_research.reflector import ResearchReflector

    request, plan, ledger = _make_request_plan(tmp_path)
    reflector = ResearchReflector(reasoner=None)

    decision = await reflector.reflect(
        request=request,
        plan=plan,
        ledger=ledger,
        round_index=1,
        source_notes=[],
        lane_summaries=[],
        evaluator_gaps=["Coverage below requirement"],
        evaluator_next_queries=["test research additional independent sources"],
    )

    assert decision.source == "evaluator_fallback"
    assert decision.stop_signal is False
    assert decision.next_queries
    assert decision.next_queries[0]["query"] == "test research additional independent sources"
    assert decision.next_queries[0]["targets"] == "evaluator-suggested"


@pytest.mark.asyncio
async def test_reflector_invokes_reasoner_and_normalizes_decision(tmp_path):
    """T2-1: when reasoner.reflect_progress returns a dict, normalize into ReflectionDecision
    with structured next_queries (query/lane_id/targets)."""
    from app.services.deep_research.reflector import ResearchReflector

    request, plan, ledger = _make_request_plan(tmp_path)

    class _StubReasoner:
        async def reflect_progress(self, **kwargs):
            return {
                "stop_signal": False,
                "rationale": "Need regulator stance + a competitor comparison number.",
                "next_queries": [
                    {"query": "SEC Reg D 506(c) 2026 tokenization rulings", "lane_id": "regulatory", "targets": "regulator stance"},
                    "Republic Forge vs CartaX 2026 trade volume",
                ],
            }

    decision = await ResearchReflector(reasoner=_StubReasoner()).reflect(
        request=request,
        plan=plan,
        ledger=ledger,
        round_index=2,
        source_notes=[{"source_id": "src_a", "key_entities": ["BlackRock"]}],
        lane_summaries=[{"lane_id": "primary", "evidence_strength": "moderate"}],
        evaluator_gaps=[],
        evaluator_next_queries=[],
    )

    assert decision.source == "reasoner"
    assert decision.stop_signal is False
    assert decision.rationale.startswith("Need regulator stance")
    assert len(decision.next_queries) == 2
    assert decision.next_queries[0]["query"].startswith("SEC Reg D")
    assert decision.next_queries[0]["lane_id"] == "regulatory"
    assert decision.next_queries[1]["targets"] == "follow-up"


@pytest.mark.asyncio
async def test_reflector_stop_signal_is_respected(tmp_path):
    """T2-1: stop_signal=true with empty next_queries should propagate."""
    from app.services.deep_research.reflector import ResearchReflector

    request, plan, ledger = _make_request_plan(tmp_path)

    class _StubReasoner:
        async def reflect_progress(self, **kwargs):
            return {"stop_signal": True, "rationale": "Coverage is strong enough.", "next_queries": []}

    decision = await ResearchReflector(reasoner=_StubReasoner()).reflect(
        request=request,
        plan=plan,
        ledger=ledger,
        round_index=3,
        source_notes=[],
        lane_summaries=[],
        evaluator_gaps=[],
        evaluator_next_queries=["this is ignored when reasoner says stop"],
    )

    assert decision.stop_signal is True
    assert decision.next_queries == []


@pytest.mark.asyncio
async def test_reflector_falls_back_when_reasoner_raises(tmp_path):
    """T2-1: LLM failures never crash the loop — fall back to evaluator signal."""
    from app.services.deep_research.reflector import ResearchReflector

    request, plan, ledger = _make_request_plan(tmp_path)

    class _FailingReasoner:
        async def reflect_progress(self, **kwargs):
            raise RuntimeError("LLM transport error")

    decision = await ResearchReflector(reasoner=_FailingReasoner()).reflect(
        request=request,
        plan=plan,
        ledger=ledger,
        round_index=1,
        source_notes=[],
        lane_summaries=[],
        evaluator_gaps=["plurality below threshold"],
        evaluator_next_queries=["follow-up query"],
    )

    assert decision.source == "evaluator_fallback"
    assert decision.stop_signal is False
    assert decision.next_queries[0]["query"] == "follow-up query"


@pytest.mark.asyncio
async def test_orchestrator_writes_reflection_jsonl_and_honors_stop_signal(tmp_path):
    """T2-1 integration: when reasoner.reflect_progress returns stop_signal=True after
    round 1, orchestrator persists reflection.jsonl and skips round 2."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    round_calls: list[int] = []

    class _ReflectiveReasoner:
        async def refine_plan(self, request, plan):
            return plan

        async def extract_claims(self, request, source):
            return [
                {
                    "text": f"{source.publisher} reports a concrete adoption signal in 2026.",
                    "status": "verified",
                    "source_ids": [source.source_id],
                    "evidence": source.content[:200],
                }
            ]

        async def reflect_progress(self, **kwargs):
            round_calls.append(kwargs.get("round_index"))
            return {
                "stop_signal": True,
                "rationale": "Coverage is strong after round 1 — stop further fan-out.",
                "next_queries": [],
            }

        async def synthesize_report(
            self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None
        ):
            ids = list(ledger.sources)
            return f"""# Brief

## Executive Thesis

In 2026 BlackRock BUIDL, Securitize, Ondo Finance, and JPMorgan Onyx captured
$1.24B AUM across 14 vehicles backed by SEC, MAS, and SFC disclosures.
Sources: {ids[0]}.

## Method And Source Standard

Primary issuer filings prioritised over secondary commentary.

## Key Findings

- Tokenized treasuries dominate 2026 RWA flow at $1.24B AUM across 14 vehicles.

## Source Ledger

- `{ids[0]}` Issuer A 2026 disclosure
"""

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://issuer.example/rwa"
        if tool_name == "web_fetch":
            return (
                "Title: Issuer A 2026 disclosure\n"
                "Issuer A reports $1.24B AUM across 14 tokenized treasury vehicles in 2026, "
                "backed by SEC and MAS disclosures."
            )
        raise AssertionError(tool_name)

    await DeepResearchOrchestrator(fake_tool, reasoner=_ReflectiveReasoner()).run(
        ResearchRequest(
            question="research RWA adoption",
            mode="topic_deep_dive",
            max_rounds=4,
            max_sources=2,
        ),
        artifact_dir=tmp_path,
    )

    reflection_lines = [
        line for line in (tmp_path / "reflection.jsonl").read_text("utf-8").splitlines() if line.strip()
    ]
    assert reflection_lines, "Tier 2-1 must persist reflection.jsonl"
    decision = json.loads(reflection_lines[0])
    assert decision["stop_signal"] is True
    assert decision["round_index"] == 1
    # Stop signal honored → no round 2 reflect call
    assert round_calls == [1]


# ─────────── T2-2: draft + review two-stage synthesis ───────────


@pytest.mark.asyncio
async def test_orchestrator_prefers_two_stage_when_reasoner_supports_it(tmp_path):
    """T2-2: when reasoner exposes draft_report + review_report, orchestrator uses the
    two-stage path and the merged report from review_report ends up in report.md."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    draft_calls: list[str] = []
    review_called: list[bool] = []
    fallback_called: list[bool] = []

    class _TwoStageReasoner:
        async def extract_claims(self, request, source):
            return [
                {
                    "text": f"{source.publisher} confirms a 35% YoY adoption signal.",
                    "status": "verified",
                    "source_ids": [source.source_id],
                    "evidence": source.content[:200],
                }
            ]

        async def draft_report(
            self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None, sections=None
        ):
            for section in sections or [
                "Executive Thesis",
                "Method And Source Standard",
                "Market Map",
                "Key Findings",
                "Strategic Implications",
                "Contradictions And Gaps",
                "Source Ledger",
            ]:
                draft_calls.append(section)
            ids = list(ledger.sources)
            return {
                "Executive Thesis": (
                    f"BlackRock BUIDL, Securitize, Ondo Finance, and JPMorgan Onyx captured $1.24B AUM across "
                    f"14 tokenized treasuries by Q4 2026. Sources: {ids[0]}."
                ),
                "Key Findings": (
                    f"- Tokenized treasuries grew 35% YoY to $1.24B AUM across 14 vehicles. Sources: {ids[0]}."
                ),
                "Source Ledger": f"- `{ids[0]}` Issuer A 2026 disclosure",
            }

        async def review_report(self, drafts, *, request, ledger, source_notes=None, lane_summaries=None):
            review_called.append(True)
            ids = list(ledger.sources)
            merged = (
                "# Deep Research: RWA Treasury Adoption\n\n"
                "## Executive Thesis\n\n"
                f"BlackRock BUIDL, Securitize, Ondo Finance, and JPMorgan Onyx captured "
                f"$1.24B AUM across 14 tokenized treasuries by Q4 2026. SEC, MAS, and SFC "
                f"each reviewed disclosures in Q2 2026. Sources: {ids[0]}.\n\n"
                "## Method And Source Standard\n\n"
                "Primary filings prioritised over secondary analyst commentary.\n\n"
                "## Market Map\n\n"
                "| Lane | Players | Evidence |\n|---|---|---|\n"
                f"| Issuance | BlackRock, Securitize, Ondo Finance, JPMorgan | {ids[0]} |\n\n"
                "## Key Findings\n\n"
                f"- Tokenized treasuries grew 35% YoY to $1.24B AUM across 14 vehicles via "
                f"BlackRock BUIDL and Securitize. Sources: {ids[0]}.\n"
                "- Custody handled by State Street and BNY Mellon remains binding.\n\n"
                "## Strategic Implications\n\n"
                "- Bundle issuance with compliance via Republic Forge or CartaX.\n\n"
                "## Contradictions And Gaps\n\n"
                "- Hong Kong SFC vs Singapore MAS rules diverge for 5 product types.\n\n"
                "## Source Ledger\n\n"
                f"- `{ids[0]}` Issuer A 2026 disclosure\n"
            )
            return {"merged_report": merged, "quality_score": 0.86, "issues": []}

        async def synthesize_report(self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None):
            fallback_called.append(True)
            return "should not be used"

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://issuer.example/rwa"
        if tool_name == "web_fetch":
            return (
                "Title: Issuer A 2026 disclosure\n"
                "Issuer A reports tokenized treasury holdings reached $1.24B in 2026 across 14 vehicles."
            )
        raise AssertionError(tool_name)

    result = await DeepResearchOrchestrator(fake_tool, reasoner=_TwoStageReasoner()).run(
        ResearchRequest(
            question="Research RWA treasury adoption.",
            mode="industry_research",
            depth="standard",
            max_rounds=1,
            max_sources=1,
        ),
        artifact_dir=tmp_path,
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")

    assert result.status == "completed", result.gaps
    assert review_called == [True], "review_report must be called exactly once"
    assert not fallback_called, "single-stage synthesize_report must not fire when two-stage succeeds"
    assert len(draft_calls) >= 3, "draft_report must be invoked for multiple sections"
    assert "BlackRock BUIDL" in report
    assert "Issuer A 2026 disclosure" in report


@pytest.mark.asyncio
async def test_orchestrator_falls_back_to_single_stage_when_draft_review_empty(tmp_path):
    """T2-2: if two-stage produces an empty drafts dict, fall back to single-stage."""
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    single_stage_called: list[bool] = []

    class _BrokenTwoStage:
        async def extract_claims(self, request, source):
            return [
                {
                    "text": f"{source.publisher} confirms a concrete adoption.",
                    "status": "verified",
                    "source_ids": [source.source_id],
                    "evidence": source.content[:200],
                }
            ]

        async def draft_report(self, request, plan, ledger, evaluation, **kwargs):
            return {}

        async def review_report(self, drafts, **kwargs):
            raise AssertionError("review_report must not be called when drafts are empty")

        async def synthesize_report(self, request, plan, ledger, evaluation, *, source_notes=None, lane_summaries=None):
            single_stage_called.append(True)
            ids = list(ledger.sources)
            return f"""# RWA Treasury Adoption Brief

## Executive Thesis

In 2026 BlackRock BUIDL, Securitize, Ondo Finance, and JPMorgan Onyx captured combined
AUM of $1.24B across 14 tokenized treasury vehicles, backed by SEC Reg D, MAS Securities
Act, and SFC Type 1 disclosures filed in Q2 2026. Sources: {ids[0]}.

## Method And Source Standard

Primary issuer filings from BlackRock, Securitize, Ondo Finance, and JPMorgan were prioritised
over secondary analyst commentary from Bernstein, Bloomberg, and Coindesk.

## Market Map

| Lane | Players | Evidence |
|---|---|---|
| Issuance | BlackRock BUIDL, Securitize, Ondo Finance, JPMorgan Onyx | {ids[0]} |
| Regulatory | SEC, MAS, SFC, FINRA | {ids[0]} |
| Custody | State Street, BNY Mellon | {ids[0]} |

## Key Findings

- Tokenized treasuries grew 35% YoY to $1.24B AUM across 14 vehicles via BlackRock BUIDL
  and Securitize, with SEC Reg D coverage in 17 filings. Sources: {ids[0]}.
- Custody handled by State Street and BNY Mellon remains the binding constraint;
  18 of the top 20 institutional buyers cite custody concerns.

## Strategic Implications

- Bundle issuance, compliance, and reporting into one workflow via Republic Forge or CartaX.
- Treat secondary liquidity routing as the durable moat for any new launchpad.

## Contradictions And Gaps

- Hong Kong SFC vs Singapore MAS transfer-restriction rules diverge for 5 product types.

## Source Ledger

- `{ids[0]}` Issuer A 2026 disclosure
"""

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://issuer.example/rwa"
        if tool_name == "web_fetch":
            return (
                "Title: Issuer A 2026 disclosure\n"
                "Issuer A reports tokenized treasury holdings reached $1.24B in 2026 across 14 vehicles."
            )
        raise AssertionError(tool_name)

    result = await DeepResearchOrchestrator(fake_tool, reasoner=_BrokenTwoStage()).run(
        ResearchRequest(
            question="RWA",
            mode="industry_research",
            depth="standard",
            max_rounds=1,
            max_sources=1,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "completed", result.gaps
    assert single_stage_called == [True], "single-stage must be the fallback when two-stage drafts are empty"


# ─────────── T2-5: footnote citations ───────────


def test_apply_footnotes_rewrites_inline_src_to_footnote_markers(tmp_path):
    """T2-5: [src_xxx] and bare src_xxx in prose become [^N]; ledger backticks stay."""
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.orchestrator import _apply_footnotes
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    a = ledger.add_source(
        url="https://issuer.example/a",
        title="Issuer A 2026 disclosure",
        publisher="Issuer A",
        source_type=SourceType.PRIMARY,
        content="x",
    )
    b = ledger.add_source(
        url="https://regulator.example/b",
        title="Regulator B Q2 filing",
        publisher="Regulator B",
        source_type=SourceType.REGULATORY,
        content="y",
    )

    report = (
        "# Brief\n\n"
        "## Executive Thesis\n\n"
        f"Evidence from [{a.source_id}] supports the thesis. Sources: {b.source_id}.\n\n"
        "## Source Ledger\n\n"
        f"- `{a.source_id}` Issuer A 2026 disclosure — Issuer A — {a.url}\n"
        f"- `{b.source_id}` Regulator B Q2 filing — Regulator B — {b.url}\n"
    )

    out = _apply_footnotes(report, ledger)

    # Inline references rewritten to footnote markers
    assert f"[{a.source_id}]" not in out
    assert "[^1]" in out
    assert "[^2]" in out
    # Backtick ledger entries are preserved
    assert f"`{a.source_id}`" in out
    assert f"`{b.source_id}`" in out
    # Footnote table appended with title/publisher/url
    assert "## Footnotes" in out
    assert "[^1]: Issuer A 2026 disclosure — Issuer A — https://issuer.example/a" in out
    assert "[^2]: Regulator B Q2 filing — Regulator B — https://regulator.example/b" in out


def test_apply_footnotes_noop_when_no_inline_refs(tmp_path):
    """T2-5: a report without inline src references is returned untouched."""
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.orchestrator import _apply_footnotes

    ledger = EvidenceLedger(tmp_path)
    report = "# Brief\n\n## Executive Thesis\n\nGeneric narrative without references.\n"

    out = _apply_footnotes(report, ledger)
    assert out == report


# ─────────── T2-6: hard routing reject ───────────


def test_routing_reminder_module_supports_hard_reject_after_threshold():
    """T2-6: same condition stack but escalated — past N=5 web_search with no
    deep_research_* invocation, the module should expose a hard-reject signal."""
    from app.services.deep_research.routing_reminder import (
        reset_session_state,
        should_hard_reject_web_search,
    )

    reset_session_state()
    available = ("web_search", "deep_research_run")
    intent = ("Investor due diligence on RWA market.",)

    # Under threshold: no reject
    for i in range(5):
        decision = should_hard_reject_web_search(
            session_id="sess-hard",
            available_tool_names=available,
            intent_hints=intent,
        )
        assert decision is None, f"call {i + 1} should not yet hard reject"

    # 6th call crosses the threshold
    final = should_hard_reject_web_search(
        session_id="sess-hard",
        available_tool_names=available,
        intent_hints=intent,
    )
    assert final is not None
    assert "deep_research_run" in final
    reset_session_state()


def test_routing_reminder_hard_reject_skipped_without_intent():
    """T2-6: hard reject must not fire for non-deep-research intents."""
    from app.services.deep_research.routing_reminder import (
        reset_session_state,
        should_hard_reject_web_search,
    )

    reset_session_state()
    for _ in range(8):
        decision = should_hard_reject_web_search(
            session_id="sess-hard-2",
            available_tool_names=("web_search", "deep_research_run"),
            intent_hints=("look up the weather forecast",),
        )
        assert decision is None
    reset_session_state()
