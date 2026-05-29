from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.services.deep_research.evaluator import ResearchEvaluator
from app.services.deep_research.extractor import extract_claims_from_source
from app.services.deep_research.language import paragraph_language_consistency, resolve_output_language_code
from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.planner import build_research_plan
from app.services.deep_research.reader import ResearchReader
from app.services.deep_research.reflector import ResearchReflector
from app.services.deep_research.schemas import (
    ResearchRequest,
    ResearchRun,
    ResearchStep,
    SourceType,
    WorkerResult,
    new_id,
)
from app.services.deep_research.searcher import ResearchSearcher, ToolInvoker
from app.services.deep_research.worker import RuntimeResearchWorker
from app.services.deep_research.writer import ResearchArtifactWriter


class DeepResearchSynthesisFailed(Exception):
    """Raised when synthesis cannot produce an analyst-grade deliverable.

    Tier 1-2 contract: never paper over with a Python string-concat fallback.
    The orchestrator catches this, marks the run failed, and writes a short
    failure notice to report.md while preserving the evidence ledger + notes.
    """


class DeepResearchOrchestrator:
    def __init__(self, tool_invoker: ToolInvoker, *, reasoner: Any | None = None, worker_runner: Any | None = None):
        self.tool_invoker = tool_invoker
        self.reasoner = reasoner
        self.worker_runner = worker_runner
        self.evaluator = ResearchEvaluator()

    async def run(self, request: ResearchRequest, *, artifact_dir: str | Path) -> ResearchRun:
        if getattr(request, "controller_mode", False):
            from app.services.deep_research.controller import DeepResearchController

            return await DeepResearchController(self.tool_invoker, reasoner=self.reasoner).run(
                request, artifact_dir=artifact_dir
            )

        if _should_use_worker_path(self.reasoner, self.worker_runner):
            return await self._run_worker_path(request, artifact_dir=artifact_dir)

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
                    writer.append_step(_step("read", "skipped", f"Skipped duplicate candidate URL {candidate.url}."))
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

            reflector = ResearchReflector(self.reasoner)
            decision = await reflector.reflect(
                request=request,
                plan=plan,
                ledger=ledger,
                round_index=round_index,
                source_notes=list(source_notes_by_id.values()),
                lane_summaries=latest_lane_summaries,
                evaluator_gaps=evaluation.gaps,
                evaluator_next_queries=evaluation.next_queries,
            )
            writer.append_reflection(decision.to_jsonable())

            if accepted_sources >= request.max_sources:
                break
            if decision.stop_signal:
                break
            follow_up_queries = [q["query"] for q in decision.next_queries if q.get("query")]
            if not follow_up_queries:
                break
            _append_next_queries(plan, follow_up_queries)

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
            report_markdown = _apply_footnotes(report_markdown, ledger)
        except DeepResearchSynthesisFailed as exc:
            report_markdown = None
            error_message = str(exc) or "Synthesis failed"
            evaluation.quality_gates["synthesis"] = "failed"
            evaluation.gaps.append(f"Synthesis failed; no user-deliverable report was produced. {error_message}")
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

    async def _run_worker_path(self, request: ResearchRequest, *, artifact_dir: str | Path) -> ResearchRun:
        artifact_path = Path(artifact_dir)
        writer = ResearchArtifactWriter(artifact_path)
        ledger = EvidenceLedger(artifact_path)
        writer.write_request(request)
        plan = build_research_plan(request)
        plan = await _maybe_refine_plan(self.reasoner, request, plan)
        writer.write_plan(plan)
        writer.append_step(
            _step("plan", "completed", f"Built {len(plan.lanes)} research lane(s) for v2 worker fan-out.")
        )

        source_notes_by_id: dict[str, dict[str, Any]] = {}
        if getattr(request, "worker_topics", None):
            # P-A: user already confirmed worker topics in the plan stage — use them as-is.
            worker_topics = list(request.worker_topics)[: _topic_budget(request, plan)]
            topic_source = "user_confirmed"
        else:
            worker_topics = await _worker_topics(self.reasoner, request, plan)
            topic_source = "planner"
        writer.append_step(
            _step(
                "worker_plan",
                "completed",
                f"Selected {len(worker_topics)} orchestrator-worker topic(s).",
                {"topics": worker_topics, "topic_source": topic_source},
            )
        )

        runner = self.worker_runner or _build_worker_runner_from_reasoner(self.reasoner)
        if runner is None:
            evaluation = self.evaluator.evaluate(request=request, ledger=ledger, round_index=0)
            evaluation.quality_gates["worker"] = "failed"
            evaluation.gaps.append("Deep Research v2 worker path is enabled but no worker runner could be built.")
            writer.append_evaluation(evaluation)
            return writer.finalize(
                request=request,
                plan=plan,
                ledger=ledger,
                evaluation=evaluation,
                status="failed",
                report_markdown=None,
            )

        worker_results = await _run_worker_fanout(
            runner,
            worker_topics,
            request=request,
            max_concurrency=min(max(1, request.concurrency), 3),
            deadline_seconds=request.deadline_seconds,
        )

        # F1 (RC1): select sources fairly across workers instead of letting the first worker
        # fill the whole budget. The worker already digested each page, so we do NOT spend a
        # per-source LLM call to re-summarize here (that serial loop was the main latency sink);
        # claims are extracted deterministically and the worker digest is the synthesizer's substrate.
        for result, source in _select_sources_round_robin(worker_results, request.max_sources):
            lane_id = source.lane_id or _lane_id_for_worker_topic(plan, result.topic)
            ledger_source = ledger.add_source(
                url=source.url,
                title=source.title,
                publisher=source.publisher,
                source_type=source.source_type
                if source.source_type != SourceType.UNKNOWN
                else _source_type_for_lane(lane_id),
                content=source.content,
                published_at=source.published_at,
                lane_id=lane_id,
                query=source.query or result.topic,
                fetch_tool=source.fetch_tool,
                source_id=source.source_id,
            )
            source.source_id = ledger_source.source_id
            source.source_type = ledger_source.source_type
            source.lane_id = ledger_source.lane_id
            source.query = ledger_source.query
            source.evidence_tier = ledger_source.evidence_tier
            source.evidence_grade = ledger_source.evidence_grade
            extract_claims_from_source(ledger, ledger_source)

        for result in worker_results:
            writer.append_worker_report(result)
            writer.append_step(
                _step(
                    "worker",
                    result.status,
                    f"Worker completed topic: {result.topic}",
                    {
                        "topic": result.topic,
                        "source_count": len(result.sources),
                        "tokens_used": result.tokens_used,
                        "error": result.error,
                    },
                )
            )

        evaluation = self.evaluator.evaluate(request=request, ledger=ledger, round_index=1)
        if not any(result.status == "ok" for result in worker_results):
            evaluation.quality_gates["worker"] = "failed"
            evaluation.gaps.append("No Deep Research worker completed successfully.")
        writer.append_evaluation(evaluation)
        writer.append_step(
            _step(
                "evaluate",
                "completed",
                "Evaluated v2 worker evidence ledger before digest synthesis.",
                {"quality_gates": evaluation.quality_gates, "gaps": evaluation.gaps},
            )
        )

        latest_lane_summaries = _aggregate_lane_summaries(
            plan=plan,
            ledger=ledger,
            source_notes_by_id=source_notes_by_id,
            evaluation=evaluation,
            round_index=1,
        )
        for summary in latest_lane_summaries:
            writer.append_lane_summary(summary)

        devils_advocate = await _maybe_devils_advocate(
            self.reasoner,
            request,
            plan,
            ledger,
            worker_results=worker_results,
            lane_summaries=latest_lane_summaries,
        )
        if devils_advocate:
            writer.append_devils_advocate(devils_advocate)
            writer.append_step(
                _step(
                    "devils_advocate",
                    "completed",
                    "Ran adversarial pre-synthesis review (cherry-picking, counter-argument, gaps).",
                    {"strongest_counter_argument": str(devils_advocate.get("strongest_counter_argument") or "")[:280]},
                )
            )

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
                worker_results=worker_results,
                devils_advocate=devils_advocate,
            )
            report_markdown = _apply_footnotes(report_markdown, ledger)
        except DeepResearchSynthesisFailed as exc:
            report_markdown = None
            error_message = str(exc) or "Synthesis failed"
            evaluation.quality_gates["synthesis"] = "failed"
            evaluation.gaps.append(f"Synthesis failed; no user-deliverable report was produced. {error_message}")
            writer.append_step(
                _step(
                    "synthesize",
                    "failed",
                    "V2 digest synthesis failed; preserving worker reports and ledger.",
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
                    "Synthesized final report from worker digests and checked citation integrity.",
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


async def _default_tool_executor(
    tool_name: str, arguments: dict[str, Any], agent_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    from app.services.agent_tools import execute_tool

    return await execute_tool(tool_name, arguments, agent_id=agent_id, user_id=user_id)


def _step(phase: str, status: str, message: str, detail: dict[str, Any] | None = None) -> ResearchStep:
    return ResearchStep(step_id=new_id("step"), phase=phase, status=status, message=message, detail=detail or {})


def _should_use_worker_path(reasoner: Any | None, worker_runner: Any | None) -> bool:
    return (
        reasoner is not None
        and hasattr(reasoner, "synthesize_from_digests")
        and (worker_runner is not None or hasattr(reasoner, "agent_id"))
    )


def _build_worker_runner_from_reasoner(reasoner: Any | None):
    agent_id = getattr(reasoner, "agent_id", None)
    user_id = getattr(reasoner, "user_id", None)
    if not isinstance(agent_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        return None
    return RuntimeResearchWorker(agent_id=agent_id, user_id=user_id)


def _short_question(question: str, *, limit: int = 200) -> str:
    """Compress a long mega-question into a short background line so worker prompts stay
    lane-focused instead of pasting all 10 dimensions into every worker (F3/RC3)."""
    text = " ".join((question or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


async def _worker_topics(reasoner: Any | None, request: ResearchRequest, plan) -> list[str]:
    if reasoner is not None and hasattr(reasoner, "decide_next"):
        try:
            decision = await reasoner.decide_next(request=request, plan=plan)
        except Exception:
            decision = None
        topics = _topics_from_decision(decision)
        if topics:
            return topics[: _topic_budget(request, plan)]

    topics: list[str] = []
    background = _short_question(request.question)
    for lane in plan.lanes:
        queries = "; ".join(query.query for query in lane.queries[:4] if query.query)
        # F3 (RC3): focus each worker on its own lane. Pasting the full 10-dimension question into
        # every worker made each one chase the whole topic and blow the token budget.
        topic = (
            f"Research lane: {lane.label or lane.lane_id}\n"
            f"Goal: {lane.goal or 'collect source-grounded evidence'}\n"
            f"Focus queries: {queries or lane.label or background}\n"
            f"Background (stay on this lane, do not chase the whole question): {background}"
        )
        topics.append(topic)
    return topics[: _topic_budget(request, plan)]


def _topic_budget(request: ResearchRequest, plan) -> int:
    lane_count = len(getattr(plan, "lanes", []) or [])
    return max(1, min(request.max_sources, max(lane_count, 3), 6))


def _topics_from_decision(decision: Any) -> list[str]:
    if not isinstance(decision, dict):
        return []
    raw_topics = decision.get("topics") or decision.get("worker_topics") or decision.get("next_topics")
    if not isinstance(raw_topics, list):
        return []
    topics = [str(topic).strip() for topic in raw_topics if str(topic or "").strip()]
    return list(dict.fromkeys(topics))


def _lane_id_for_worker_topic(plan, topic: str) -> str:
    topic_lower = (topic or "").casefold()
    for lane in getattr(plan, "lanes", []) or []:
        if lane.lane_id.casefold() in topic_lower or lane.label.casefold() in topic_lower:
            return lane.lane_id
    lanes = getattr(plan, "lanes", []) or []
    return lanes[0].lane_id if lanes else ""


async def _run_worker_fanout(
    runner: Any,
    topics: list[str],
    *,
    request: ResearchRequest,
    max_concurrency: int,
    deadline_seconds: int | None,
) -> list[WorkerResult]:
    if not topics:
        return []
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def run_one(topic: str) -> WorkerResult:
        async with semaphore:
            try:
                coroutine = runner.run(topic, request=request)
                if deadline_seconds:
                    return await asyncio.wait_for(coroutine, timeout=deadline_seconds)
                return await coroutine
            except asyncio.TimeoutError:
                return WorkerResult(topic=topic, intermediate_report="", status="failed", error="worker timed out")
            except Exception as exc:
                return WorkerResult(
                    topic=topic,
                    intermediate_report="",
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

    results = await asyncio.gather(*(run_one(topic) for topic in topics), return_exceptions=False)
    return [result for result in results if isinstance(result, WorkerResult)]


def _select_sources_round_robin(worker_results: list[WorkerResult], max_sources: int) -> list[tuple[WorkerResult, Any]]:
    """Fair cross-worker source selection (F1/RC1).

    Round-robin one source per worker per pass so the first worker cannot fill the whole
    budget and squeeze later lanes out (the production incident: 64 fetched sources collapsed
    to 8, all from the first two lanes). Deduplicates by url across workers and keeps consuming
    until the budget is met or every worker is exhausted.
    """
    queues = [list(result.sources) for result in worker_results]
    cursors = [0] * len(queues)
    seen_urls: set[str] = set()
    selected: list[tuple[WorkerResult, Any]] = []
    while len(selected) < max_sources:
        progressed = False
        for index, queue in enumerate(queues):
            if len(selected) >= max_sources:
                break
            while cursors[index] < len(queue):
                source = queue[cursors[index]]
                cursors[index] += 1
                if not source.url or source.url in seen_urls:
                    continue
                seen_urls.add(source.url)
                selected.append((worker_results[index], source))
                progressed = True
                break
        if not progressed:
            break
    return selected


def _source_type_for_lane(lane_id: str) -> SourceType:
    # F6/RC6: fuzzy-match planner lane ids (e.g. "market_data", "protocol") so worker sources get a
    # real source type instead of all collapsing to UNKNOWN→tier3.
    lane = (lane_id or "").casefold()
    if any(key in lane for key in ("regul", "complian", "legal", "policy")):
        return SourceType.REGULATORY
    if any(key in lane for key in ("official", "issuer", "primary", "filing")):
        return SourceType.PRIMARY
    if any(key in lane for key in ("market", "data", "metric", "stat")):
        return SourceType.DATASET
    if any(key in lane for key in ("tech", "protocol", "mechanism", "engineer")):
        return SourceType.TECHNICAL
    if any(key in lane for key in ("secondary", "news", "press", "media", "competitor")):
        return SourceType.SECONDARY
    return SourceType.UNKNOWN


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
            contradiction_group=item.get("contradiction_group")
            if isinstance(item.get("contradiction_group"), str)
            else None,
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


async def _maybe_devils_advocate(
    reasoner: Any | None,
    request: ResearchRequest,
    plan,
    ledger: EvidenceLedger,
    *,
    worker_results: list[WorkerResult],
    lane_summaries: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Run the adversarial pre-synthesis pass if the reasoner exposes it; never block the run on failure."""
    if reasoner is None or not hasattr(reasoner, "devils_advocate_review"):
        return None
    try:
        review = await reasoner.devils_advocate_review(
            request, plan, ledger, worker_results=worker_results, lane_summaries=lane_summaries
        )
    except Exception:
        return None
    if not isinstance(review, dict) or not review:
        return None
    return review


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

        # F6/RC7: the worker path has no source_notes, so backfill lane findings from the captured
        # sources themselves — otherwise lane summaries are hollow (production: key_findings = 0).
        if not key_findings:
            for source in lane_sources[:6]:
                snippet = " ".join((source.content or "").split())
                if not snippet:
                    continue
                lead = snippet.split(". ")[0][:200]
                key_findings.append(f"{source.title or source.publisher}: {lead}")

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
        missing_evidence = [query for query in evaluation.next_queries if query and query not in covered_questions]

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
    worker_results: list[WorkerResult] | None = None,
    devils_advocate: dict[str, Any] | None = None,
) -> str:
    """Produce an analyst-grade markdown report or raise DeepResearchSynthesisFailed.

    Honesty contract (Tier 1-2): NO string-concat / stitched fallback. The v2 worker path
    synthesizes ONLY from compressed worker digests via `synthesize_from_digests`; if that
    cannot produce a deliverable it fails loudly so the caller writes a failure notice rather
    than a pasted dump. The non-worker (linear) path uses the single-call `synthesize_report`.
    """
    if reasoner is None:
        raise DeepResearchSynthesisFailed("No synthesis reasoner is configured for this run.")

    min_chars = _minimum_report_chars(request)
    errors: list[str] = []

    if worker_results and hasattr(reasoner, "synthesize_from_digests"):
        best_partial = ""
        for attempt in range(1, 3):
            try:
                try:
                    report = await reasoner.synthesize_from_digests(
                        request,
                        plan,
                        ledger,
                        evaluation,
                        worker_results=worker_results,
                        source_notes=source_notes,
                        lane_summaries=lane_summaries,
                        devils_advocate=devils_advocate,
                    )
                except TypeError:
                    report = await reasoner.synthesize_from_digests(
                        request,
                        plan,
                        ledger,
                        evaluation,
                        worker_results=worker_results,
                    )
            except Exception as exc:
                errors.append(f"digest-stage attempt {attempt}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(report, str) and report.strip() and len(report.strip()) >= min_chars:
                return report.strip() + "\n"
            if isinstance(report, str) and report.strip():
                errors.append(f"digest-stage attempt {attempt}: report shorter than {min_chars} chars")
                if len(report.strip()) > len(best_partial):
                    best_partial = report.strip()
            else:
                errors.append(f"digest-stage attempt {attempt}: empty or non-string response")
        # F5: full synthesis fell short. If real evidence exists and the writer produced a usable
        # (if short) draft, deliver a coverage-aware narrowed report rather than failing the whole
        # run. Still the writer's own synthesis — no stitched fallback.
        if best_partial and ledger.sources and len(best_partial) >= _narrowed_minimum_chars(request):
            return _with_coverage_notice(best_partial, plan, ledger)
        raise DeepResearchSynthesisFailed("; ".join(errors) or "Digest synthesis produced no deliverable")

    if not hasattr(reasoner, "synthesize_report"):
        raise DeepResearchSynthesisFailed("; ".join(errors) or "Reasoner exposes no synthesis path")

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
            errors.append(f"single-stage attempt {attempt}: {type(exc).__name__}: {exc}")
            continue
        if isinstance(report, str) and report.strip() and len(report.strip()) >= min_chars:
            return report.strip() + "\n"
        if isinstance(report, str) and report.strip():
            errors.append(f"single-stage attempt {attempt}: report shorter than {min_chars} chars")
        else:
            errors.append(f"single-stage attempt {attempt}: empty or non-string response")

    raise DeepResearchSynthesisFailed("; ".join(errors) or "Synthesis attempts exhausted")


_INLINE_SOURCE_TOKEN_RE = re.compile(
    r"\[(src_[a-zA-Z0-9_]{8,})\]"  # [src_ab12]
    r"|(?<![`\-])(src_[a-zA-Z0-9_]{8,})\b"  # bare src_ab12 in prose
)


def _apply_footnotes(report: str | None, ledger: EvidenceLedger) -> str | None:
    """Tier 2-5: rewrite inline [src_xxx] / bare src_xxx prose references as [^N]
    footnote markers and append a `## Footnotes` block at the end of the report.
    Backtick-quoted `src_xxx` entries (used inside `## Source Ledger`) are left intact
    so the ledger keeps its native form.
    """
    if not report or not ledger.sources:
        return report

    # RC12: neutralize hallucinated citations (ids not in the ledger) before footnote
    # conversion, so a few fabricated refs do not one-vote-veto an otherwise grounded report.
    report = _strip_unknown_source_refs(report, ledger)

    used: dict[str, int] = {}

    def _replace(match: re.Match) -> str:
        sid = match.group(1) or match.group(2)
        if sid not in ledger.sources:
            return match.group(0)
        if sid not in used:
            used[sid] = len(used) + 1
        return f"[^{used[sid]}]"

    converted = _INLINE_SOURCE_TOKEN_RE.sub(_replace, report)

    if not used:
        return converted

    if "## Footnotes" in converted:
        return converted

    lines = ["", "## Footnotes", ""]
    for sid, num in sorted(used.items(), key=lambda kv: kv[1]):
        source = ledger.sources[sid]
        title = source.title or "Source"
        publisher = source.publisher or "Unknown publisher"
        url = source.url or ""
        lines.append(f"[^{num}]: {title} — {publisher} — {url}")
    return converted.rstrip() + "\n\n" + "\n".join(lines) + "\n"


_SOURCE_REF_RE = re.compile(r"\[src_[a-zA-Z0-9_]+\]|`src_[a-zA-Z0-9_]+`|\bsrc_[a-zA-Z0-9_]+")
_FOOTNOTE_REF_RE = re.compile(r"\[\^?\d+\]")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$", re.MULTILINE)

_PROSE_PROPER_RE = re.compile(
    r"\b[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]*)+\b"  # Multi-word Title Case (Issuer A, Federal Reserve)
    r"|\b[A-Z][a-z]+[A-Z][a-zA-Z]+\b"  # CamelCase (BlackRock, JPMorgan)
    r"|\b[A-Z]{2,6}\b"  # Acronyms (SEC, MAS, BUIDL)
)
_PROSE_INNER_TITLECASE_RE = re.compile(r"(?<=[a-z][\s,])[A-Z][a-z]{2,}\b")
_ZH_ENTITY_RE = re.compile(
    r"[一-鿿]{2,8}(?:公司|集团|局|委员会|银行|协会|交易所|证监会|央行|部|院|大学|证券|基金|保险)"
)


def _evaluate_synthesis_quality(
    report: str | None, *, request: ResearchRequest, ledger: EvidenceLedger
) -> tuple[str, str]:
    if not report:
        return "failed", "Synthesis quality failed: report is too short for a deep research deliverable."
    if not ledger.sources:
        return "failed", "Synthesis quality failed: no fetched source is available for source-grounded synthesis."
    target_language = resolve_output_language_code(request)
    # F5: a narrowed report (limited evidence + explicit coverage notice) is held to a lower floor
    # and skips the digit/entity density gates that assume full-coverage synthesis.
    is_narrowed = _COVERAGE_NOTICE_MARKER in report
    language_ok, foreign_paragraphs = paragraph_language_consistency(report, target_language)
    if not language_ok:
        return (
            "failed",
            (
                f"Synthesis quality failed: report mixes languages — {foreign_paragraphs} paragraph(s) are not in "
                f"the target output language ({target_language}). Rewrite the whole report in one language."
            ),
        )
    unknown_refs = _unknown_source_refs(report, ledger)
    if unknown_refs:
        return (
            "failed",
            "Synthesis quality failed: report cites unknown source ids not in the evidence ledger: "
            + ", ".join(unknown_refs[:8]),
        )
    floor = _narrowed_minimum_chars(request) if is_narrowed else _minimum_report_chars(request)
    if len(report.strip()) < floor:
        return "failed", "Synthesis quality failed: report is too short for a deep research deliverable."
    cited_source_ids = {source_id for source_id in ledger.sources if source_id in report}
    footnote_markers = len(re.findall(r"\[\^\d+\]", report))
    required_citations = min(max(2, len(ledger.sources) // 2), len(ledger.sources))
    # Tier 2-5: footnote markers count as citations too — the synthesis path now rewrites
    # inline [src_xxx] to [^N] and emits a Footnotes table.
    if max(len(cited_source_ids), footnote_markers) < required_citations:
        return (
            "failed",
            "Synthesis quality failed: report does not cite enough source ids or footnotes from the evidence ledger.",
        )
    required_sections = ("Executive", "Findings", "Source")
    if not all(section.casefold() in report.casefold() for section in required_sections):
        return "failed", "Synthesis quality failed: report is missing executive, findings, or source-grounded sections."
    if request.mode != "source_ledger_audit" and _looks_like_evidence_list_dump(report):
        return "failed", "Synthesis quality failed: report is an evidence-list dump, not analytical writing."
    if _looks_like_generic_summary(report):
        return "failed", "Synthesis quality failed: report is generic and lacks concrete source-grounded analysis."

    digit_count = _prose_digit_count(report)
    required_digits = _required_digit_count(request)
    if not is_narrowed and digit_count < required_digits:
        return (
            "failed",
            (
                f"Synthesis quality failed: report has only {digit_count} concrete numbers in prose; "
                f"deep research at mode={request.mode}/depth={request.depth or 'standard'} requires at least {required_digits}."
            ),
        )

    if not is_narrowed and request.mode != "source_ledger_audit":
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


def _unknown_source_refs(report: str, ledger: EvidenceLedger) -> list[str]:
    refs = set(re.findall(r"src_[a-zA-Z0-9_]+", report or ""))
    return sorted(ref for ref in refs if ref not in ledger.sources)


def _strip_unknown_source_refs(report: str, ledger: EvidenceLedger) -> str:
    """RC12: neutralize hallucinated citations — remove [src_xxx] / `src_xxx` / bare src_xxx
    tokens whose id is not in the evidence ledger (model-fabricated refs), keeping the prose.
    A few invented ids then no longer fail an otherwise source-grounded report; genuine ledger
    refs are untouched (later converted to footnotes). If too few real refs remain, the
    citation-sufficiency gate still fails the report downstream."""
    unknown = {ref for ref in re.findall(r"src_[a-zA-Z0-9_]+", report or "") if ref not in ledger.sources}
    if not unknown:
        return report
    cleaned = report
    for ref in unknown:
        cleaned = re.sub(rf"\[\s*{re.escape(ref)}\s*\]", "", cleaned)  # [src_xxx]
        cleaned = cleaned.replace(f"`{ref}`", "")  # `src_xxx`
        cleaned = re.sub(rf"\b{re.escape(ref)}\b", "", cleaned)  # bare src_xxx
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)  # collapse doubled spaces left by removal
    cleaned = re.sub(r"\s+([.,;)])", r"\1", cleaned)  # tidy orphaned space before punctuation
    return cleaned


def _minimum_report_chars(request: ResearchRequest) -> int:
    depth = (request.depth or "").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return 1200
    if depth in {"quick", "light"}:
        return 700
    return 900


_COVERAGE_NOTICE_MARKER = "**Coverage notice:**"


def _narrowed_minimum_chars(request: ResearchRequest) -> int:
    """F5: a narrowed (coverage-limited) report is held to a lower floor than a full report —
    honestly reporting limited evidence should not be punished as an outright failure."""
    return max(400, _minimum_report_chars(request) // 3)


def _with_coverage_notice(report: str, plan, ledger: EvidenceLedger) -> str:
    """Prefix a narrowed report with an explicit coverage notice naming the lanes that still have
    no usable source (F5/RC4). Keeps the run honest: partial coverage is stated, not hidden."""
    covered = {source.lane_id for source in ledger.sources.values() if source.lane_id}
    uncovered = [lane.label or lane.lane_id for lane in plan.lanes if lane.lane_id not in covered]
    lines = [
        f"> {_COVERAGE_NOTICE_MARKER} Evidence was limited, so this is a narrowed report scoped to the "
        f"{len(ledger.sources)} source(s) that returned usable content."
    ]
    if uncovered:
        lines.append("> Research lanes still uncovered: " + ", ".join(uncovered) + ".")
    return "\n".join(lines) + "\n\n" + report.strip() + "\n"


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
        target_lane.queries.append(
            SearchQuery(query=query, lane_id=target_lane.lane_id, rationale="Evaluator gap follow-up.")
        )
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
