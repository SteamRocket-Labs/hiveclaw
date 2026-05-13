from __future__ import annotations

import json
import uuid
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
        )
        parsed = _parse_json_object(content)
        if not parsed:
            return None
        parsed["source_id"] = source.source_id
        return parsed

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
        )

    async def _invoke(self, title: str, content: str) -> str | None:
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
                    metadata={"task": "deep_research_internal_reasoning", "title": title},
                ),
                system_prompt_suffix=(
                    "You are running as an internal Deep Research reasoning pass. "
                    "Tools are disabled. Do not ask to browse. Use only provided evidence."
                ),
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
