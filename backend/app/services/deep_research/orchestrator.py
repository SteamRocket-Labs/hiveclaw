from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.services.deep_research.evaluator import ResearchEvaluator
from app.services.deep_research.extractor import extract_claims_from_source
from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.planner import build_research_plan
from app.services.deep_research.reader import ResearchReader
from app.services.deep_research.schemas import (
    ResearchRequest,
    ResearchRun,
    ResearchStep,
    SourceType,
    new_id,
)
from app.services.deep_research.searcher import ResearchSearcher, ToolInvoker
from app.services.deep_research.writer import ResearchArtifactWriter


class DeepResearchOrchestrator:
    def __init__(self, tool_invoker: ToolInvoker, *, reasoner: Any | None = None):
        self.tool_invoker = tool_invoker
        self.reasoner = reasoner
        self.evaluator = ResearchEvaluator()

    async def run(self, request: ResearchRequest, *, artifact_dir: str | Path) -> ResearchRun:
        artifact_path = Path(artifact_dir)
        writer = ResearchArtifactWriter(artifact_path)
        ledger = EvidenceLedger(artifact_path)
        writer.write_request(request)
        plan = build_research_plan(request)
        plan = await _maybe_refine_plan(self.reasoner, request, plan)
        writer.write_plan(plan)
        writer.append_step(_step("plan", "completed", f"Built {len(plan.lanes)} research lane(s)."))

        searcher = ResearchSearcher(self.tool_invoker)
        reader = ResearchReader(self.tool_invoker)
        accepted_sources = 0
        evaluation = None
        seen_source_urls: set[str] = set()
        searched_queries: set[str] = set()

        for round_index in range(1, request.max_rounds + 1):
            writer.append_step(_step("search", "running", f"Starting research round {round_index}."))
            candidates = []
            for lane in plan.lanes:
                lane = _lane_with_unsearched_queries(lane, searched_queries)
                if not lane.queries:
                    continue
                remaining = max(request.max_sources - accepted_sources - len(candidates), 0)
                if remaining <= 0:
                    break
                lane_candidates = await searcher.search_lane(lane, max_results=remaining)
                searched_queries.update(_query_key(query.query) for query in lane.queries)
                candidates.extend(lane_candidates)
            writer.append_step(
                _step("search", "completed", f"Discovered {len(candidates)} candidate URL(s).", {"round": round_index})
            )

            for candidate in candidates:
                if accepted_sources >= request.max_sources:
                    break
                if candidate.url in seen_source_urls:
                    writer.append_step(
                        _step("read", "skipped", f"Skipped duplicate candidate URL {candidate.url}.")
                    )
                    continue
                source_type = _source_type_for_lane(candidate.lane_id)
                fetched = await reader.fetch_candidate(candidate, source_type=source_type)
                if fetched is None:
                    writer.append_step(
                        _step("read", "failed", f"Could not fetch usable source text for {candidate.url}.")
                    )
                    continue
                source = ledger.add_source(
                    url=fetched.url,
                    title=fetched.title,
                    publisher=fetched.publisher,
                    source_type=fetched.source_type,
                    content=fetched.content,
                    lane_id=fetched.lane_id,
                    query=fetched.query,
                    fetch_tool=fetched.fetch_tool,
                )
                seen_source_urls.add(source.url)
                accepted_sources += 1
                reasoned_claims = await _maybe_extract_claims(self.reasoner, request, ledger, source)
                if not reasoned_claims:
                    extract_claims_from_source(ledger, source)
                writer.append_step(
                    _step("read", "completed", f"Fetched and ledgered {source.url}.", {"source_id": source.source_id})
                )

            evaluation = self.evaluator.evaluate(request=request, ledger=ledger, round_index=round_index)
            writer.append_evaluation(evaluation)
            writer.append_step(
                _step(
                    "evaluate",
                    "completed",
                    "Evaluated attribution, plurality, freshness, completeness, and contradiction gates.",
                    {"quality_gates": evaluation.quality_gates, "gaps": evaluation.gaps},
                )
            )
            if not evaluation.next_queries or accepted_sources >= request.max_sources:
                break
            _append_next_queries(plan, evaluation.next_queries)

        if evaluation is None:
            evaluation = self.evaluator.evaluate(request=request, ledger=ledger, round_index=request.max_rounds)

        report_markdown = await _synthesize_report(self.reasoner, request, plan, ledger, evaluation)
        synthesis_gate, synthesis_gap = _evaluate_synthesis_quality(
            report_markdown,
            request=request,
            ledger=ledger,
        )
        evaluation.quality_gates["synthesis"] = synthesis_gate
        if synthesis_gap:
            evaluation.gaps.append(synthesis_gap)
        writer.append_step(
            _step(
                "synthesize",
                synthesis_gate,
                "Synthesized analyst-grade report and checked source-grounded report quality.",
                {"synthesis_gate": synthesis_gate, "gap": synthesis_gap},
            )
        )

        failed_gates = {gate for gate, state in evaluation.quality_gates.items() if state == "failed"}
        status = (
            "completed"
            if ledger.sources and evaluation.quality_gates.get("attribution") == "passed" and not failed_gates
            else "failed"
        )
        return writer.finalize(
            request=request,
            plan=plan,
            ledger=ledger,
            evaluation=evaluation,
            status=status,
            report_markdown=report_markdown,
        )


