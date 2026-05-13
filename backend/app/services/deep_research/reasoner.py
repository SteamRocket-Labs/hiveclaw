from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.deep_research.schemas import (
    ResearchLane,
    ResearchPlan,
    ResearchRequest,
    SearchQuery,
    SourceRecord,
    SourceType,
    to_jsonable,
)


class RuntimeDeepResearchReasoner:
    """Tool-disabled internal reasoning layer for Deep Research.

    Web discovery and fetching stay inside the governed tool runtime. This
    reasoner only plans, extracts, and synthesizes from already fetched evidence
    through invoke_agent with an empty tool surface.
    """

    def __init__(self, *, agent_id: uuid.UUID, user_id: uuid.UUID):
        self.agent_id = agent_id
        self.user_id = user_id

    async def refine_plan(self, request: ResearchRequest, plan: ResearchPlan) -> ResearchPlan:
        payload = {
            "question": request.question,
            "mode": request.mode,
            "scope": request.scope,
            "depth": request.depth,
            "source_policy": request.source_policy,
            "baseline_plan": to_jsonable(plan),
        }
        content = await self._invoke(
            "Create a concrete Deep Research plan. Return JSON only.",
            (
                "Return JSON with a `lanes` array. Each lane must include lane_id, label, goal, "
                "preferred_source_types, and 2-4 specific search queries. Use concrete company, "
                "regulator, dataset, technical, and competitor terms when the topic implies them. "
                "Do not include commentary outside JSON.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
        )
        parsed = _parse_json_object(content)
        lanes_payload = parsed.get("lanes") if isinstance(parsed, dict) else None
        if not isinstance(lanes_payload, list):
            return plan
        lanes: list[ResearchLane] = []
        for item in lanes_payload[:8]:
            if not isinstance(item, dict):
                continue
            lane_id = str(item.get("lane_id") or "").strip() or f"lane_{len(lanes) + 1}"
            queries = [
                SearchQuery(
                    query=str(query.get("query") if isinstance(query, dict) else query).strip(),
                    lane_id=lane_id,
                    rationale=str(query.get("rationale") if isinstance(query, dict) else item.get("goal") or "").strip(),
                )
                for query in item.get("queries", [])
                if str(query.get("query") if isinstance(query, dict) else query).strip()
            ][:4]
            if not queries:
                continue
            lanes.append(
                ResearchLane(
                    lane_id=lane_id,
                    label=str(item.get("label") or lane_id).strip(),
                    goal=str(item.get("goal") or "").strip(),
                    queries=queries,
                    preferred_source_types=_source_types(item.get("preferred_source_types")),
                )
            )
        if len(lanes) < 3:
            return plan
        return ResearchPlan(
            question=plan.question,
            mode=plan.mode,
            lanes=lanes,
            scope=plan.scope,
            time_window=plan.time_window,
            source_policy=plan.source_policy,
        )

    async def extract_claims(self, request: ResearchRequest, source: SourceRecord) -> list[dict[str, Any]]:
        payload = {
            "question": request.question,
            "source": {
                "source_id": source.source_id,
                "url": source.url,
                "title": source.title,
                "publisher": source.publisher,
                "source_type": source.source_type.value,
                "content": source.content[:12000],
            },
        }
        content = await self._invoke(
            "Extract source-bound material claims. Return JSON only.",
            (
                "Return a JSON array of 1-10 material claims that matter to the research question. "
                "Each item must include text, status, source_ids, evidence, and optional notes. "
                "Use only claims directly supported by this source content. If the source is weak, return []. "
                "Do not infer beyond the source.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
        )
        parsed = _parse_json_array(content)
        return [item for item in parsed if isinstance(item, dict)]

    async def summarize_source(self, request: ResearchRequest, source: SourceRecord) -> dict[str, Any] | None:
        """Extract structured per-source notes (entities, numbers, dates, mechanisms,
        limitations, source-bound summary) so the synthesis stage gets focused facts
        rather than 1.8K excerpts averaged across all sources."""
        payload = {
            "question": request.question,
            "source": {
                "source_id": source.source_id,
                "url": source.url,
                "title": source.title,
                "publisher": source.publisher,
                "source_type": source.source_type.value,
                "content": source.content[:12000],
            },
        }
        content = await self._invoke(
            "Extract structured source notes. Return JSON only.",
            (
                "Read the source and produce structured notes. Return a JSON object with:\n"
                "  - relevance_score: float 0-1 (how directly this source answers the question)\n"
                "  - credibility_score: float 0-1 (primary>regulator>analyst>blog)\n"
                "  - recency_signal: one of 'current' / 'recent' / 'stale' / 'undated'\n"
                "  - key_entities: [str] named companies, regulators, products, agencies\n"
                "  - key_numbers: [str] concrete metrics with units (e.g. '35% YoY', '$4.2B AUM')\n"
                "  - key_dates: [str] specific dates or quarters\n"
                "  - mechanisms: [str] how the thing actually works (workflow, routing, controls)\n"
                "  - limitations: [str] what this source does not cover or explicitly disclaims\n"
                "  - source_bound_summary: 2-4 sentences citing only this source\n"
                "Do not infer beyond the source. If the source is unusable, return {}.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
        )
        parsed = _parse_json_object(content)
        if not parsed:
            return None
        parsed["source_id"] = source.source_id
        return parsed

    async def reflect_progress(
        self,
        *,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger,
        round_index: int,
        source_notes: list[dict[str, Any]],
        lane_summaries: list[dict[str, Any]],
        evaluator_gaps: list[str],
    ) -> dict[str, Any]:
        """Tier 2-1: decide whether to stop or what specific gaps to target next."""
        payload = {
            "question": request.question,
            "mode": request.mode,
            "scope": request.scope,
            "depth": request.depth,
            "rounds_done": round_index,
            "rounds_budget": request.max_rounds,
            "source_count": len(ledger.sources),
            "claim_count": len(ledger.claims),
            "plan_lanes": [
                {"lane_id": lane.lane_id, "label": lane.label, "goal": lane.goal}
                for lane in plan.lanes
            ],
            "sources_brief": [
                {
                    "source_id": source.source_id,
                    "url": source.url,
                    "publisher": source.publisher,
                    "lane_id": source.lane_id,
                }
                for source in ledger.sources.values()
            ][:30],
            "source_notes": source_notes[:30],
            "lane_summaries": lane_summaries[:20],
            "evaluator_gaps": evaluator_gaps[:10],
        }
        content = await self._invoke(
            "Reflect on research progress. Return JSON only.",
            (
                "You are a senior research analyst reviewing a mid-investigation evidence ledger. "
                "Decide:\n"
                "1. stop_signal: Have we collected enough evidence to write a confident analyst-grade report? "
                "Set true if rounds_done >= rounds_budget OR coverage is strong AND remaining gaps are minor.\n"
                "2. rationale: 1-2 sentences explaining the decision (what is missing or why it is enough).\n"
                "3. next_queries: When not stopping, 2-5 SPECIFIC follow-up search queries targeting concrete "
                "missing pieces — a number, a regulator stance, a competitor comparison, a technical mechanism. "
                "Avoid vague phrases like 'more sources'. Each query: {query, lane_id, targets} where "
                "targets is the gap this query closes.\n\n"
                "Output JSON only: {stop_signal: bool, rationale: str, next_queries: [{query, lane_id, targets}]}\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
        )
        parsed = _parse_json_object(content)
        if not parsed:
            return {"stop_signal": False, "rationale": "", "next_queries": []}
        return parsed

    async def draft_report(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger,
        evaluation,
        *,
        source_notes: list[dict[str, Any]] | None = None,
        lane_summaries: list[dict[str, Any]] | None = None,
        sections: list[str] | None = None,
    ) -> dict[str, str]:
        """Tier 2-2 Stage A: per-section drafting. Each section gets a focused LLM call
        with source_notes + lane_summaries + section-relevant excerpts."""
        chosen_sections = sections or list(_sections_for_mode(request.mode))
        common_evidence = {
            "request": {
                "question": request.question,
                "mode": request.mode,
                "scope": request.scope,
                "depth": request.depth,
            },
            "source_notes": (source_notes or [])[:30],
            "lane_summaries": (lane_summaries or [])[:20],
            "sources": [
                {
                    "source_id": source.source_id,
                    "title": source.title,
                    "publisher": source.publisher,
                    "url": source.url,
                    "lane_id": source.lane_id,
                    "excerpt": source.content[:6000],
                }
                for source in ledger.sources.values()
            ][:12],
            "claims": [to_jsonable(claim) for claim in ledger.claims][:25],
            "quality_gates": evaluation.quality_gates,
            "gaps": evaluation.gaps[:10],
        }
        drafts: dict[str, str] = {}
        for section_name in chosen_sections:
            content = await self._invoke(
                f"Draft the '{section_name}' section. Markdown only.",
                (
                    "You are drafting a single section of an analyst-grade Deep Research report. "
                    "Output only this section's body (no `#`/`##` heading line) in the user's language.\n\n"
                    f"Section: {section_name}\n"
                    f"Section guidance: {_SECTION_GUIDANCE.get(section_name, 'Weave concrete entities, numbers, and dates from the evidence.')}\n\n"
                    "Hard rules:\n"
                    "- Use specific entities, numbers, and dates from source_notes + lane_summaries.\n"
                    "- Cite source ids inline e.g. [src_ab12] when claims map to a source.\n"
                    "- No generic prose; no padding.\n"
                    "- 2-6 paragraphs (or 3-8 table rows / bullets) typical.\n\n"
                    f"Evidence:\n{json.dumps(common_evidence, ensure_ascii=False)}"
                ),
                mode=request.mode,
            )
            drafts[section_name] = (content or "").strip()
        return drafts

    async def review_report(
        self,
        drafts: dict[str, str],
        *,
        request: ResearchRequest,
        ledger,
        source_notes: list[dict[str, Any]] | None = None,
        lane_summaries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Tier 2-2 Stage B: critic-review the section drafts. Returns dict with
        merged_report (final markdown), quality_score (0-1), and issues list."""
        merged_fallback = _stitch_sections(request, drafts)
        payload = {
            "request": {
                "question": request.question,
                "mode": request.mode,
                "depth": request.depth,
            },
            "drafts": drafts,
            "source_count": len(ledger.sources),
            "claim_count": len(ledger.claims),
            "source_ids": list(ledger.sources)[:20],
            "source_notes": (source_notes or [])[:30],
            "lane_summaries": (lane_summaries or [])[:20],
        }
        content = await self._invoke(
            "Critic-review the section drafts. Return JSON only.",
            (
                "You are a senior research critic reviewing analyst section drafts. Tasks:\n"
                "1. Check every material claim cites a source id; flag missing ones.\n"
                "2. Check concrete numbers and named entities are present (not generic prose).\n"
                "3. Identify internal contradictions or unjustified leaps.\n"
                "4. Tighten language; remove padding; ensure narrative flow.\n"
                "5. Produce a SINGLE well-formed markdown report combining the sections in this order:\n"
                "   # <title>, ## Executive Thesis, ## Method And Source Standard, ## Market Map, "
                "## Key Findings, ## Strategic Implications, ## Contradictions And Gaps, ## Source Ledger.\n"
                "6. Score quality 0-1 (1 = analyst-grade, ready to ship; <0.5 = unfit).\n\n"
                "Return JSON: {merged_report: str, quality_score: float, issues: [str]}.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
        )
        parsed = _parse_json_object(content)
        merged = str(parsed.get("merged_report") or "").strip() if parsed else ""
        try:
            score = float(parsed.get("quality_score")) if parsed and parsed.get("quality_score") is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        issues = parsed.get("issues") if parsed and isinstance(parsed.get("issues"), list) else []
        return {
            "merged_report": merged or merged_fallback,
            "quality_score": score,
            "issues": [str(item) for item in issues][:20],
        }

    async def synthesize_report(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger,
        evaluation,
        *,
        source_notes: list[dict[str, Any]] | None = None,
        lane_summaries: list[dict[str, Any]] | None = None,
    ) -> str | None:
        payload = {
            "question": request.question,
            "mode": request.mode,
            "scope": request.scope,
            "depth": request.depth,
            "source_policy": request.source_policy,
            "plan": to_jsonable(plan),
            "source_notes": source_notes or [],
            "lane_summaries": lane_summaries or [],
            "sources": [
                {
                    "source_id": source.source_id,
                    "title": source.title,
                    "publisher": source.publisher,
                    "url": source.url,
                    "source_type": source.source_type.value,
                    "lane_id": source.lane_id,
                    "excerpt": source.content[:8000],
                }
                for source in ledger.sources.values()
            ],
            "claims": [to_jsonable(claim) for claim in ledger.claims],
            "quality_gates": evaluation.quality_gates,
            "gaps": evaluation.gaps,
        }
        return await self._invoke(
            "Write an analyst-grade Deep Research report.",
            (
                "Write a reusable markdown research report in the user's language. Requirements:\n"
                "- Start with `# <specific title>`.\n"
                "- Include `## Executive Thesis`, `## Method And Source Standard`, "
                "`## Market Map`, `## Key Findings`, `## Strategic Implications`, "
                "`## Contradictions And Gaps`, and `## Source Ledger`.\n"
                "- Every material claim must cite source ids inline, e.g. [src_ab12].\n"
                "- Use the structured `source_notes` (per-source facts: entities, numbers, "
                "dates, mechanisms, limitations) and `lane_summaries` (per-lane evidence "
                "strength, key findings, contradictions) as your primary substrate. Weave "
                "specific entities, numbers, and dates from these notes into every section; "
                "do not paraphrase generically.\n"
                "- Prefer concrete numbers, named actors, product mechanics, and decision implications.\n"
                "- Separate verified findings from inferred implications and gaps.\n"
                "- Do not write generic educational text or ungrounded recommendations.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
        )

    async def _invoke(self, title: str, content: str, *, mode: str | None = None) -> str | None:
        model, fallback_model, agent = await self._resolve_models()
        if model is None or agent is None:
            return None
        result = await invoke_agent(
            AgentInvocationRequest(
                model=model,
                fallback_model=fallback_model,
                messages=[{"role": "user", "content": content}],
                memory_messages=[{"role": "user", "content": content}],
                agent_name=f"{agent.name} Deep Research Internal Analyst",
                role_description=(
                    "Internal Deep Research reasoning worker. Plan, extract, and synthesize from provided evidence only."
                ),
                agent_id=self.agent_id,
                user_id=self.user_id,
                session_context=SessionContext(
                    source="deep_research",
                    channel="internal",
                    metadata={"task": "deep_research_internal_reasoning", "title": title, "mode": mode or ""},
                ),
                system_prompt_suffix=_build_system_prompt_suffix(mode),
                initial_tools=[],
                expand_tools=False,
                core_tools_only=True,
                max_tool_rounds=1,
            )
        )
        return (result.content or "").strip() or None

    async def _resolve_models(self) -> tuple[LLMModel | None, LLMModel | None, Agent | None]:
        async with async_session() as db:
            agent_result = await db.execute(select(Agent).where(Agent.id == self.agent_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                return None, None, None
            model = None
            fallback_model = None
            if agent.primary_model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                model = model_result.scalar_one_or_none()
            if agent.fallback_model_id:
                fallback_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                fallback_model = fallback_result.scalar_one_or_none()
            return model or fallback_model, fallback_model, agent


_DEFAULT_REPORT_SECTIONS = (
    "Executive Thesis",
    "Method And Source Standard",
    "Market Map",
    "Key Findings",
    "Strategic Implications",
    "Contradictions And Gaps",
    "Source Ledger",
)

# T2-3: mode-specific section templates and persona prompts.
_MODE_SECTIONS: dict[str, tuple[str, ...]] = {
    "topic_deep_dive": (
        "Executive Thesis",
        "Method And Source Standard",
        "Topic Profile",
        "Mechanism And Workflow",
        "Competitive Positioning",
        "Key Findings",
        "Strategic Implications",
        "Contradictions And Gaps",
        "Source Ledger",
    ),
    "industry_research": _DEFAULT_REPORT_SECTIONS,
    "source_ledger_audit": (
        "Executive Thesis",
        "Method And Source Standard",
        "Claim Audit Table",
        "Findings By Claim",
        "Limitations",
        "Source Ledger",
    ),
}

_MODE_PERSONAS: dict[str, str] = {
    "topic_deep_dive": (
        "ROLE: topic-deep-dive specialist. Focus on one product, protocol, or firm; "
        "prioritise mechanism, competitive positioning, and decision implications over breadth."
    ),
    "industry_research": (
        "ROLE: market analyst. Use Porter's Five Forces, value-chain, and TAM/SAM thinking "
        "implicitly. Quantify market shares, segment sizes, and competitive intensity. "
        "Name specific entities and concrete numbers in every section."
    ),
    "source_ledger_audit": (
        "ROLE: fact-checker auditor. Cross-reference each claim against the cited source. "
        "Flag jurisdiction-specific or time-bound assertions. Downgrade unsupported claims "
        "rather than asserting them confidently."
    ),
}

# T2-4: universal expert-researcher persona injected on every internal reasoning pass.
_UNIVERSAL_PERSONA = (
    "EXPERT RESEARCH PERSONA — Today is {today}. The user is a highly experienced analyst. "
    "Do not simplify. Mistakes erode trust — be accurate and thorough. "
    "Value good arguments over authorities. Provide detailed explanations, including "
    "concrete numbers, named entities, mechanics, and decision implications. "
    "Flag speculation explicitly. Be proactive — surface angles the user did not ask about."
)


def _sections_for_mode(mode: str | None) -> tuple[str, ...]:
    return _MODE_SECTIONS.get((mode or "").strip().lower(), _DEFAULT_REPORT_SECTIONS)


def _persona_for_mode(mode: str | None) -> str:
    return _MODE_PERSONAS.get((mode or "").strip().lower(), "")


def _build_system_prompt_suffix(mode: str | None) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    parts = [_UNIVERSAL_PERSONA.format(today=today)]
    mode_persona = _persona_for_mode(mode)
    if mode_persona:
        parts.append(mode_persona)
    parts.append(
        "You are running as an internal Deep Research reasoning pass. "
        "Tools are disabled. Do not ask to browse. Use only provided evidence."
    )
    return " ".join(parts)

_SECTION_GUIDANCE = {
    "Executive Thesis": "3-5 sentences. State the most defensible thesis. Name specific actors and numbers.",
    "Method And Source Standard": "1-2 paragraphs. How sources were prioritised (primary > regulator > analyst).",
    "Market Map": "A table or structured bullets. Players per segment, with source ids in the evidence column.",
    "Key Findings": "5-8 findings as bullets. Each cites at least one source id. Numbers and entities required.",
    "Strategic Implications": "3-6 bullets. Implications must follow from cited findings, not generic advice.",
    "Contradictions And Gaps": "Disagreements between sources or unresolved questions. Specific, not vague.",
    "Source Ledger": "Bullet list of `src_xxxx` source id — title — publisher — url for every fetched source.",
}


def _stitch_sections(request: ResearchRequest, drafts: dict[str, str]) -> str:
    """Deterministic fallback stitcher used when the critic LLM fails to return JSON."""
    title = (request.question or "Deep Research").strip()
    lines = [f"# Deep Research: {title}", ""]
    for section in _DEFAULT_REPORT_SECTIONS:
        body = (drafts.get(section) or "").strip()
        if not body:
            continue
        lines.append(f"## {section}")
        lines.append("")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _source_types(value: Any) -> list[SourceType]:
    if not isinstance(value, list):
        return []
    parsed: list[SourceType] = []
    for item in value:
        try:
            parsed.append(SourceType(str(item)))
        except ValueError:
            continue
    return parsed


def _parse_json_object(content: str | None) -> dict[str, Any]:
    parsed = _parse_json(content)
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_array(content: str | None) -> list[Any]:
    parsed = _parse_json(content)
    return parsed if isinstance(parsed, list) else []


def _parse_json(content: str | None) -> Any:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([idx for idx in (text.find("{"), text.find("[")) if idx >= 0], default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None
