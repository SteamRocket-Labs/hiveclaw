from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import plan_mode_core
from app.services.deep_research.schemas import ResearchRequest

DEEP_RESEARCH_HANDOFF_TARGET = "deep_research"


def deep_research_plan_signature(request: ResearchRequest, *, worker_topics: list[str] | None = None) -> str:
    payload = {
        "tool": "deep_research_start" if request.depth in {"full", "flagship", "deep"} else "deep_research_run",
        "question": request.question,
        "mode": request.mode,
        "scope": request.scope,
        "depth": request.depth,
        "source_policy": request.source_policy,
        "time_window": request.time_window,
        "max_rounds": request.max_rounds,
        "max_sources": request.max_sources,
        "concurrency": request.concurrency,
        "token_budget": request.token_budget,
        "deadline_seconds": request.deadline_seconds,
        "output_format": normalize_deep_research_output_format(request.output_format),
        "output_language": request.output_language,
        "worker_topics": worker_topics or request.worker_topics,
        "controller_mode": request.controller_mode,
    }
    canonical = plan_mode_core.canonical_plan_json(payload)
    return "deep_research:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def build_deep_research_plan_fill(request: ResearchRequest, preview: dict[str, Any]) -> dict[str, Any]:
    worker_topics = _string_list(preview.get("worker_topics"))
    clarifying_questions = _string_list(preview.get("clarifying_questions"))
    normalized_format = normalize_deep_research_output_format(request.output_format)
    raw_plan = preview.get("plan") if isinstance(preview.get("plan"), dict) else {}
    lanes = raw_plan.get("lanes") if isinstance(raw_plan, dict) else []
    lanes = lanes if isinstance(lanes, list) else []
    lane_labels = [
        str(lane.get("label") or lane.get("lane_id") or "").strip() for lane in lanes if isinstance(lane, dict)
    ]
    lane_labels = [item for item in lane_labels if item]

    title = f"Deep Research: {_short_title(request.question)}"
    output_capabilities = _output_capabilities(normalized_format)
    handoff_payload = _handoff_payload(request, worker_topics=worker_topics, output_format=normalized_format)
    estimated_duration = _duration_for_depth(request.depth)
    token_cost = _token_cost_for_depth(request.depth)
    risk_level = "high" if request.depth in {"full", "flagship", "deep"} else "medium"

    steps: list[dict[str, Any]] = [
        {
            "order": 1,
            "description": "Confirm research scope, depth, evidence standard, output language, and requested delivery format.",
            "expected_output": "User-approved immutable Deep Research plan.",
        },
        {
            "order": 2,
            "description": "Run the approved research lanes and worker topics against source-ledger-backed evidence.",
            "expected_output": "Fetched sources, claims, worker reports, lane summaries, and evaluation artifacts.",
        },
        {
            "order": 3,
            "description": "Synthesize the canonical markdown report with source-bound claims and explicit gaps.",
            "expected_output": "report.md, final.json, sources.jsonl, claims.jsonl, and steps.jsonl.",
        },
    ]
    if normalized_format not in {"markdown", "md"}:
        steps.append(
            {
                "order": 4,
                "description": f"Convert the canonical markdown report into {normalized_format.upper()} as a derived artifact.",
                "expected_output": f"report.{_output_suffix(normalized_format)} while preserving report.md unchanged.",
            }
        )

    return {
        "title": title,
        "objective": f"Produce a source-ledger-backed Deep Research report answering: {request.question}",
        "motivation": (
            "Deep Research is a long-running, evidence-sensitive workflow. The user should see and approve "
            "the research plan before any fan-out, source fetching, or synthesis begins."
        ),
        "steps": steps,
        "success_criteria": [
            "The canonical deliverable is report.md and it remains available even when another output format is requested.",
            "Every material claim is tied to fetched evidence or explicitly marked unsupported/inferred.",
            "The run writes auditable artifacts: plan.json, sources.jsonl, claims.jsonl, steps.jsonl, and final.json.",
            "The final status is honest: failed synthesis produces a failure notice, not a stitched evidence dump.",
        ],
        "wake_policy": {"type": "none"},
        "required_capabilities": ["deep_research_start", "web_search", "web_fetch", *output_capabilities],
        "external_side_effects": [],
        "risk_assessment": {
            "level": risk_level,
            "reasons": [
                "Long-running research can spend significant tokens and external fetch budget.",
                "Report quality depends on source freshness, attribution, and synthesis quality gates.",
            ],
        },
        "estimated_cost": {
            "tokens_per_run": token_cost,
            "expected_duration": estimated_duration,
        },
        "stop_conditions": [
            "The user rejects the plan or requests a revised scope.",
            "The run reaches the configured deadline or source cap.",
            "Synthesis cannot produce a source-grounded user-deliverable report.",
        ],
        "handoff": {
            "target": DEEP_RESEARCH_HANDOFF_TARGET,
            "create_objective": False,
            "create_trigger": False,
            "payload": handoff_payload,
        },
        "deep_research": {
            "question": request.question,
            "mode": request.mode,
            "scope": request.scope,
            "depth": request.depth,
            "source_policy": request.source_policy,
            "time_window": request.time_window,
            "max_rounds": request.max_rounds,
            "max_sources": request.max_sources,
            "concurrency": request.concurrency,
            "token_budget": request.token_budget,
            "deadline_seconds": request.deadline_seconds,
            "output_format": normalized_format,
            "output_language": request.output_language,
            "worker_topics": worker_topics,
            "clarifying_questions": clarifying_questions,
            "lane_labels": lane_labels,
            "output_contract": {
                "canonical": "report.md",
                "requested": f"report.{_output_suffix(normalized_format)}",
                "derived_formats_preserve_markdown": True,
            },
        },
    }


