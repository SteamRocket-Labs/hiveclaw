"""Tier 3-1 DeepResearchController — LLM-as-controller research loop.

Replaces the fixed `for round in range(max_rounds)` pipeline with a token-budget-
driven loop where the LLM decides the next action each step (search / visit /
reflect / answer). At 85% of the token budget the loop enters Beast Mode and
forces a synthesis pass using the current ledger.

Every step is recorded to `controller_trace.jsonl` so the run can be replayed
and the controller's choices audited. The controller is opt-in via
`ResearchRequest.controller_mode`; the orchestrator dispatches here when set,
otherwise the Tier 1/2 path runs unchanged.
"""

from __future__ import annotations

import json
import logging
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
    SearchCandidate,
    SourceType,
    new_id,
    to_jsonable,
    utc_now,
)
from app.services.deep_research.searcher import ResearchSearcher
from app.services.deep_research.writer import ResearchArtifactWriter

logger = logging.getLogger(__name__)


_DEFAULT_TOKEN_BUDGET = 200_000
_BUDGET_BEAST_THRESHOLD = 0.85
_MAX_CONTROLLER_STEPS = 32
_BEAST_MODE_ESTIMATED_TOKENS = 12_000
_DEFAULT_STEP_ESTIMATED_TOKENS = 1_500