async def run_deep_research(
    *,
    request: ResearchRequest,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace: Path,
    runtime_task_id: uuid.UUID | None = None,
    tool_executor: Callable[[str, dict[str, Any], uuid.UUID, uuid.UUID], Awaitable[str]] | None = None,
) -> ResearchRun:
    run_id = runtime_task_id.hex if runtime_task_id else uuid.uuid4().hex
    artifact_dir = (
        workspace / "runtime_artifacts" / "long_tasks" / run_id / "deep_research"
        if runtime_task_id
        else workspace / "runtime_artifacts" / "deep_research" / run_id
    )

    async def invoke_tool(tool_name: str, arguments: dict) -> str:
        executor = tool_executor or _default_tool_executor
        return await executor(tool_name, arguments, agent_id, user_id)

    reasoner = await _build_runtime_reasoner(agent_id=agent_id, user_id=user_id)
    return await DeepResearchOrchestrator(invoke_tool, reasoner=reasoner).run(request, artifact_dir=artifact_dir)


async def _default_tool_executor(tool_name: str, arguments: dict[str, Any], agent_id: uuid.UUID, user_id: uuid.UUID) -> str:
    from app.services.agent_tools import execute_tool

    return await execute_tool(tool_name, arguments, agent_id=agent_id, user_id=user_id)


def _step(phase: str, status: str, message: str, detail: dict[str, Any] | None = None) -> ResearchStep:
    return ResearchStep(step_id=new_id("step"), phase=phase, status=status, message=message, detail=detail or {})


def _source_type_for_lane(lane_id: str) -> SourceType:
    return {
        "official": SourceType.PRIMARY,
        "regulatory": SourceType.REGULATORY,
        "market": SourceType.DATASET,
        "technical": SourceType.TECHNICAL,
        "secondary": SourceType.SECONDARY,
    }.get(lane_id, SourceType.UNKNOWN)


async def _maybe_refine_plan(reasoner: Any | None, request: ResearchRequest, plan):
    if reasoner is None or not hasattr(reasoner, "refine_plan"):
        return plan
    try:
        refined = await reasoner.refine_plan(request, plan)
    except Exception:
        return plan
    return refined or plan


async def _maybe_extract_claims(reasoner: Any | None, request: ResearchRequest, ledger: EvidenceLedger, source) -> bool:
    if reasoner is None or not hasattr(reasoner, "extract_claims"):
        return False
    try:
        extracted = await reasoner.extract_claims(request, source)
    except Exception:
        return False
    if not isinstance(extracted, list):
        return False
    added = 0
    from app.services.deep_research.schemas import ClaimStatus

    for item in extracted[:4]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        evidence = str(item.get("evidence") or "").strip()
        if len(text) < 30 or not evidence:
            continue
        source_ids = item.get("source_ids")
        if not isinstance(source_ids, list) or source.source_id not in {str(value) for value in source_ids}:
            source_ids = [source.source_id]
        try:
            status = ClaimStatus(str(item.get("status") or "verified"))
        except ValueError:
            status = ClaimStatus.VERIFIED
        ledger.add_claim(
            text=text,
            status=status,
            source_ids=[str(value) for value in source_ids],
            evidence=evidence,
            notes=str(item.get("notes") or "").strip(),
            contradiction_group=item.get("contradiction_group") if isinstance(item.get("contradiction_group"), str) else None,
        )
        added += 1
    return added > 0


async def _synthesize_report(reasoner: Any | None, request: ResearchRequest, plan, ledger: EvidenceLedger, evaluation) -> str | None:
    if reasoner is not None and hasattr(reasoner, "synthesize_report"):
        try:
            report = await reasoner.synthesize_report(request, plan, ledger, evaluation)
        except Exception:
            report = None
        if isinstance(report, str) and report.strip():
            return report.strip() + "\n"
    return _fallback_analyst_report(request, plan, ledger, evaluation)


