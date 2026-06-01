from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services import plan_mode_core
from app.services.deep_research.plan_contract import (
    build_runtime_contract,
    default_format_brief,
    normalize_contract_output_format,
    request_arguments_from_contract,
)
from app.services.deep_research.schemas import ResearchRequest

DEEP_RESEARCH_HANDOFF_TARGET = "deep_research"


async def build_deep_research_plan_preview(research_request: ResearchRequest) -> dict[str, Any]:
    """Build a no-side-effect preview for the Plan Mode confirmation card."""
    from app.services.deep_research.orchestrator import _topic_budget, _worker_topics
    from app.services.deep_research.planner import build_research_plan

    plan = build_research_plan(research_request)
    topics = await _worker_topics(None, research_request, plan)
    return {
        "plan": _to_jsonable(plan),
        "worker_topics": topics[: _topic_budget(research_request, plan)],
        "clarifying_questions": _clarifying_questions(research_request),
    }


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

    wants_chinese = _wants_chinese_plan(request)
    title = _user_facing_title(request.question, wants_chinese=wants_chinese)
    output_capabilities = _output_capabilities(normalized_format, wants_chinese=wants_chinese)
    handoff_payload = _handoff_payload(request, worker_topics=worker_topics, output_format=normalized_format)
    runtime_contract = build_runtime_contract(request, preview, output_format=normalized_format)
    estimated_duration = _duration_for_depth(request.depth)
    token_cost = _token_cost_for_depth(request.depth)
    risk_level = "high" if request.depth in {"full", "flagship", "deep"} else "medium"

    if wants_chinese:
        steps: list[dict[str, Any]] = [
            {
                "order": 1,
                "description": "确认研究范围、时间窗口、证据标准、输出语言和交付格式。",
                "expected_output": "用户确认后的研究方案；确认前不开始抓取来源或综合写作。",
            },
            {
                "order": 2,
                "description": "按主要赛道并行收集和核验高可信来源。",
                "expected_output": "每条赛道形成可追溯证据摘要，并区分事实、判断和推测。",
            },
            {
                "order": 3,
                "description": "跨赛道综合、识别矛盾和缺口，并运行归因、新鲜度、完整性与矛盾检查。",
                "expected_output": "形成可用于投资决策的核心结论、风险边界和待验证问题。",
            },
            {
                "order": 4,
                "description": "撰写最终中文 markdown 报告。",
                "expected_output": "输出 report.md，包含执行摘要、赛道分析、风险矩阵、投资含义和来源归因。",
            },
        ]
    else:
        steps = [
            {
                "order": 1,
                "description": "Confirm research scope, depth, evidence standard, output language, and requested delivery format.",
                "expected_output": "User-approved research plan; no source fetching or synthesis starts before approval.",
            },
            {
                "order": 2,
                "description": "Collect and verify high-trust sources across the approved research lanes.",
                "expected_output": "Traceable evidence summaries that separate facts, judgments, and speculation.",
            },
            {
                "order": 3,
                "description": "Synthesize across lanes, identify contradictions and gaps, and run quality checks.",
                "expected_output": "Decision-useful findings, risk boundaries, and unresolved questions.",
            },
            {
                "order": 4,
                "description": "Write the final markdown report.",
                "expected_output": "report.md with executive summary, lane analysis, risk matrix, investment implications, and citations.",
            },
        ]
    if normalized_format not in {"markdown", "md"}:
        steps.append(
            {
                "order": 4,
                "description": (
                    f"在 report.md 完成后，为 {normalized_format.upper()} 重新组织表达方式并生成衍生交付物。"
                    if wants_chinese
                    else f"After report.md is complete, adapt the presentation for {normalized_format.upper()} as a derived deliverable."
                ),
                "expected_output": (
                    f"保留 report.md 作为原始报告，同时生成适合该格式阅读/演示的 report.{_output_suffix(normalized_format)}。"
                    if wants_chinese
                    else f"report.{_output_suffix(normalized_format)} adapted for the format while preserving report.md."
                ),
            }
        )

    return {
        "title": title,
        "objective": (
            f"生成一份面向投资决策的中文 Deep Research 报告，回答：{request.question}。关键判断需要有来源支撑，事实、判断和推测要明确区分。"
            if wants_chinese
            else f"Produce a decision-useful Deep Research report answering: {request.question}. Material judgments need source support and clear fact/judgment/speculation boundaries."
        ),
        "motivation": _motivation_text(wants_chinese),
        "steps": steps,
        "success_criteria": _success_criteria(wants_chinese),
        "wake_policy": {"type": "none"},
        "required_capabilities": [
            "Deep Research",
            "Web 来源核验" if wants_chinese else "Web source verification",
            *output_capabilities,
        ],
        "external_side_effects": [],
        "risk_assessment": {
            "level": risk_level,
            "reasons": _risk_reasons(wants_chinese),
        },
        "estimated_cost": {
            "tokens_per_run": token_cost,
            "expected_duration": estimated_duration,
        },
        "stop_conditions": _stop_conditions(wants_chinese),
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
                "format_brief": default_format_brief(normalized_format),
            },
            "runtime_contract": runtime_contract,
        },
    }