class DeepResearchController:
    def __init__(self, tool_invoker, *, reasoner: Any | None = None):
        self.tool_invoker = tool_invoker
        self.reasoner = reasoner
        self.evaluator = ResearchEvaluator()

    async def run(self, request: ResearchRequest, *, artifact_dir: str | Path) -> ResearchRun:
        # Imports kept local to avoid circular import with orchestrator helpers.
        from app.services.deep_research.orchestrator import (
            DeepResearchSynthesisFailed,
            _aggregate_lane_summaries,
            _apply_footnotes,
            _evaluate_synthesis_quality,
            _maybe_extract_claims,
            _maybe_refine_plan,
            _maybe_summarize_source,
            _synthesize_report,
        )

        artifact_path = Path(artifact_dir)
        writer = ResearchArtifactWriter(artifact_path)
        ledger = EvidenceLedger(artifact_path)
        writer.write_request(request)

        plan = build_research_plan(request)
        plan = await _maybe_refine_plan(self.reasoner, request, plan)
        writer.write_plan(plan)
        writer.append_step(
            _step("plan", "completed", f"Controller loop primed with {len(plan.lanes)} lane(s).")
        )

        searcher = ResearchSearcher(self.tool_invoker)
        reader = ResearchReader(self.tool_invoker)
        trace_path = artifact_path / "controller_trace.jsonl"

        token_budget = request.token_budget or _DEFAULT_TOKEN_BUDGET
        beast_threshold = int(token_budget * _BUDGET_BEAST_THRESHOLD)
        token_used = 0

        source_notes_by_id: dict[str, dict[str, Any]] = {}
        seen_source_urls: set[str] = set()
        gaps_queue: list[str] = [request.question]
        controller_beast_mode = False
        answer_requested = False
        last_step_index = 0

        for step_index in range(1, _MAX_CONTROLLER_STEPS + 1):
            last_step_index = step_index
            if token_used >= beast_threshold:
                controller_beast_mode = True
                writer.append_step(
                    _step("controller", "beast_mode", "Token budget 85% threshold reached.")
                )
                _record_trace(
                    trace_path,
                    _trace_entry(
                        step_index=step_index,
                        action_type="beast_mode",
                        rationale="Budget exhausted; forcing final synthesis with current ledger.",
                        decision={},
                        outputs={"reason": "budget_exhausted"},
                        tokens_estimated=_BEAST_MODE_ESTIMATED_TOKENS,
                        role="controller",
                    ),
                )
                token_used += _BEAST_MODE_ESTIMATED_TOKENS
                break

            decision = await self._decide_action(
                request=request,
                plan=plan,
                ledger=ledger,
                gaps_queue=gaps_queue,
                source_notes_by_id=source_notes_by_id,
                step_index=step_index,
                token_used=token_used,
                token_budget=token_budget,
            )
            action_type = str(decision.get("type") or "").strip().lower() or "reflect"

            outputs, executed_tokens, role = await self._execute_action(
                action_type=action_type,
                decision=decision,
                request=request,
                plan=plan,
                ledger=ledger,
                searcher=searcher,
                reader=reader,
                writer=writer,
                source_notes_by_id=source_notes_by_id,
                seen_source_urls=seen_source_urls,
                gaps_queue=gaps_queue,
                step_index=step_index,
                maybe_extract_claims=_maybe_extract_claims,
                maybe_summarize_source=_maybe_summarize_source,
            )

            _record_trace(
                trace_path,
                _trace_entry(
                    step_index=step_index,
                    action_type=action_type,
                    rationale=str(decision.get("rationale") or ""),
                    decision=decision,
                    outputs=outputs,
                    tokens_estimated=executed_tokens,
                    role=role,
                ),
            )
            token_used += executed_tokens

            if action_type == "answer":
                answer_requested = True
                break
            if len(ledger.sources) >= request.max_sources:
                writer.append_step(
                    _step("controller", "source_cap", f"Reached max_sources={request.max_sources}.")
                )
                break

        evaluation = self.evaluator.evaluate(
            request=request, ledger=ledger, round_index=last_step_index or 1
        )
        writer.append_evaluation(evaluation)
        writer.append_step(
            _step(
                "evaluate",
                "completed",
                "Final evaluation after controller loop.",
                {"quality_gates": evaluation.quality_gates, "gaps": evaluation.gaps},
            )
        )

        latest_lane_summaries = _aggregate_lane_summaries(
            plan=plan,
            ledger=ledger,
            source_notes_by_id=source_notes_by_id,
            evaluation=evaluation,
            round_index=last_step_index or 1,
        )
        for summary in latest_lane_summaries:
            writer.append_lane_summary(summary)

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
            evaluation.gaps.append(
                f"Synthesis failed; no user-deliverable report was produced. {error_message}"
            )
            writer.append_step(
                _step(
                    "synthesize",
                    "failed",
                    "Synthesis failed; preserving ledger and writing failure notice.",
                    {"error": error_message, "controller_beast_mode": controller_beast_mode},
                )
            )
        else:
            synthesis_gate, synthesis_gap = _evaluate_synthesis_quality(
                report_markdown, request=request, ledger=ledger
            )
            evaluation.quality_gates["synthesis"] = synthesis_gate
            if synthesis_gap:
                evaluation.gaps.append(synthesis_gap)
            writer.append_step(
                _step(
                    "synthesize",
                    synthesis_gate,
                    "Controller-driven synthesis complete.",
                    {
                        "synthesis_gate": synthesis_gate,
                        "controller_beast_mode": controller_beast_mode,
                        "answer_requested": answer_requested,
                    },
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

    async def _decide_action(
        self,
        *,
        request: ResearchRequest,
        plan,
        ledger: EvidenceLedger,
        gaps_queue: list[str],
        source_notes_by_id: dict[str, dict[str, Any]],
        step_index: int,
        token_used: int,
        token_budget: int,
    ) -> dict[str, Any]:
        if self.reasoner is not None and hasattr(self.reasoner, "decide_controller_action"):
            try:
                payload = {
                    "step_index": step_index,
                    "token_used": token_used,
                    "token_budget": token_budget,
                    "current_question": gaps_queue[0] if gaps_queue else request.question,
                    "open_gaps": gaps_queue[:5],
                    "ledger_summary": {
                        "source_count": len(ledger.sources),
                        "claim_count": len(ledger.claims),
                        "lanes_covered": sorted(
                            {source.lane_id for source in ledger.sources.values() if source.lane_id}
                        ),
                    },
                    "source_notes": list(source_notes_by_id.values())[:10],
                }
                decision = await self.reasoner.decide_controller_action(request=request, plan=plan, **payload)
                if isinstance(decision, dict) and decision.get("type"):
                    return decision
            except Exception as exc:
                logger.warning(
                    "[Controller] decide_controller_action failed at step=%d: %s", step_index, exc
                )

        # Deterministic fallback: search until min_sources met, then reflect, then answer.
        if len(ledger.sources) < max(2, request.max_sources // 2):
            return {
                "type": "search",
                "rationale": "Fallback: not enough sources yet — running planned searches.",
                "queries": [query.query for lane in plan.lanes for query in lane.queries][:3],
            }
        if step_index < _MAX_CONTROLLER_STEPS // 2:
            return {
                "type": "reflect",
                "rationale": "Fallback: midway checkpoint — reflect on current ledger.",
            }
        return {
            "type": "answer",
            "rationale": "Fallback: late in loop — proceed to synthesis.",
        }

    async def _execute_action(
        self,
        *,
        action_type: str,
        decision: dict[str, Any],
        request: ResearchRequest,
        plan,
        ledger: EvidenceLedger,
        searcher: ResearchSearcher,
        reader: ResearchReader,
        writer: ResearchArtifactWriter,
        source_notes_by_id: dict[str, dict[str, Any]],
        seen_source_urls: set[str],
        gaps_queue: list[str],
        step_index: int,
        maybe_extract_claims,
        maybe_summarize_source,
    ) -> tuple[dict[str, Any], int, str]:
        if action_type == "search":
            queries = _string_list(decision.get("queries"))
            outputs = await self._dispatch_search(
                queries=queries,
                request=request,
                plan=plan,
                ledger=ledger,
                searcher=searcher,
                reader=reader,
                writer=writer,
                source_notes_by_id=source_notes_by_id,
                seen_source_urls=seen_source_urls,
                step_index=step_index,
                maybe_extract_claims=maybe_extract_claims,
                maybe_summarize_source=maybe_summarize_source,
            )
            return outputs, _DEFAULT_STEP_ESTIMATED_TOKENS, "researcher"

        if action_type == "visit":
            urls = _string_list(decision.get("urls"))
            outputs = await self._dispatch_visit(
                urls=urls,
                request=request,
                ledger=ledger,
                reader=reader,
                writer=writer,
                source_notes_by_id=source_notes_by_id,
                seen_source_urls=seen_source_urls,
                step_index=step_index,
                maybe_extract_claims=maybe_extract_claims,
                maybe_summarize_source=maybe_summarize_source,
            )
            return outputs, _DEFAULT_STEP_ESTIMATED_TOKENS, "researcher"

        if action_type == "reflect":
            outputs = await self._dispatch_reflect(
                request=request,
                plan=plan,
                ledger=ledger,
                source_notes_by_id=source_notes_by_id,
                gaps_queue=gaps_queue,
                step_index=step_index,
            )
            return outputs, _DEFAULT_STEP_ESTIMATED_TOKENS // 2, "critic"

        if action_type == "answer":
            writer.append_step(
                _step("controller", "answer", "LLM controller requested final synthesis.")
            )
            return {"reason": "controller_requested_answer"}, _DEFAULT_STEP_ESTIMATED_TOKENS, "writer"

        logger.info("[Controller] Unknown action_type=%s; recording as noop", action_type)
        return {"warning": f"unknown action_type={action_type}"}, 0, "controller"

    async def _dispatch_search(
        self,
        *,
        queries: list[str],
        request: ResearchRequest,
        plan,
        ledger: EvidenceLedger,
        searcher: ResearchSearcher,
        reader: ResearchReader,
        writer: ResearchArtifactWriter,
        source_notes_by_id: dict[str, dict[str, Any]],
        seen_source_urls: set[str],
        step_index: int,
        maybe_extract_claims,
        maybe_summarize_source,
    ) -> dict[str, Any]:
        if not queries:
            return {"queries": [], "candidates": 0, "accepted": 0, "warning": "no queries provided"}

        target_lane = plan.lanes[-1] if plan.lanes else None
        lane_id = target_lane.lane_id if target_lane else "controller"
        candidates: list[SearchCandidate] = []
        for query in queries[:5]:
            raw = await self.tool_invoker("web_search", {"query": query})
            from app.services.deep_research.searcher import _extract_urls, canonicalize_url

            for raw_url in _extract_urls(raw)[:5]:
                url = canonicalize_url(raw_url)
                if not url or url in seen_source_urls:
                    continue
                candidates.append(
                    SearchCandidate(url=url, lane_id=lane_id, query=query, rank=len(candidates) + 1)
                )

        accepted = 0
        for candidate in candidates:
            if len(ledger.sources) >= request.max_sources:
                break
            fetched = await reader.fetch_candidate(
                candidate, source_type=_source_type_for_lane(candidate.lane_id)
            )
            if fetched is None:
                writer.append_step(
                    _step("read", "failed", f"Controller could not fetch {candidate.url}.")
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
            accepted += 1
            reasoned = await maybe_extract_claims(self.reasoner, request, ledger, source)
            if not reasoned:
                extract_claims_from_source(ledger, source)
            note = await maybe_summarize_source(self.reasoner, request, source)
            if note is not None:
                source_notes_by_id[source.source_id] = note
                writer.append_source_note(note)
            writer.append_step(
                _step("read", "completed", f"Controller ledgered {source.url}.", {"source_id": source.source_id, "step": step_index})
            )

        return {"queries": queries, "candidates": len(candidates), "accepted": accepted}

    async def _dispatch_visit(
        self,
        *,
        urls: list[str],
        request: ResearchRequest,
        ledger: EvidenceLedger,
        reader: ResearchReader,
        writer: ResearchArtifactWriter,
        source_notes_by_id: dict[str, dict[str, Any]],
        seen_source_urls: set[str],
        step_index: int,
        maybe_extract_claims,
        maybe_summarize_source,
    ) -> dict[str, Any]:
        if not urls:
            return {"urls": [], "accepted": 0, "warning": "no urls provided"}

        accepted = 0
        for url in urls[:5]:
            if len(ledger.sources) >= request.max_sources:
                break
            if url in seen_source_urls:
                continue
            candidate = SearchCandidate(url=url, lane_id="controller_visit", query=request.question)
            fetched = await reader.fetch_candidate(
                candidate, source_type=SourceType.UNKNOWN
            )
            if fetched is None:
                writer.append_step(
                    _step("read", "failed", f"Controller visit could not fetch {url}.")
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
            accepted += 1
            reasoned = await maybe_extract_claims(self.reasoner, request, ledger, source)
            if not reasoned:
                extract_claims_from_source(ledger, source)
            note = await maybe_summarize_source(self.reasoner, request, source)
            if note is not None:
                source_notes_by_id[source.source_id] = note
                writer.append_source_note(note)
            writer.append_step(
                _step("read", "completed", f"Controller visited {source.url}.", {"source_id": source.source_id, "step": step_index})
            )

        return {"urls": urls, "accepted": accepted}

    async def _dispatch_reflect(
        self,
        *,
        request: ResearchRequest,
        plan,
        ledger: EvidenceLedger,
        source_notes_by_id: dict[str, dict[str, Any]],
        gaps_queue: list[str],
        step_index: int,
    ) -> dict[str, Any]:
        from app.services.deep_research.reflector import ResearchReflector

        evaluation_now = self.evaluator.evaluate(
            request=request, ledger=ledger, round_index=step_index
        )
        reflector = ResearchReflector(self.reasoner)
        decision = await reflector.reflect(
            request=request,
            plan=plan,
            ledger=ledger,
            round_index=step_index,
            source_notes=list(source_notes_by_id.values()),
            lane_summaries=[],
            evaluator_gaps=evaluation_now.gaps,
            evaluator_next_queries=evaluation_now.next_queries,
        )

        new_queries = [item.get("query") for item in decision.next_queries if item.get("query")]
        for query in new_queries:
            if query and query not in gaps_queue:
                gaps_queue.append(query)

        return {
            "stop_signal": decision.stop_signal,
            "rationale": decision.rationale,
            "new_queries": new_queries,
            "source": decision.source,
        }


def _step(phase: str, status: str, message: str, detail: dict[str, Any] | None = None) -> ResearchStep:
    return ResearchStep(
        step_id=new_id("step"),
        phase=phase,
        status=status,
        message=message,
        detail=detail or {},
    )


def _trace_entry(
    *,
    step_index: int,
    action_type: str,
    rationale: str,
    decision: dict[str, Any],
    outputs: dict[str, Any],
    tokens_estimated: int,
    role: str,
) -> dict[str, Any]:
    return {
        "step_index": step_index,
        "action_type": action_type,
        "role": role,
        "rationale": rationale,
        "decision": to_jsonable(decision),
        "outputs": to_jsonable(outputs),
        "tokens_estimated": tokens_estimated,
        "timestamp": utc_now(),
    }


def _record_trace(path: Path, entry: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("query") or item.get("url") or item.get("text")
            if text and isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out


def _source_type_for_lane(lane_id: str) -> SourceType:
    return {
        "official": SourceType.PRIMARY,
        "regulatory": SourceType.REGULATORY,
        "market": SourceType.DATASET,
        "technical": SourceType.TECHNICAL,
        "secondary": SourceType.SECONDARY,
    }.get(lane_id, SourceType.UNKNOWN)
