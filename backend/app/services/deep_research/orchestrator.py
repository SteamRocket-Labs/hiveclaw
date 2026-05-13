from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

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


class DeepResearchSynthesisFailed(Exception):
    """Raised when synthesis cannot produce an analyst-grade deliverable.

    Tier 1-2 contract: never paper over with a Python string-concat fallback.
    The orchestrator catches this, marks the run failed, and writes a short
    failure notice to report.md while preserving the evidence ledger + notes.
    """


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
        source_notes_by_id: dict[str, dict[str, Any]] = {}
        latest_lane_summaries: list[dict[str, Any]] = []

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
                note = await _maybe_summarize_source(self.reasoner, request, source)
                if note is not None:
                    source_notes_by_id[source.source_id] = note
                    writer.append_source_note(note)
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

            round_summaries = _aggregate_lane_summaries(
                plan=plan,
                ledger=ledger,
                source_notes_by_id=source_notes_by_id,
                evaluation=evaluation,
                round_index=round_index,
            )
            for summary in round_summaries:
                writer.append_lane_summary(summary)
            latest_lane_summaries = round_summaries

            if not evaluation.next_queries or accepted_sources >= request.max_sources:
                break
            _append_next_queries(plan, evaluation.next_queries)

        if evaluation is None:
            evaluation = self.evaluator.evaluate(request=request, ledger=ledger, round_index=request.max_rounds)

        report_markdown: str | None
        try:
            report_markdown = await _synthesize_report(
                self.reasoner,
                request,
                plan,
                ledger,
                evaluation,
                source_notes=list(source_notes_by_id.values()),
                lane_summaries=latest_lane_summaries,
            )
        except DeepResearchSynthesisFailed as exc:
            report_markdown = None
            error_message = str(exc) or "Synthesis failed"
            evaluation.quality_gates["synthesis"] = "failed"
            evaluation.gaps.append(
                f"Synthesis failed; no user-deliverable report was produced. {error_message}"
            )
            writer.append_step(
                _step(
                    "synthesize",
                    "failed",
                    "Synthesis failed; preserving ledger and writing failure notice.",
                    {"error": error_message},
                )
            )
        else:
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
            if (
                report_markdown
                and ledger.sources
                and evaluation.quality_gates.get("attribution") == "passed"
                and not failed_gates
            )
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

    for item in extracted[:10]:
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


async def _maybe_summarize_source(
    reasoner: Any | None,
    request: ResearchRequest,
    source,
) -> dict[str, Any] | None:
    """Call reasoner.summarize_source if available; never block the run on failure."""
    if reasoner is None or not hasattr(reasoner, "summarize_source"):
        return None
    try:
        note = await reasoner.summarize_source(request, source)
    except Exception:
        return None
    if not isinstance(note, dict) or not note:
        return None
    note.setdefault("source_id", source.source_id)
    return note


def _aggregate_lane_summaries(
    *,
    plan,
    ledger: EvidenceLedger,
    source_notes_by_id: dict[str, dict[str, Any]],
    evaluation,
    round_index: int,
) -> list[dict[str, Any]]:
    """Deterministic per-lane evidence aggregation. Tier 2 swaps this for an LLM reflector."""
    from app.services.deep_research.schemas import ClaimStatus

    summaries: list[dict[str, Any]] = []
    for lane in plan.lanes:
        lane_sources = [source for source in ledger.sources.values() if source.lane_id == lane.lane_id]
        if not lane_sources:
            continue
        lane_notes = [source_notes_by_id.get(source.source_id) for source in lane_sources]
        lane_notes = [note for note in lane_notes if isinstance(note, dict)]

        key_findings: list[str] = []
        key_entities: set[str] = set()
        key_numbers: set[str] = set()
        limitations: list[str] = []
        for note in lane_notes:
            summary_text = str(note.get("source_bound_summary") or "").strip()
            if summary_text:
                key_findings.append(summary_text)
            for entity in note.get("key_entities") or []:
                key_entities.add(str(entity))
            for number in note.get("key_numbers") or []:
                key_numbers.add(str(number))
            for limitation in note.get("limitations") or []:
                limitations.append(str(limitation))

        contradictions = [
            claim.text
            for claim in ledger.claims
            if claim.status == ClaimStatus.CONTRADICTED
            and any(sid in {s.source_id for s in lane_sources} for sid in claim.source_ids)
        ]

        if len(lane_sources) >= 4:
            strength = "strong"
        elif len(lane_sources) >= 2:
            strength = "moderate"
        else:
            strength = "weak"

        covered_questions = [query.query for query in lane.queries]
        missing_evidence = [
            query
            for query in evaluation.next_queries
            if query and query not in covered_questions
        ]

        summaries.append(
            {
                "lane_id": lane.lane_id,
                "label": lane.label,
                "round_index": round_index,
                "source_count": len(lane_sources),
                "evidence_strength": strength,
                "covered_questions": covered_questions,
                "key_findings": key_findings[:6],
                "key_entities": sorted(key_entities)[:12],
                "key_numbers": sorted(key_numbers)[:12],
                "limitations": limitations[:6],
                "contradictions": contradictions[:6],
                "missing_evidence": missing_evidence[:6],
            }
        )
    return summaries


