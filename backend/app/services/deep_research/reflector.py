"""Tier 2-1 ResearchReflector: LLM-driven mid-investigation reflection.

Replaces evaluator's mechanical `<q> additional independent sources` next-query
emission with an LLM call that looks at the full ledger + structured notes and
decides whether to stop OR what specific gaps to target next.

Falls back to the evaluator's mechanical signal when no reasoner is wired or the
LLM call fails — Tier 1 behavior is preserved as the floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.deep_research.ledger import EvidenceLedger
from app.services.deep_research.schemas import ResearchPlan, ResearchRequest

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReflectionDecision:
    """One per-round reflection record. Persisted to reflection.jsonl."""

    round_index: int
    stop_signal: bool
    rationale: str = ""
    next_queries: list[dict[str, Any]] = field(default_factory=list)
    source: str = "reasoner"

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "stop_signal": self.stop_signal,
            "rationale": self.rationale,
            "next_queries": self.next_queries,
            "source": self.source,
        }


class ResearchReflector:
    """Wraps a reasoner with reflect_progress capability; emits ReflectionDecision."""

    def __init__(self, reasoner: Any | None = None):
        self.reasoner = reasoner

    async def reflect(
        self,
        *,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger: EvidenceLedger,
        round_index: int,
        source_notes: list[dict[str, Any]],
        lane_summaries: list[dict[str, Any]],
        evaluator_gaps: list[str],
        evaluator_next_queries: list[str],
    ) -> ReflectionDecision:
        if self.reasoner is None or not hasattr(self.reasoner, "reflect_progress"):
            return self._mechanical_fallback(round_index, evaluator_gaps, evaluator_next_queries)

        try:
            decision = await self.reasoner.reflect_progress(
                request=request,
                plan=plan,
                ledger=ledger,
                round_index=round_index,
                source_notes=source_notes,
                lane_summaries=lane_summaries,
                evaluator_gaps=evaluator_gaps,
            )
        except Exception as exc:
            logger.warning(
                "[Reflector] LLM reflect failed (round=%d): %s — falling back to evaluator signal",
                round_index,
                exc,
            )
            return self._mechanical_fallback(round_index, evaluator_gaps, evaluator_next_queries)

        return self._normalize_decision(decision, round_index, evaluator_next_queries)

    @staticmethod
    def _normalize_decision(
        decision: Any,
        round_index: int,
        evaluator_next_queries: list[str],
    ) -> ReflectionDecision:
        if isinstance(decision, ReflectionDecision):
            return decision
        if not isinstance(decision, dict):
            return ResearchReflector._mechanical_fallback(round_index, [], evaluator_next_queries)

        stop_signal = bool(decision.get("stop_signal", False))
        rationale = str(decision.get("rationale") or "").strip()
        raw_queries = decision.get("next_queries") or []
        next_queries: list[dict[str, Any]] = []
        if isinstance(raw_queries, list):
            for item in raw_queries[:5]:
                if isinstance(item, str) and item.strip():
                    next_queries.append(
                        {"query": item.strip(), "lane_id": "", "targets": "follow-up"}
                    )
                elif isinstance(item, dict):
                    query_text = str(item.get("query") or "").strip()
                    if not query_text:
                        continue
                    next_queries.append(
                        {
                            "query": query_text,
                            "lane_id": str(item.get("lane_id") or "").strip(),
                            "targets": str(item.get("targets") or "").strip(),
                        }
                    )

        return ReflectionDecision(
            round_index=round_index,
            stop_signal=stop_signal,
            rationale=rationale,
            next_queries=next_queries,
            source="reasoner",
        )

    @staticmethod
    def _mechanical_fallback(
        round_index: int,
        evaluator_gaps: list[str],
        evaluator_next_queries: list[str],
    ) -> ReflectionDecision:
        rationale = (
            "Evaluator mechanical fallback: "
            + ("; ".join(evaluator_gaps) if evaluator_gaps else "no gaps recorded")
        )
        return ReflectionDecision(
            round_index=round_index,
            stop_signal=not evaluator_next_queries,
            rationale=rationale,
            next_queries=[
                {"query": query, "lane_id": "", "targets": "evaluator-suggested"}
                for query in evaluator_next_queries
            ],
            source="evaluator_fallback",
        )
