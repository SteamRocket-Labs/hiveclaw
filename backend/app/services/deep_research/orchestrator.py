from __future__ import annotations

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


class DeepResearchOrchestrator:
    def __init__(self, tool_invoker: ToolInvoker):
        self.tool_invoker = tool_invoker
        self.evaluator = ResearchEvaluator()

    async def run(self, request: ResearchRequest, *, artifact_dir: str | Path) -> ResearchRun:
        artifact_path = Path(artifact_dir)
        writer = ResearchArtifactWriter(artifact_path)
        ledger = EvidenceLedger(artifact_path)
        writer.write_request(request)
        plan = build_research_plan(request)
        writer.write_plan(plan)
        writer.append_step(_step("plan", "completed", f"Built {len(plan.lanes)} research lane(s)."))

        searcher = ResearchSearcher(self.tool_invoker)
        reader = ResearchReader(self.tool_invoker)
        accepted_sources = 0
        evaluation = None
        seen_source_urls: set[str] = set()

        for round_index in range(1, request.max_rounds + 1):
            writer.append_step(_step("search", "running", f"Starting research round {round_index}."))
            candidates = []
            for lane in plan.lanes:
                remaining = max(request.max_sources - accepted_sources - len(candidates), 0)
                if remaining <= 0:
                    break
                lane_candidates = await searcher.search_lane(lane, max_results=remaining)
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

        if evaluation is None:
            evaluation = self.evaluator.evaluate(request=request, ledger=ledger, round_index=request.max_rounds)

        failed_gates = {gate for gate, state in evaluation.quality_gates.items() if state == "failed"}
        status = (
            "completed"
            if ledger.sources and evaluation.quality_gates.get("attribution") == "passed" and not failed_gates
            else "failed"
        )
        return writer.finalize(request=request, plan=plan, ledger=ledger, evaluation=evaluation, status=status)


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

    return await DeepResearchOrchestrator(invoke_tool).run(request, artifact_dir=artifact_dir)


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