def _evaluate_synthesis_quality(report: str | None, *, request: ResearchRequest, ledger: EvidenceLedger) -> tuple[str, str]:
    if not report or len(report.strip()) < _minimum_report_chars(request):
        return "failed", "Synthesis quality failed: report is too short for a deep research deliverable."
    if not ledger.sources:
        return "failed", "Synthesis quality failed: no fetched source is available for source-grounded synthesis."
    cited_source_ids = {source_id for source_id in ledger.sources if source_id in report}
    required_citations = min(max(2, len(ledger.sources) // 2), len(ledger.sources))
    if len(cited_source_ids) < required_citations:
        return "failed", "Synthesis quality failed: report does not cite enough source ids from the evidence ledger."
    required_sections = ("Executive", "Findings", "Source")
    if not all(section.casefold() in report.casefold() for section in required_sections):
        return "failed", "Synthesis quality failed: report is missing executive, findings, or source-grounded sections."
    if _looks_like_generic_summary(report):
        return "failed", "Synthesis quality failed: report is generic and lacks concrete source-grounded analysis."
    return "passed", ""


def _minimum_report_chars(request: ResearchRequest) -> int:
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return 1200
    if depth in {"quick", "light"}:
        return 700
    return 900


def _looks_like_generic_summary(report: str) -> bool:
    lowered = " ".join(report.casefold().split())
    generic_phrases = (
        "big opportunity",
        "follow compliance",
        "manage risks",
        "early stage",
        "important trend",
    )
    return len(report) < 1800 and sum(1 for phrase in generic_phrases if phrase in lowered) >= 2


def _fallback_analyst_report(request: ResearchRequest, plan, ledger: EvidenceLedger, evaluation) -> str:
    source_items = list(ledger.sources.values())
    claim_items = list(ledger.claims)
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
    if claim_items:
        for index, claim in enumerate(claim_items[:10], start=1):
            sources = ", ".join(claim.source_ids)
            lines.append(f"{index}. {claim.text} Sources: {sources}.")
    else:
        lines.append("No material claims passed extraction. Treat the run as incomplete.")

    lines.extend(["", "## Market Map", "", "| Lane | Publisher | Evidence Role | Source |", "|---|---|---|---|"])
    for source in source_items[:12]:
        lines.append(f"| {source.lane_id or 'unknown'} | {source.publisher} | {source.source_type.value} | {source.source_id} |")

    lines.extend(["", "## Strategic Implications", ""])
    if source_items:
        hosts = ", ".join(sorted({urlparse(source.url).netloc.removeprefix("www.") for source in source_items})[:6])
        lines.append(f"- Evidence coverage spans {len(source_items)} fetched source(s) across: {hosts}.")
        lines.append("- Use the source ledger to separate verified claims from inferred or unsupported analysis.")
        lines.append("- Re-run with narrower scope if decisions require jurisdiction-specific, company-specific, or legal-grade conclusions.")
    else:
        lines.append("- No strategic implication can be supported without fetched sources.")

    lines.extend(["", "## Contradictions And Gaps", ""])
    if evaluation.gaps:
        lines.extend(f"- {gap}" for gap in evaluation.gaps)
    else:
        lines.append("- No blocking evidence gap was recorded by the evaluator.")

    lines.extend(["", "## Source Ledger", ""])
    for source in source_items:
        lines.append(f"- `{source.source_id}` {source.title} — {source.publisher} — {source.url}")
    lines.append("")
    return "\n".join(lines)


def _append_next_queries(plan, next_queries: list[str]) -> None:
    from app.services.deep_research.schemas import SearchQuery

    if not next_queries or not plan.lanes:
        return
    target_lane = plan.lanes[-1]
    existing = {_query_key(query.query) for lane in plan.lanes for query in lane.queries}
    for query in next_queries:
        key = _query_key(query)
        if not key or key in existing:
            continue
        target_lane.queries.append(SearchQuery(query=query, lane_id=target_lane.lane_id, rationale="Evaluator gap follow-up."))
        existing.add(key)


def _lane_with_unsearched_queries(lane, searched_queries: set[str]):
    from dataclasses import replace

    queries = [query for query in lane.queries if _query_key(query.query) not in searched_queries]
    return replace(lane, queries=queries)


def _query_key(query: str) -> str:
    return " ".join((query or "").casefold().split())


async def _build_runtime_reasoner(*, agent_id: uuid.UUID, user_id: uuid.UUID):
    try:
        from app.services.deep_research.reasoner import RuntimeDeepResearchReasoner

        return RuntimeDeepResearchReasoner(agent_id=agent_id, user_id=user_id)
    except Exception:
        return None
