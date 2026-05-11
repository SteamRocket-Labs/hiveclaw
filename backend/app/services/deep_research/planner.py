from __future__ import annotations

import re

from app.services.deep_research.schemas import ResearchLane, ResearchPlan, ResearchRequest, SearchQuery, SourceType


_LANE_PRESETS: tuple[tuple[str, str, str, tuple[SourceType, ...]], ...] = (
    ("official", "Official/project sources", "Find official product, protocol, company, or project sources.", (SourceType.PRIMARY, SourceType.TECHNICAL)),
    ("regulatory", "Regulatory/legal sources", "Find regulator, legal, policy, or compliance evidence.", (SourceType.REGULATORY, SourceType.PRIMARY)),
    ("market", "Market/data sources", "Find datasets, market maps, adoption metrics, or transaction evidence.", (SourceType.DATASET, SourceType.SECONDARY)),
    ("competitor", "Competitor/ecosystem sources", "Find ecosystem participants, competitors, vendors, and adoption examples.", (SourceType.PRIMARY, SourceType.SECONDARY)),
    ("technical", "Technical documentation", "Find docs, specs, APIs, repositories, or implementation notes.", (SourceType.TECHNICAL, SourceType.PRIMARY)),
    ("secondary", "Secondary context", "Find reputable analysis or trade coverage for context and contradictions.", (SourceType.SECONDARY,)),
)


def build_research_plan(request: ResearchRequest) -> ResearchPlan:
    """Deterministic baseline planner.

    LLM-assisted planning can be layered on later through invoke_agent, but this
    baseline keeps the workflow deterministic, testable, and governance-safe.
    """
    question = _normalize_question(request.question)
    lane_count = _lane_count_for_depth(request.depth)
    lanes: list[ResearchLane] = []
    for lane_id, label, goal, source_types in _LANE_PRESETS[:lane_count]:
        queries = [
            SearchQuery(query=_build_query(question, lane_id, request.time_window), lane_id=lane_id, rationale=goal),
        ]
        if request.scope:
            queries.append(
                SearchQuery(
                    query=_build_query(f"{question} {request.scope}", lane_id, request.time_window),
                    lane_id=lane_id,
                    rationale=f"Scope-specific query for {label}.",
                )
            )
        lanes.append(
            ResearchLane(
                lane_id=lane_id,
                label=label,
                goal=goal,
                queries=queries,
                preferred_source_types=list(source_types),
            )
        )
    return ResearchPlan(
        question=request.question,
        mode=request.mode,
        lanes=lanes,
        scope=request.scope,
        time_window=request.time_window,
        source_policy=request.source_policy,
    )


def _lane_count_for_depth(depth: str) -> int:
    normalized = depth.strip().lower()
    if normalized in {"quick", "light"}:
        return 3
    if normalized in {"full", "flagship", "deep"}:
        return 6
    return 4


def _build_query(question: str, lane_id: str, time_window: str) -> str:
    lane_terms = {
        "official": "official documentation announcement",
        "regulatory": "regulation legal compliance regulator",
        "market": "market data adoption report statistics",
        "competitor": "competitors ecosystem comparison",
        "technical": "technical docs github architecture",
        "secondary": "analysis news trade coverage",
    }
    pieces = [question, lane_terms.get(lane_id, lane_id)]
    if time_window:
        pieces.append(time_window)
    return " ".join(item for item in pieces if item).strip()


def _normalize_question(question: str) -> str:
    return re.sub(r"\s+", " ", question).strip()