async def deep_research_handoff_handler(_db: Any, plan: Any) -> dict[str, Any]:
    plan_json = plan.plan_json or {}
    handoff = plan_json.get("handoff") if isinstance(plan_json, dict) else {}
    payload = handoff.get("payload") if isinstance(handoff, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("Deep Research plan is missing handoff.payload")

    payload = dict(payload)
    deep_research_plan = plan_json.get("deep_research") if isinstance(plan_json.get("deep_research"), dict) else {}
    runtime_contract = deep_research_plan.get("runtime_contract") if isinstance(deep_research_plan, dict) else None
    if isinstance(runtime_contract, dict):
        payload.update({key: value for key, value in request_arguments_from_contract(runtime_contract).items() if value is not None})
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
    return normalize_contract_output_format(value)


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


def _output_capabilities(output_format: str, *, wants_chinese: bool) -> list[str]:
    if output_format in {"docx", "xlsx", "pptx"}:
        return ["Office 交付物生成" if wants_chinese else "Office artifact generation"]
    return []


def _output_suffix(output_format: str) -> str:
    return {
        "markdown": "md",
        "json": "json",
        "html": "html",
        "docx": "docx",
        "xlsx": "xlsx",
        "pptx": "pptx",
    }.get(output_format, "md")


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _wants_chinese_plan(request: ResearchRequest) -> bool:
    language = str(request.output_language or "").lower()
    return language.startswith("zh") or bool(any("\u4e00" <= char <= "\u9fff" for char in request.question))


def _motivation_text(wants_chinese: bool) -> str:
    if wants_chinese:
        return (
            "Deep Research 是长时间、证据敏感的研究流程。在任何 fan-out、来源抓取或综合写作开始前，"
            "用户应先看到并确认研究范围、证据标准和交付物。"
        )
    return (
        "Deep Research is a long-running, evidence-sensitive workflow. The user should see and approve "
        "the research plan before any fan-out, source fetching, or synthesis begins."
    )


def _success_criteria(wants_chinese: bool) -> list[str]:
    if wants_chinese:
        return [
            "canonical 交付物为 report.md；即使请求其他格式，也必须保留原始 markdown 报告。",
            "每个关键判断都绑定到已抓取证据，或明确标注为 unsupported / inferred。",
            "内部保留完整来源账本和审计轨迹，用于核验关键判断和复盘质量门。",
            "最终状态必须诚实：综合失败时输出失败说明，而不是拼接证据摘要冒充完整报告。",
        ]
    return [
        "The canonical deliverable is report.md and it remains available even when another output format is requested.",
        "Every material claim is tied to fetched evidence or explicitly marked unsupported/inferred.",
        "Internal source ledgers and audit traces are preserved for claim verification and quality-gate review.",
        "The final status is honest: failed synthesis produces a failure notice, not a stitched evidence dump.",
    ]


def _risk_reasons(wants_chinese: bool) -> list[str]:
    if wants_chinese:
        return [
            "长时间研究会消耗较高 token 和外部来源抓取预算。",
            "报告质量依赖来源新鲜度、归因完整性和综合阶段 quality gates。",
        ]
    return [
        "Long-running research can spend significant tokens and external fetch budget.",
        "Report quality depends on source freshness, attribution, and synthesis quality gates.",
    ]


def _stop_conditions(wants_chinese: bool) -> list[str]:
    if wants_chinese:
        return [
            "用户拒绝计划或要求调整研究范围。",
            "运行达到预设时间或来源上限。",
            "综合阶段无法产出有来源支撑、可交付给用户的报告。",
        ]
    return [
        "The user rejects the plan or requests a revised scope.",
        "The run reaches the configured deadline or source cap.",
        "Synthesis cannot produce a source-grounded user-deliverable report.",
    ]


def _clarifying_questions(research_request: ResearchRequest) -> list[str]:
    return [
        "Scope and exclusions: which sub-topics, regions, projects, or asset classes must be included or excluded?",
        f"Time window: currently '{research_request.time_window or 'unspecified'}'. Confirm the period the report should cover.",
        f"Depth: currently '{research_request.depth}'. Confirm whether this is the right depth and cost profile.",
        "Decision use: what investment, product, or strategy decision should the report support?",
        "Output: confirm the final language and whether markdown alone is enough or an Office artifact is needed.",
    ]


def _to_jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _to_jsonable(getattr(value, key)) for key in value.__dataclass_fields__}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    if hasattr(value, "value"):
        return value.value
    return value


def _short_title(question: str) -> str:
    text = " ".join(str(question or "").split())
    return text[:90] + ("..." if len(text) > 90 else "")


def _user_facing_title(question: str, *, wants_chinese: bool) -> str:
    text = _short_title(question)
    if not wants_chinese:
        return f"Deep Research: {text}"

    cleaned = re.sub(r"使用\s*deep\s*research\s*做(一个|一份)?", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"使用\s*deepresearch\s*做(一个|一份)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" ：:，,。")
    if "web3" in cleaned.lower() and "全景" in cleaned:
        return "Web3 全景深度研究报告"
    if cleaned:
        return f"Deep Research：{cleaned}"
    return f"Deep Research：{text}"


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
