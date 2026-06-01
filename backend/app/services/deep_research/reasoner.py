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
from app.services.deep_research.language import resolve_output_language_label
from app.services.deep_research.prompt_craft import REASONING_CALIBRATION, WRITING_QUALITY
from app.services.deep_research.schemas import (
    ResearchLane,
    ResearchPlan,
    ResearchRequest,
    SearchQuery,
    SourceRecord,
    SourceType,
    WorkerResult,
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
            role="planner",
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
                    rationale=str(
                        query.get("rationale") if isinstance(query, dict) else item.get("goal") or ""
                    ).strip(),
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
                "content": source.content[:30000],
            },
        }
        content = await self._invoke(
            "Extract source-bound material claims. Return JSON only.",
            (
                "Return a JSON array of 1-10 material claims that matter to the research question. "
                "Each item must include text, status, source_ids, evidence, and optional notes. "
                "Set `status` by epistemic strength: 'verified' only when the source directly states it, "
                "'inferred' when reasoned from the source, 'unsupported' when not actually backed. Never mark an "
                "inferred claim as verified. "
                "Use only claims directly supported by this source content. If the source is weak, return []. "
                "Do not infer beyond the source.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
            role="researcher",
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
                "content": source.content[:30000],
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
            role="researcher",
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
            "plan_lanes": [{"lane_id": lane.lane_id, "label": lane.label, "goal": lane.goal} for lane in plan.lanes],
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
            role="critic",
        )
        parsed = _parse_json_object(content)
        if not parsed:
            return {"stop_signal": False, "rationale": "", "next_queries": []}
        return parsed

    async def decide_controller_action(
        self,
        *,
        request: ResearchRequest,
        plan: ResearchPlan,
        step_index: int,
        token_used: int,
        token_budget: int,
        current_question: str,
        open_gaps: list[str],
        ledger_summary: dict[str, Any],
        source_notes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Tier 3-1 + Tier 3-2: planner sub-agent picks the next controller action."""
        payload = {
            "request": {
                "question": request.question,
                "mode": request.mode,
                "depth": request.depth,
                "max_sources": request.max_sources,
            },
            "step_index": step_index,
            "token_used": token_used,
            "token_budget": token_budget,
            "budget_used_pct": round(token_used / max(token_budget, 1), 3),
            "current_question": current_question,
            "open_gaps": open_gaps[:5],
            "plan_lanes": [{"lane_id": lane.lane_id, "label": lane.label, "goal": lane.goal} for lane in plan.lanes],
            "ledger_summary": ledger_summary,
            "source_notes": source_notes[:8],
        }
        content = await self._invoke(
            "Pick the next controller action. Return JSON only.",
            (
                "You are the Planner sub-agent for an LLM-controlled Deep Research loop. "
                "Choose ONE action for this step:\n"
                "  - search: queries: [str] (1-3 specific queries closing a named gap)\n"
                "  - visit: urls: [str] (1-3 canonical URLs you already know are authoritative)\n"
                "  - reflect: no payload (call this when coverage looks complete but you want a critic pass)\n"
                "  - answer: no payload (call this when ledger is sufficient for analyst-grade synthesis)\n"
                "Prefer search early, reflect mid-loop, answer when source_count >= max_sources or coverage is solid.\n"
                "Stop fan-out if budget_used_pct >= 0.7.\n\n"
                "Output JSON only: {type: 'search'|'visit'|'reflect'|'answer', rationale: str, queries?: [str], urls?: [str]}\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
            role="planner",
        )
        parsed = _parse_json_object(content)
        if not parsed or not parsed.get("type"):
            return {"type": "reflect", "rationale": "Planner returned no action; defaulting to reflect."}
        return parsed

    async def decide_next(
        self,
        *,
        request: ResearchRequest,
        plan: ResearchPlan,
    ) -> dict[str, Any]:
        """V2 planner: pick bounded worker topics before fan-out."""
        payload = {
            "request": {
                "question": request.question,
                "mode": request.mode,
                "depth": request.depth,
                "scope": request.scope,
                "max_sources": request.max_sources,
                "concurrency": request.concurrency,
            },
            "plan_lanes": [
                {
                    "lane_id": lane.lane_id,
                    "label": lane.label,
                    "goal": lane.goal,
                    "queries": [query.query for query in lane.queries],
                }
                for lane in plan.lanes
            ],
        }
        content = await self._invoke(
            "Select Deep Research v2 worker topics. Return JSON only.",
            (
                "You are the Planner sub-agent for the Deep Research v2 orchestrator-worker runtime. "
                "Return 3-6 independent worker topics. Each topic must be concrete enough for one worker to "
                "search, fetch, and digest source-grounded evidence without delegation. Prefer independent lanes "
                "such as primary issuer evidence, regulator evidence, market/data evidence, technical mechanics, "
                "competitor/customer evidence, or claim-audit evidence.\n\n"
                "Output JSON only: {topics: [str], rationale: str, stop: bool}. Set stop=false unless the "
                "question is impossible or already fully answered by provided context.\n\n"
                f"{json.dumps(payload, ensure_ascii=False)}"
            ),
            mode=request.mode,
            role="planner",
        )
        parsed = _parse_json_object(content)
        if not parsed:
            return {"topics": [], "rationale": "Planner returned no worker topics.", "stop": False}
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
                    "excerpt": source.content[:30000],
                }
                for source in ledger.sources.values()
            ],
            "claims": _compress_claims_for_synthesis(ledger.claims),
            "quality_gates": evaluation.quality_gates,
            "gaps": evaluation.gaps,
        }
        language = resolve_output_language_label(request)
        return await self._invoke(
            "Write an analyst-grade Deep Research report.",
            (
                f"Write a reusable markdown research report in {language}. Translate all evidence into {language}; "
                "keep proper names, tickers, and identifiers in their original form. Never mix languages.\n\n"
                "CORE PRINCIPLE — INTEGRATION, NOT SUMMARIZATION. Synthesize across `source_notes`, "
                "`lane_summaries`, and the source excerpts; never summarize sources one after another or stitch them.\n"
                "Forbidden patterns: sequential summarization ('Source 1 says…; Source 2 says…'); cherry-picking "
                "(surface disconfirming evidence too); unresolved contradictions (resolve by evidence quality, "
                "recency, and scope, or flag as irreconcilable).\n\n"
                "Requirements:\n"
                "- Start with `# <specific title>`; include `## Executive Thesis`, `## Method And Source Standard`, "
                "`## Market Map`, `## Key Findings`, `## Strategic Implications`, `## Contradictions And Gaps`, and "
                "`## Source Ledger`.\n"
                "- Every material claim cites source ids inline, e.g. [src_ab12]; never invent ids.\n"
                "- Weave specific entities, numbers, dates, and mechanisms from `source_notes` and `lane_summaries` "
                "into every section; weight claims by evidence quality (primary/authoritative > strong secondary > "
                "press > weak).\n"
                "- Separate verified findings from inferred implications and gaps. No generic educational text.\n\n"
                + _depth_expectation(request)
                + "\n\n"
                + REASONING_CALIBRATION
                + "\n\n"
                + WRITING_QUALITY
                + "\n\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
            mode=request.mode,
            role="writer",
            max_output_tokens=_SYNTHESIS_MAX_OUTPUT_TOKENS,
        )

    async def synthesize_from_digests(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger,
        evaluation,
        *,
        worker_results: list[WorkerResult],
        source_notes: list[dict[str, Any]] | None = None,
        lane_summaries: list[dict[str, Any]] | None = None,
        devils_advocate: dict[str, Any] | None = None,
    ) -> str | None:
        """V2 final synthesis from worker digests.

        The writer receives compressed worker reports, structured notes, claims,
        and source metadata. It deliberately does not receive large raw source
        excerpts; workers and source-note extraction own source reading.
        """
        payload = {
            "question": request.question,
            "mode": request.mode,
            "scope": request.scope,
            "depth": request.depth,
            "source_policy": request.source_policy,
            "plan": to_jsonable(plan),
            "worker_digests": [
                {
                    "topic": result.topic,
                    "status": result.status,
                    "error": result.error,
                    "tokens_used": result.tokens_used,
                    # Task2: a worker's full digest is the load-bearing synthesis
                    # input; the old 12K cap truncated long workers mid-report. 40K
                    # fits a complete worker report under the restored output budget.
                    "intermediate_report": result.intermediate_report[:40000],
                    "sources": [
                        {
                            "source_id": source.source_id,
                            "title": source.title,
                            "publisher": source.publisher,
                            "url": source.url,
                            "source_type": source.source_type.value,
                            "lane_id": source.lane_id,
                            "fetch_tool": source.fetch_tool,
                            "evidence_tier": source.evidence_tier,
                            "evidence_grade": source.evidence_grade,
                        }
                        for source in result.sources
                    ],
                }
                for result in worker_results
            ],
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
                    "fetch_tool": source.fetch_tool,
                    "evidence_tier": source.evidence_tier,
                    "evidence_grade": source.evidence_grade,
                }
                for source in ledger.sources.values()
            ],
            "claims": _compress_claims_for_synthesis(ledger.claims),
            "quality_gates": evaluation.quality_gates,
            "gaps": evaluation.gaps,
            "devils_advocate": devils_advocate or {},
        }
        instruction = build_digest_synthesis_instruction(request, resolve_output_language_label(request))
        if devils_advocate:
            instruction += (
                "\n\nADVERSARIAL REVIEW — a Devil's Advocate critique of the evidence is in `devils_advocate`. "
                "You MUST address it: neutralise the cherry_picking and confirmation_bias items; for every "
                "missing_warrants item supply the warrant or downgrade the claim; for every overclaim recalibrate "
                "the certainty language; weigh the alternative_explanations; fold the strongest_counter_argument "
                "into `## Contradictions And Gaps`; and either close or explicitly flag every whats_missing item. "
                "Do not silently ignore it."
            )
        return await self._invoke(
            "Write an analyst-grade Deep Research report from worker digests.",
            f"{instruction}\n\n{json.dumps(payload, ensure_ascii=False)}",
            mode=request.mode,
            role="writer",
            max_output_tokens=_SYNTHESIS_MAX_OUTPUT_TOKENS,
        )

    async def devils_advocate_review(
        self,
        request: ResearchRequest,
        plan: ResearchPlan,
        ledger,
        *,
        worker_results: list[WorkerResult],
        lane_summaries: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """P-Q2: one adversarial pass BEFORE synthesis. Domain-general, borrowed from
        academic-research-skills `devils_advocate_agent`: steel-man then stress-test the
        emerging findings so the writer must resolve contradictions and the strongest
        counter-argument instead of cherry-picking a tidy story."""
        payload = {
            "question": request.question,
            "mode": request.mode,
            "scope": request.scope,
            "worker_digests": [
                {
                    "topic": result.topic,
                    "intermediate_report": result.intermediate_report[:20000],
                }
                for result in worker_results
            ],
            "lane_summaries": lane_summaries or [],
            "sources": [
                {
                    "source_id": source.source_id,
                    "publisher": source.publisher,
                    "evidence_tier": source.evidence_tier,
                    "evidence_grade": source.evidence_grade,
                }
                for source in ledger.sources.values()
            ],
            "claims": [to_jsonable(claim) for claim in ledger.claims][:80],
        }
        instruction = (
            "You are the Devil's Advocate on a deep research run (any domain). Steel-man the emerging findings, "
            "then stress-test them BEFORE the final report is written. Be specific, constructive, and cite "
            "source ids where relevant; do not be gratuitously negative.\n"
            "Apply three lenses: (a) Toulmin — for each major claim, is there a WARRANT linking the evidence to "
            "the conclusion, or is it data without a warrant? (b) Epistemic calibration — does the certainty "
            "language match the evidence (preliminary evidence must not be stated as established)? (c) "
            "Inference-to-best-explanation — is the strongest ALTERNATIVE explanation addressed?\n\n"
            "Return JSON only:\n"
            "{\n"
            '  "cherry_picking": [str],            // confirming-only evidence; disconfirming evidence ignored\n'
            '  "confirmation_bias": [str],         // themes selected to fit a desired answer\n'
            '  "missing_warrants": [str],          // claims where the leap from evidence to conclusion is unjustified\n'
            '  "overclaims": [str],                // preliminary/contested evidence stated with established-fact language\n'
            '  "alternative_explanations": [str],  // other credible readings of the same evidence\n'
            '  "strongest_counter_argument": str,  // the single most compelling criticism a hostile expert raises\n'
            '  "whats_missing": [str],             // absent evidence/perspectives that matter\n'
            '  "overrated_claims": [{"claim": str, "why": str}], // claims leaning on tier4/single sources\n'
            '  "so_what": str                      // is the significance justified?\n'
            "}\n\n"
            f"{json.dumps(payload, ensure_ascii=False)}"
        )
        # RC5: a single malformed (non-JSON) critic response used to silently drop the
        # entire adversarial pass. Retry once with an explicit reminder before giving up.
        parsed: dict[str, Any] = {}
        for attempt in range(2):
            reminder = (
                ""
                if attempt == 0
                else "\n\nYour previous reply was not valid JSON. Reply with the JSON object ONLY, no prose."
            )
            content = await self._invoke(
                "Stress-test the research evidence before synthesis. Return JSON only.",
                instruction + reminder,
                mode=request.mode,
                role="critic",
            )
            parsed = _parse_json_object(content)
            if parsed:
                break
        return parsed or None

    async def _invoke(
        self,
        title: str,
        content: str,
        *,
        mode: str | None = None,
        role: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str | None:
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
                    metadata={
                        "task": "deep_research_internal_reasoning",
                        "title": title,
                        "mode": mode or "",
                        "role": role or "",
                    },
                ),
                system_prompt_suffix=_build_system_prompt_suffix(mode, role),
                initial_tools=[],
                expand_tools=False,
                core_tools_only=True,
                disable_tools=True,
                max_tool_rounds=1,
                max_output_tokens=max_output_tokens,
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
                    select(LLMModel).where(
                        LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                    )
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

_ROLE_PERSONAS: dict[str, str] = {
    "planner": (
        "SUB-AGENT ROLE: Planner. Decompose the research question into lanes and "
        "concrete next-step actions. Pick search queries that close specific gaps; "
        "pick visit targets that are already named in evidence. Never produce final prose."
    ),
    "researcher": (
        "SUB-AGENT ROLE: Researcher. Read fetched sources carefully. Extract source-bound "
        "facts, entities, numbers, and dates only. Refuse to infer beyond what the source "
        "supports; downgrade weak claims to inferred or unsupported."
    ),
    "critic": (
        "SUB-AGENT ROLE: Critic. Audit the evidence ledger for completeness, contradictions, "
        "weak attribution, and missing perspectives. Flag where coverage is thin BEFORE the "
        "writer drafts. Output JSON only when the call expects it."
    ),
    "writer": (
        "SUB-AGENT ROLE: Writer. Produce analyst-grade markdown. Every material claim cites "
        "a source id. Use concrete numbers and named entities from the structured notes; "
        "no generic prose. Mark inference and gaps explicitly."
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


def _persona_for_role(role: str | None) -> str:
    return _ROLE_PERSONAS.get((role or "").strip().lower(), "")


def _build_system_prompt_suffix(mode: str | None, role: str | None = None) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    parts = [_UNIVERSAL_PERSONA.format(today=today)]
    mode_persona = _persona_for_mode(mode)
    if mode_persona:
        parts.append(mode_persona)
    role_persona = _persona_for_role(role)
    if role_persona:
        parts.append(role_persona)
    parts.append(
        "You are running as an internal Deep Research reasoning pass. "
        "Tools are disabled. Do not ask to browse. Use only provided evidence."
    )
    return " ".join(parts)


def _depth_expectation(request: ResearchRequest) -> str:
    """Depth-aware report-scale expectation (F4/RC4).

    The production run failed synthesis twice below the 1200-char floor partly because the
    anti-padding guidance pushed the writer too terse. This makes the depth contract explicit:
    full depth = more substantiated content, not more filler.
    """
    depth = (request.depth or "standard").strip().lower()
    if depth in {"full", "flagship", "deep"}:
        return (
            "DEPTH EXPECTATION — this is a FULL-depth report. Develop every major section into a thorough, "
            "multi-section analyst report grounded in concrete evidence; go well beyond a skeleton or a short "
            "brief. Depth means MORE substantiated content (specific numbers, named entities, mechanisms, and "
            "resolved contradictions) — it is NOT a licence for filler or padding. Each paragraph must earn its place."
        )
    if depth in {"quick", "light"}:
        return (
            "DEPTH EXPECTATION — this is a QUICK-depth report. Be concise: lead with the most decision-relevant "
            "findings as a tight, well-supported brief."
        )
    return (
        "DEPTH EXPECTATION — this is a STANDARD-depth report. Develop each section with concrete evidence into a "
        "complete analyst brief, not a skeleton."
    )


# Task1: the final report must not truncate. A full analyst report (every worker
# dimension + cross-cutting analysis + so-what + ledger) runs well past a model's
# chat-default output ceiling — f733867 capped near 9K chars, one dimension short
# of its mandated Contradictions/Ledger sections. 32768 is a report-grade budget
# modern long-context models support; get_max_tokens still clamps it to
# MAX_OUTPUT_TOKENS_HARD_LIMIT.
_SYNTHESIS_MAX_OUTPUT_TOKENS = 32768


def build_digest_synthesis_instruction(request: ResearchRequest, language: str) -> str:
    """Final-report synthesis instruction — the anti-stitch DNA: integration, not summarization.

    Domain-general, borrowed from academic-research-skills `synthesis_agent`: explicit
    anti-patterns (sequential summarization / cherry-picking / unresolved contradictions),
    a convergence -> divergence -> resolution -> gap process, and evidence-weighted writing.
    """
    return (
        f"Write the FINAL Deep Research report in {language}. Translate all evidence into {language}; "
        "keep proper names, tickers, and identifiers in their original form. Never mix languages.\n\n"
        "CORE PRINCIPLE — INTEGRATION, NOT SUMMARIZATION. You are writing ONE coherent analyst report from "
        "multiple worker digests. Do NOT stitch, concatenate, or list the digests one after another. The worker "
        "digests are your EVIDENCE; the report is your ARGUMENT — it must carry a through-line thesis across "
        "dimensions, not place per-worker summaries side by side.\n"
        "Priority when these collide: (1) INTEGRATION — a through-line thesis carried across dimensions plus a "
        "clear so-what; (2) COVERAGE — every dimension represented; (3) DEPTH within any one dimension. Coverage "
        "is the floor, not the spine: never drop a dimension, but never let 'cover every dimension' degrade into "
        "'dimension 1 found X, dimension 2 found Y'.\n"
        "Forbidden patterns (reject your own draft if it does any of these):\n"
        "- Sequential summarization: 'Worker 1 found X. Worker 2 found Y.' Instead integrate across sources: "
        "'Converging evidence establishes X, operating through mechanism Y, though Z moderates it when ...'.\n"
        "- Worker-by-worker or source-by-source structure: never organize the final report as one block per worker, "
        "one block per lane, or one paragraph per source. That is a 资料汇编 / evidence packet, not research.\n"
        "- Cherry-picking: do not report only confirming evidence; surface disconfirming evidence and weigh it.\n"
        "- Unresolved contradictions: when sources disagree, resolve by comparing evidence quality, recency, and "
        "scope — or explicitly flag the disagreement as irreconcilable.\n"
        "- Covered but spineless: if a reader cannot restate your central judgment and what to DO about it, you "
        "have catalogued the dimensions, not analyzed them.\n\n"
        "Method before writing: first build an internal synthesis brief, then write. The brief must include: "
        "(1) CENTRAL THESIS; (2) EVIDENCE MATRIX mapping each major claim to source ids, evidence tier/grade, "
        "and the WARRANT explaining why the evidence supports the claim; (3) CONTRADICTION MAP with the strongest "
        "counter-evidence and how you resolve or downgrade it; (4) IMPLICATION MAP answering the so-what / what "
        "decision the evidence supports; (5) REPORT OUTLINE. Do not print the brief as JSON; use it to write the "
        "markdown report. Then write an integrated argument that leads with the strongest, best-supported themes "
        "and weights claims by evidence quality (primary/authoritative > strong secondary > press > weak).\n\n"
        "Hard requirements:\n"
        "- Weight by the per-source `evidence_tier`/`evidence_grade`: tier1 (primary/authoritative) and tier2 "
        "(strong secondary) carry the argument; a tier4 (blog/social) source must NOT be the sole support for any "
        "key claim — corroborate it or mark the claim inferred.\n"
        "- Cite ONLY source ids present in `sources` / worker digest source metadata; never invent ids. Every "
        "material claim cites a source id inline, e.g. [src_ab12].\n"
        "- Sections in order: `# <specific title>`, `## Executive Thesis` (state the central judgment up front), "
        "`## Method And Source Standard`, `## Cross-Cutting Analysis` (THE analytical core — organize by the 2-4 "
        "tensions/through-lines from step 5, NOT by worker; each subsection pulls evidence from MULTIPLE dimensions "
        "to advance the thesis), `## Key Findings` (the per-dimension evidence base — see coverage rule), "
        "`## Contradictions And Gaps`, `## Strategic Implications` (the so-what — what the findings MEAN and which "
        "decision they support, not a recap), `## Source Ledger`; add mode-specific sections when useful.\n"
        "- COVERAGE IS MANDATORY (the floor): `## Key Findings` must contain a distinct `###` subsection for EVERY "
        "research dimension present in the worker digests (one per worker topic/lane). Never collapse or silently "
        "drop a dimension — a thin dimension gets a short honest subsection, not omission. But these subsections "
        "are the EVIDENCE BASE supporting the thesis from `## Cross-Cutting Analysis`, not the report's spine. "
        "Integrate sources WITHIN each subsection (the no-stitching rule still holds).\n"
        "- `## Cross-Cutting Analysis` and `## Strategic Implications` must deliver JUDGMENT — what the evidence "
        "means, the trade-offs, the so-what — not restate the per-dimension findings.\n"
        "- Use concrete numbers, named entities, dates, and mechanisms. Separate verified findings from inferred "
        "implications and gaps.\n"
        "- If evidence is insufficient, say so explicitly instead of padding with generic prose.\n"
        "- OUTPUT FORMAT: return ONLY the markdown report, starting with `# `. Do NOT wrap it in a "
        'tool call or any XML/JSON envelope (no `<FileWriter>`, `<write_file>`, `content="..."`, or '
        "similar) — tools are disabled, so any such wrapper is persisted verbatim as garbage.\n\n"
        + _depth_expectation(request)
        + "\n\n"
        + REASONING_CALIBRATION
        + "\n\n"
        + WRITING_QUALITY
    )


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


def _compress_claims_for_synthesis(claims: list, *, limit: int = 200) -> list[dict[str, Any]]:
    """Trim the claim ledger to its load-bearing fields for the synthesis payload.

    Workers already digested the evidence; the writer needs each claim's text +
    source ids + status, not the extraction evidence/notes. Contradictions are
    surfaced first because the report must resolve them.

    Task2: RC10 originally capped this at 60 on the theory that a 128K claim payload
    overflowed the writer and collapsed its output. RC11 later proved the real cause
    was tool exposure, not payload size — so the aggressive cap was over-fitting a
    misdiagnosis. With output budget restored (Task1) and long-context models, 200
    keeps effectively the whole ledger while still bounding a pathological run.
    """
    prioritized = sorted(claims, key=lambda c: 0 if getattr(c, "contradiction_group", None) else 1)
    compressed: list[dict[str, Any]] = []
    for claim in prioritized[:limit]:
        entry: dict[str, Any] = {
            "claim_id": getattr(claim, "claim_id", ""),
            "text": getattr(claim, "text", ""),
            "source_ids": list(getattr(claim, "source_ids", []) or []),
            "status": str(getattr(claim, "status", "")),
        }
        group = getattr(claim, "contradiction_group", None)
        if group:
            entry["contradiction_group"] = group
        compressed.append(entry)
    return compressed


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