async def deep_research_handoff_handler(_db: Any, plan: Any) -> dict[str, Any]:
    plan_json = plan.plan_json or {}
    handoff = plan_json.get("handoff") if isinstance(plan_json, dict) else {}
    payload = handoff.get("payload") if isinstance(handoff, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("Deep Research plan is missing handoff.payload")

    payload = dict(payload)
    payload["plan_confirmed"] = True
    request = ResearchRequest.from_arguments(payload)
    workspace = Path(get_settings().AGENT_DATA_DIR) / str(plan.agent_id)
    user_id = getattr(plan, "confirmed_by_user_id", None) or getattr(plan, "requested_by_user_id", None)
    if user_id is None:
        raise ValueError("Deep Research handoff requires a confirmed user id")

    from app.tools.handlers.deep_research import start_deep_research_background_run

    return await start_deep_research_background_run(
        request=request,
        agent_id=plan.agent_id,
        user_id=user_id,
        workspace=workspace,
        plan_id=plan.id,
    )


def register_deep_research_handoff(service: Any) -> None:
    service.register_handoff_handler(DEEP_RESEARCH_HANDOFF_TARGET, deep_research_handoff_handler)


def normalize_deep_research_output_format(value: str | None) -> str:
    normalized = str(value or "markdown").strip().lower()
    aliases = {
        "md": "markdown",
        "doc": "docx",
        "slides": "pptx",
        "ppt": "pptx",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"markdown", "json", "html", "docx", "pptx"}:
        return "markdown"
    return normalized


def _handoff_payload(request: ResearchRequest, *, worker_topics: list[str], output_format: str) -> dict[str, Any]:
    return {
        "question": request.question,
        "mode": request.mode,
        "scope": request.scope,
        "depth": request.depth,
        "source_policy": request.source_policy,
        "time_window": request.time_window,
        "max_rounds": request.max_rounds,
        "max_sources": request.max_sources,
        "concurrency": request.concurrency,
        "token_budget": request.token_budget,
        "deadline_seconds": request.deadline_seconds,
        "output_format": output_format,
        "output_language": request.output_language,
        "plan_confirmed": True,
        "worker_topics": worker_topics,
        "controller_mode": request.controller_mode,
    }


def _output_capabilities(output_format: str) -> list[str]:
    if output_format in {"docx", "pptx"}:
        return ["office_document_create"]
    return []


def _output_suffix(output_format: str) -> str:
    return {
        "markdown": "md",
        "json": "json",
        "html": "html",
        "docx": "docx",
        "pptx": "pptx",
    }.get(output_format, "md")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _short_title(question: str) -> str:
    text = " ".join(str(question or "").split())
    return text[:90] + ("..." if len(text) > 90 else "")


def _duration_for_depth(depth: str) -> str:
    return {
        "quick": "about 2-4 minutes",
        "light": "about 2-4 minutes",
        "standard": "about 4-8 minutes",
        "full": "about 8-15 minutes",
        "flagship": "about 12-20 minutes",
        "deep": "about 8-15 minutes",
    }.get(str(depth or "").lower(), "about 4-8 minutes")


def _token_cost_for_depth(depth: str) -> str:
    return {
        "quick": "low-medium",
        "light": "low-medium",
        "standard": "medium",
        "full": "high",
        "flagship": "high",
        "deep": "high",
    }.get(str(depth or "").lower(), "medium")