async def _synthesize_report(
    reasoner: Any | None,
    request: ResearchRequest,
    plan,
    ledger: EvidenceLedger,
    evaluation,
    *,
    source_notes: list[dict[str, Any]] | None = None,
    lane_summaries: list[dict[str, Any]] | None = None,
) -> str:
    """Produce an analyst-grade markdown report or raise DeepResearchSynthesisFailed.

    Tier 1-2: no string-concat fallback. When the LLM cannot produce a deliverable,
    the caller marks the run failed and writes a failure notice — never a pasted dump.
    """
    if reasoner is None or not hasattr(reasoner, "synthesize_report"):
        raise DeepResearchSynthesisFailed(
            "No synthesis reasoner is configured for this run."
        )

    min_chars = _minimum_report_chars(request)
    errors: list[str] = []
    for attempt in range(1, 3):
        try:
            try:
                report = await reasoner.synthesize_report(
                    request,
                    plan,
                    ledger,
                    evaluation,
                    source_notes=source_notes,
                    lane_summaries=lane_summaries,
                )
            except TypeError:
                report = await reasoner.synthesize_report(request, plan, ledger, evaluation)
        except Exception as exc:
            errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(report, str) and report.strip() and len(report.strip()) >= min_chars:
            return report.strip() + "\n"
        if isinstance(report, str) and report.strip():
            errors.append(f"attempt {attempt}: report shorter than {min_chars} chars")
        else:
            errors.append(f"attempt {attempt}: empty or non-string response")

    raise DeepResearchSynthesisFailed("; ".join(errors) or "Synthesis attempts exhausted")


_SOURCE_REF_RE = re.compile(r"\[src_[a-zA-Z0-9_]+\]|`src_[a-zA-Z0-9_]+`|\bsrc_[a-zA-Z0-9_]+")
_FOOTNOTE_REF_RE = re.compile(r"\[\^?\d+\]")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$", re.MULTILINE)

_PROSE_PROPER_RE = re.compile(
    r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]*)+\b"  # Multi-word Title Case (Issuer A, Federal Reserve)
    r"|\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b"             # CamelCase (BlackRock, JPMorgan)
    r"|\b[A-Z]{2,6}\b"                             # Acronyms (SEC, MAS, BUIDL)
)
_PROSE_INNER_TITLECASE_RE = re.compile(r"(?<=[a-z][\s,])[A-Z][a-z]{2,}\b")
_ZH_ENTITY_RE = re.compile(
    r"[一-鿿]{2,8}(?:公司|集团|局|委员会|银行|协会|交易所|证监会|央行|部|院|大学|证券|基金|保险)"
)


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
    if request.mode != "source_ledger_audit" and _looks_like_evidence_list_dump(report):
        return "failed", "Synthesis quality failed: report is an evidence-list dump, not analytical writing."
    if _looks_like_generic_summary(report):
        return "failed", "Synthesis quality failed: report is generic and lacks concrete source-grounded analysis."

    digit_count = _prose_digit_count(report)
    required_digits = _required_digit_count(request)
    if digit_count < required_digits:
        return (
            "failed",
            (
                f"Synthesis quality failed: report has only {digit_count} concrete numbers in prose; "
                f"deep research at mode={request.mode}/depth={request.depth or 'standard'} requires at least {required_digits}."
            ),
        )

    if request.mode != "source_ledger_audit":
        entity_count = _named_entity_count(report)
        required_entities = _required_entity_count(request)
        if entity_count < required_entities:
            return (
                "failed",
                (
                    f"Synthesis quality failed: report references only {entity_count} named entities (companies, "
                    f"regulators, products); analyst-grade synthesis requires at least {required_entities} for "
                    f"mode={request.mode}."
                ),
            )

    return "passed", ""


def _minimum_report_chars(request: ResearchRequest) -> int:
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return 1200
    if depth in {"quick", "light"}:
        return 700
    return 900


def _required_digit_count(request: ResearchRequest) -> int:
    """Mode/depth-aware concrete-number threshold. source_ledger_audit relaxes since
    audit reports center on provenance, not market quantification."""
    if request.mode == "source_ledger_audit":
        return 8
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return 20
    if depth in {"quick", "light"}:
        return 8
    return 12


def _required_entity_count(request: ResearchRequest) -> int:
    """Mode-aware named-entity threshold. topic_deep_dive tolerates narrower coverage
    than industry_research, but both demand concrete actors."""
    if request.mode == "industry_research":
        return 8
    return 6


def _strip_for_prose(report: str) -> str:
    body = _SOURCE_REF_RE.sub("", report)
    body = _FOOTNOTE_REF_RE.sub("", body)
    body = _CODE_FENCE_RE.sub("", body)
    body = _HEADING_RE.sub("", body)
    body = _TABLE_DIVIDER_RE.sub("", body)
    return body


def _prose_digit_count(report: str) -> int:
    return sum(1 for ch in _strip_for_prose(report) if ch.isdigit())


def _named_entity_count(report: str) -> int:
    body = _strip_for_prose(report)
    entities: set[str] = set()
    entities.update(_PROSE_PROPER_RE.findall(body))
    entities.update(_PROSE_INNER_TITLECASE_RE.findall(body))
    entities.update(_ZH_ENTITY_RE.findall(body))
    return len(entities)


def _looks_like_evidence_list_dump(report: str) -> bool:
    """Heuristic: many ledger lines of the form "- `src_xxx`" paired with very few H2
    sections signals a pasted evidence list, not analytical writing."""
    ledger_lines = report.count("\n- `src_")
    section_count = report.count("\n## ")
    return ledger_lines >= 3 and section_count <= 5


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
