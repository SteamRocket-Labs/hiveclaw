from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.deep_research.schemas import ResearchLane, ResearchPlan, ResearchRequest, SearchQuery, SourceType

RUNTIME_CONTRACT_SCHEMA = "deep_research_runtime_contract.v1"
SUPPORTED_DEEP_RESEARCH_FORMATS: frozenset[str] = frozenset(
    {"markdown", "json", "html", "docx", "xlsx", "pptx"}
)


def runtime_manifest() -> dict[str, Any]:
    return {
        "runtime_version": "deep_research.vnext",
        "supported_depths": ["quick", "standard", "full", "flagship", "deep"],
        "supported_source_policies": ["primary_only", "primary_preferred", "mixed"],
        "supported_output_formats": sorted(SUPPORTED_DEEP_RESEARCH_FORMATS),
        "supported_office_artifacts": ["docx", "xlsx", "pptx"],
        "supported_worker_roles": ["planner", "research_worker", "critic", "writer", "composer"],
        "max_worker_topics": 6,
        "max_sources": 60,
        "supports_runtime_adaptation": True,
    }


def build_runtime_contract(request: ResearchRequest, preview: dict[str, Any], *, output_format: str) -> dict[str, Any]:
    raw_plan = preview.get("plan") if isinstance(preview.get("plan"), dict) else {}
    raw_lanes = raw_plan.get("lanes") if isinstance(raw_plan, dict) else []
    raw_lanes = raw_lanes if isinstance(raw_lanes, list) else []
    worker_topics = _string_list(preview.get("worker_topics"))
    lanes: list[dict[str, Any]] = []
    for index, item in enumerate(raw_lanes):
        if not isinstance(item, dict):
            continue
        lane_id = str(item.get("lane_id") or item.get("id") or f"lane_{index + 1}").strip()
        label = str(item.get("label") or lane_id).strip()
        queries = _queries_from_lane(item, lane_id=lane_id)
        worker_topic = worker_topics[index] if index < len(worker_topics) else _fallback_worker_topic(item, request)
        lanes.append(
            {
                "id": lane_id,
                "label": label,
                "goal": str(item.get("goal") or "").strip() or f"Collect evidence for {label}.",
                "worker_topic": worker_topic,
                "queries": queries,
                "preferred_source_types": _source_type_values(item.get("preferred_source_types")),
                "must_answer": _string_list(item.get("must_answer")),
            }
        )
    if not lanes:
        for index, topic in enumerate(worker_topics[:6]):
            lanes.append(
                {
                    "id": f"lane_{index + 1}",
                    "label": topic[:80] or f"Lane {index + 1}",
                    "goal": topic,
                    "worker_topic": topic,
                    "queries": [{"query": topic, "rationale": "Confirmed worker topic."}],
                    "preferred_source_types": [],
                    "must_answer": [],
                }
            )

    requested_format = normalize_contract_output_format(output_format)
    contract = {
        "schema": RUNTIME_CONTRACT_SCHEMA,
        "runtime_version": "deep_research.vnext",
        "question": request.question,
        "mode": request.mode,
        "decision_context": "",
        "audience": "",
        "scope": {
            "raw": request.scope,
            "in_scope": [request.scope] if request.scope else [],
            "out_of_scope": [],
            "assumptions": [],
        },
        "research": {
            "depth": request.depth,
            "source_policy": request.source_policy,
            "time_window": request.time_window,
            "lanes": lanes,
            "quality_gates": [
                "single_language",
                "source_grounded_claims",
                "evidence_weighting",
                "contradictions_addressed",
                "not_sequential_summary",
            ],
        },
        "budget": {
            "max_sources": request.max_sources,
            "max_rounds": request.max_rounds,
            "concurrency": request.concurrency,
            "deadline_seconds": request.deadline_seconds,
            "token_budget": request.token_budget,
        },
        "output": {
            "language": request.output_language,
            "requested_formats": [requested_format],
            "primary_format": requested_format,
            "format_briefs": {requested_format: default_format_brief(requested_format)},
        },
        "allowed_adaptations": [
            "replace_failed_source_with_same_lane_source",
            "add_follow_up_query_within_lane",
            "downgrade_or_discard_low_quality_source",
        ],
        "requires_reconfirmation_if": [
            "new_lane",
            "new_format",
            "budget_increase_over_25_percent",
            "external_side_effect",
            "scope_change",
        ],
    }
    validate_runtime_contract(contract)
    return contract


def validate_runtime_contract(contract: dict[str, Any]) -> None:
    if not isinstance(contract, dict):
        raise ValueError("Deep Research runtime contract must be an object")
    if contract.get("schema") != RUNTIME_CONTRACT_SCHEMA:
        raise ValueError(f"Unsupported Deep Research runtime contract schema: {contract.get('schema')!r}")
    research = contract.get("research")
    if not isinstance(research, dict):
        raise ValueError("Deep Research runtime contract requires research")
    lanes = research.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("Deep Research runtime contract requires at least one lane")
    for lane in lanes:
        if not isinstance(lane, dict):
            raise ValueError("Deep Research runtime contract lanes must be objects")
        if not str(lane.get("id") or "").strip():
            raise ValueError("Deep Research runtime contract lane requires id")
        if not str(lane.get("worker_topic") or lane.get("goal") or "").strip():
            raise ValueError("Deep Research runtime contract lane requires worker_topic or goal")
    output = contract.get("output")
    if not isinstance(output, dict):
        raise ValueError("Deep Research runtime contract requires output")
    formats = output.get("requested_formats")
    if not isinstance(formats, list) or not formats:
        raise ValueError("Deep Research runtime contract requires output.requested_formats")
    unsupported = [fmt for fmt in formats if normalize_contract_output_format(fmt) not in SUPPORTED_DEEP_RESEARCH_FORMATS]
    if unsupported:
        raise ValueError(f"Unsupported Deep Research output format(s): {unsupported}")


def research_plan_from_contract(contract: dict[str, Any]) -> ResearchPlan:
    validate_runtime_contract(contract)
    research = contract["research"]
    lanes = []
    for lane in research["lanes"]:
        lane_id = str(lane.get("id") or lane.get("lane_id") or "").strip()
        lanes.append(
            ResearchLane(
                lane_id=lane_id,
                label=str(lane.get("label") or lane_id).strip(),
                goal=str(lane.get("goal") or lane.get("worker_topic") or "").strip(),
                queries=[
                    SearchQuery(
                        query=str(query.get("query") if isinstance(query, dict) else query).strip(),
                        lane_id=lane_id,
                        rationale=str(query.get("rationale") if isinstance(query, dict) else "").strip(),
                    )
                    for query in lane.get("queries", [])
                    if str(query.get("query") if isinstance(query, dict) else query).strip()
                ],
                preferred_source_types=_coerce_source_types(lane.get("preferred_source_types")),
            )
        )
    scope = contract.get("scope")
    scope_text = scope.get("raw", "") if isinstance(scope, dict) else str(scope or "")
    return ResearchPlan(
        question=str(contract.get("question") or ""),
        mode=str(contract.get("mode") or "topic_deep_dive"),
        lanes=lanes,
        scope=scope_text,
        time_window=str(research.get("time_window") or ""),
        source_policy=str(research.get("source_policy") or "primary_preferred"),
    )


def worker_topics_from_contract(contract: dict[str, Any]) -> list[str]:
    validate_runtime_contract(contract)
    topics: list[str] = []
    for lane in contract["research"]["lanes"]:
        topic = str(lane.get("worker_topic") or lane.get("goal") or lane.get("label") or "").strip()
        if topic:
            topics.append(topic)
    return topics


def request_arguments_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    validate_runtime_contract(contract)
    research = contract.get("research") if isinstance(contract.get("research"), dict) else {}
    budget = contract.get("budget") if isinstance(contract.get("budget"), dict) else {}
    output = contract.get("output") if isinstance(contract.get("output"), dict) else {}
    scope = contract.get("scope")
    scope_text = scope.get("raw", "") if isinstance(scope, dict) else str(scope or "")
    formats = output.get("requested_formats") if isinstance(output.get("requested_formats"), list) else []
    primary_format = output.get("primary_format") or (formats[0] if formats else "markdown")
    return {
        "question": contract.get("question"),
        "mode": contract.get("mode") or "topic_deep_dive",
        "scope": scope_text,
        "depth": research.get("depth") or "standard",
        "source_policy": research.get("source_policy") or "primary_preferred",
        "time_window": research.get("time_window") or "",
        "max_rounds": budget.get("max_rounds"),
        "max_sources": budget.get("max_sources"),
        "concurrency": budget.get("concurrency"),
        "token_budget": budget.get("token_budget"),
        "deadline_seconds": budget.get("deadline_seconds"),
        "output_format": primary_format,
        "output_language": output.get("language") or "",
        "plan_confirmed": True,
        "worker_topics": worker_topics_from_contract(contract),
        "approved_plan": deepcopy(contract),
    }


def normalize_contract_output_format(value: Any) -> str:
    normalized = str(value or "markdown").strip().lower()
    aliases = {"md": "markdown", "doc": "docx", "slides": "pptx", "ppt": "pptx", "sheet": "xlsx"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in SUPPORTED_DEEP_RESEARCH_FORMATS else "markdown"


def default_format_brief(output_format: str) -> dict[str, str]:
    normalized = normalize_contract_output_format(output_format)
    return {
        "markdown": {"purpose": "auditable research dossier", "expression": "full source-grounded report"},
        "json": {"purpose": "structured machine output", "expression": "claims, sources, gates, gaps"},
        "html": {"purpose": "interactive research brief", "expression": "reader-friendly drill-down"},
        "docx": {"purpose": "formal research memo", "expression": "memo with tables, footnotes, and appendix"},
        "xlsx": {"purpose": "evidence workbook", "expression": "structured evidence and claims sheets"},
        "pptx": {"purpose": "executive decision deck", "expression": "storyline slides with takeaways"},
    }[normalized]


def _queries_from_lane(item: dict[str, Any], *, lane_id: str) -> list[dict[str, str]]:
    raw_queries = item.get("queries")
    if not isinstance(raw_queries, list):
        return []
    queries: list[dict[str, str]] = []
    for query in raw_queries:
        if isinstance(query, dict):
            text = str(query.get("query") or "").strip()
            rationale = str(query.get("rationale") or item.get("goal") or "").strip()
        else:
            text = str(query or "").strip()
            rationale = str(item.get("goal") or "").strip()
        if text:
            queries.append({"query": text, "lane_id": lane_id, "rationale": rationale})
    return queries


def _fallback_worker_topic(item: dict[str, Any], request: ResearchRequest) -> str:
    label = str(item.get("label") or item.get("lane_id") or item.get("id") or "Research lane").strip()
    goal = str(item.get("goal") or "collect source-grounded evidence").strip()
    return f"Research lane: {label}\nGoal: {goal}\nBackground: {request.question}"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def _source_type_values(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        raw = item.value if isinstance(item, SourceType) else str(item or "").strip().lower()
        if raw in {source_type.value for source_type in SourceType}:
            values.append(raw)
    return values


def _coerce_source_types(value: Any) -> list[SourceType]:
    source_types: list[SourceType] = []
    for raw in _source_type_values(value):
        try:
            source_types.append(SourceType(raw))
        except ValueError:
            continue
    return source_types
