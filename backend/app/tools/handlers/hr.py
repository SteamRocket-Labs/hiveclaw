"""HR tools — create digital employees through conversational guidance."""

from __future__ import annotations
# ruff: noqa: F401 -- this facade explicitly supplies runner dependencies per call.

import json
import hashlib
import logging
import re
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.services.archetype import Archetype, apply_archetype_defaults

from app.api.skills import _fetch_github_directory, _get_github_token, _parse_github_url
from app.services.capability_reuse_service import reuse_existing_skill_for_agent
from app.services.external_capabilities.skill_source_adapter import stage_external_skill_package_review_for_tenant
from app.services import plan_mode_core
from app.services.subprocess_env import build_agent_subprocess_env
from app.services.code_execution.service import execute_agent_command
from app.services.skill_seeder import DEFAULT_BUILTIN_SKILL_FOLDERS, DEFAULT_PACK_SKILL_FOLDERS
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

logger = logging.getLogger(__name__)

ROLE_DESCRIPTION_MAX_CHARS = 4000
HR_LONG_TEXT_MAX_CHARS = 4000

UNKNOWN_SOURCE_ATTRIBUTION_TYPE = "unknown_or_needs_company_source"
COMPANY_SOURCE_ATTRIBUTION_TYPE = "supported_by_company_kb"

SOURCE_ATTRIBUTION_TYPES = [
    "confirmed_by_user",
    COMPANY_SOURCE_ATTRIBUTION_TYPE,
    "suggested_by_history",
    "suggested_by_general_knowledge",
    UNKNOWN_SOURCE_ATTRIBUTION_TYPE,
]

SOURCE_ATTRIBUTIONS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "description": "Blueprint field this source supports, e.g. boundaries, focus_content, core_outputs.",
            },
            "value_summary": {
                "type": "string",
                "description": "Short summary of the proposed value that this source supports.",
            },
            "source_type": {
                "type": "string",
                "enum": SOURCE_ATTRIBUTION_TYPES,
                "description": (
                    "Whether the value is user-confirmed, supported by freshly authorized Company Knowledge, "
                    "historical, general, or unresolved. "
                    "If omitted, the server records it as unresolved knowledge debt."
                ),
            },
            "source_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Evidence refs such as kb://..., t3:memory/..., explicit:..., or external source refs.",
            },
        },
        "required": ["field"],
    },
    "description": (
        "Source attribution for substantive blueprint content. Company Knowledge references require a fresh "
        "cite decision; history is advisory, and the canonical draft must be shown to the user for confirmation."
    ),
}


def _trim_role_description_for_prompt_guard(value: object) -> str:
    """Normalize without silently rewriting model-authored content.

    The tool schema owns the explicit maxLength boundary. Internal callers and
    tests that bypass schema validation retain the full value for evidence.
    """
    return str(value or "").strip()


def _stamp_hr_blueprint_trigger_exemption(config: dict | None) -> dict:
    cfg = dict(config or {})
    metadata = dict(cfg.get("metadata") or {})
    metadata["plan_exempt_reason"] = plan_mode_core.PLAN_EXEMPT_CONFIRMED_HR_BLUEPRINT
    cfg["metadata"] = metadata
    return cfg


def _default_skill_count() -> int:
    return len(DEFAULT_BUILTIN_SKILL_FOLDERS) + len(DEFAULT_PACK_SKILL_FOLDERS)


def _default_ready_now() -> list[str]:
    return [
        f"builtin tools + {_default_skill_count()} default skills",
        "workspace, memory, heartbeat, and self-evolution scaffolding",
    ]


_SKILLS_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")


def _parse_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except (ValueError, TypeError):
                logger.debug("[HR] Failed to parse JSON list: %s", raw[:80])
    return []


def _parse_external_skill_urls(value) -> list[str]:
    return _dedupe_strings([item for item in _parse_list(value) if isinstance(item, str)])


def _is_external_skill_ref(value: str) -> bool:
    item = str(value).strip()
    return bool(_parse_github_url(item) or _SKILLS_REF_RE.match(item))


def _split_requested_skill_inputs(values: list[str]) -> tuple[list[str], list[str]]:
    platform_skills: list[str] = []
    external_refs: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        if _is_external_skill_ref(item):
            external_refs.append(item)
        else:
            platform_skills.append(item)
    return _dedupe_strings(platform_skills), _dedupe_strings(external_refs)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _parse_source_attributions(
    value: object,
    *,
    validated_company_source_refs: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize source attributions and reject invalid source types.

    These annotations are advisory/provenance metadata for the blueprint. They
    let the HR flow distinguish company knowledge, historical suggestions, and
    unresolved knowledge debt without letting any non-current-session source
    silently become a confirmed decision.
    """
    raw_items = _parse_list(value)
    normalized: list[dict[str, Any]] = []
    warnings: list[str] = []
    invalid_count = 0
    defaulted_source_type_count = 0
    for item in raw_items:
        if not isinstance(item, dict):
            invalid_count += 1
            continue
        source_type = str(item.get("source_type") or "").strip()
        field = str(item.get("field") or "").strip()
        if not field:
            invalid_count += 1
            continue
        source_refs = _dedupe_strings([ref for ref in _parse_list(item.get("source_refs")) if isinstance(ref, str)])
        if not source_type:
            source_type = UNKNOWN_SOURCE_ATTRIBUTION_TYPE
            defaulted_source_type_count += 1
        elif source_type == COMPANY_SOURCE_ATTRIBUTION_TYPE and (
            not source_refs
            or validated_company_source_refs is None
            or any(ref not in validated_company_source_refs for ref in source_refs)
        ):
            source_type = UNKNOWN_SOURCE_ATTRIBUTION_TYPE
            warnings.append(
                "supported_by_company_kb requires fresh accessible company evidence; attribution was recorded "
                "as unknown_or_needs_company_source"
            )
        elif source_type not in SOURCE_ATTRIBUTION_TYPES:
            invalid_count += 1
            continue
        entry: dict[str, Any] = {
            "field": field,
            "source_type": source_type,
            "value_summary": str(item.get("value_summary") or "").strip(),
            "source_refs": source_refs,
        }
        normalized.append(entry)
    if invalid_count:
        warnings.append(f"invalid source_attributions ignored: {invalid_count}")
    if defaulted_source_type_count:
        warnings.append(
            "missing source_attributions source_type defaulted to "
            f"{UNKNOWN_SOURCE_ATTRIBUTION_TYPE}: {defaulted_source_type_count}"
        )
    return normalized, warnings


def _knowledge_debt_from_source_attributions(source_attributions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "field": item["field"],
            "value_summary": item.get("value_summary", ""),
            "source_type": "unknown_or_needs_company_source",
            "reason": "unknown_or_needs_company_source",
        }
        for item in source_attributions
        if item.get("source_type") == "unknown_or_needs_company_source"
    ]


def _source_attribution_policy() -> dict[str, Any]:
    return {
        "company_knowledge_lane": "governed_tool_only_fresh_cite",
        "history_suggestion_lane": "advisory",
        "general_knowledge_lane": "fallback",
        "confirmation_rule": (
            "Company Knowledge attributions require a fresh cite decision. The exact canonical draft, including "
            "history or general-knowledge suggestions, must be presented to the user and explicitly confirmed."
        ),
        "source_type_precedence": [
            "confirmed_by_user",
            COMPANY_SOURCE_ATTRIBUTION_TYPE,
            "suggested_by_history",
            "suggested_by_general_knowledge",
            "unknown_or_needs_company_source",
        ],
    }


def _confirmation_requirements(source_attributions: list[dict[str, Any]]) -> dict[str, Any]:
    source_types = _dedupe_strings([str(item.get("source_type")) for item in source_attributions])
    return {
        "must_present_all_substantive_blueprint_content": True,
        "source_types_to_present": source_types,
        "company_kb_attribution_available": True,
        "must_confirm_before_create": True,
    }


def _company_evidence_id(source_ref: str) -> uuid.UUID | None:
    rendered = str(source_ref or "").strip()
    prefix = "company-evidence://"
    if not rendered.startswith(prefix) or "#" in rendered:
        return None
    try:
        identifier = uuid.UUID(rendered.removeprefix(prefix))
    except (TypeError, ValueError):
        return None
    return identifier if rendered == f"{prefix}{identifier}" else None


async def _verify_company_kb_source_refs(
    *,
    session: Any,
    principal: Any,
    source_attributions: object,
    trace_id: str,
    gateway: Any | None = None,
) -> set[str]:
    """Return Company Knowledge refs that pass a fresh cite decision."""
    from app.services.company_knowledge_gateway import (
        CompanyKnowledgeGateway,
        CompanyKnowledgeSourceExplainRequest,
    )

    candidates: dict[str, uuid.UUID] = {}
    for item in _parse_list(source_attributions):
        if not isinstance(item, dict) or str(item.get("source_type") or "").strip() != COMPANY_SOURCE_ATTRIBUTION_TYPE:
            continue
        for raw_ref in _parse_list(item.get("source_refs")):
            if not isinstance(raw_ref, str):
                continue
            source_ref = raw_ref.strip()
            evidence_id = _company_evidence_id(source_ref)
            if evidence_id is not None:
                candidates[source_ref] = evidence_id

    verifier = gateway or CompanyKnowledgeGateway()
    verified: set[str] = set()
    for ordinal, (source_ref, evidence_id) in enumerate(candidates.items()):
        result = await verifier.explain_source(
            session,
            principal=principal,
            request=CompanyKnowledgeSourceExplainRequest(
                evidence_id=evidence_id,
                trace_id=f"{trace_id}:company-source:{ordinal}"[:300],
            ),
        )
        payload = result.payload if result.status == "ok" and isinstance(result.payload, dict) else {}
        coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
        if payload.get("source_ref") == source_ref and coverage.get("complete") is True:
            verified.add(source_ref)
    return verified


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _blueprint_hash(blueprint: dict) -> str:
    from app.services.hr_creation_service import canonical_hr_blueprint_hash

    return canonical_hr_blueprint_hash(blueprint)


def _text_blob(*values: object) -> str:
    chunks: list[str] = []
    for value in values:
        if isinstance(value, list):
            chunks.extend(str(item) for item in value)
        elif isinstance(value, dict):
            chunks.append(_canonical_json(value))
        elif value is not None:
            chunks.append(str(value))
    return f" {' '.join(chunks).lower()} "


def _classify_creation_risk(
    *,
    role_description: str,
    primary_users: list[str],
    core_outputs: list[str],
    focus_content: str,
    triggers: list[dict],
    welcome_message: str,
    declared_risk_class: str = "standard",
) -> str:
    del role_description, primary_users, core_outputs, focus_content, triggers, welcome_message
    risk_class = str(declared_risk_class or "standard").strip().lower()
    if risk_class not in {"standard", "high"}:
        raise ValueError(f"invalid risk_class: {declared_risk_class!r}")
    return risk_class


def _build_creation_flow_gates(
    *,
    name: str,
    role_description: str,
    primary_users: list[str],
    core_outputs: list[str],
    boundaries: str,
    focus_content: str,
    triggers: list[dict],
    risk_class: str,
    manual_steps: list[str],
    install_now_skill_names: list[str],
    mcp_server_ids: list[str],
    clawhub_slugs: list[str],
    external_skill_refs: list[str],
) -> tuple[dict, list[str]]:
    identity_missing = []
    if len(name) < 2 or len(name) > 100:
        identity_missing.append("name")
    if not role_description:
        identity_missing.append("role_description")
    if not primary_users:
        identity_missing.append("primary_users")
    if not core_outputs:
        identity_missing.append("core_outputs")

    governance_missing = []
    if risk_class == "high" and not boundaries:
        governance_missing.append("boundaries")

    activation_missing = []
    if not focus_content and not triggers:
        activation_missing.append("first_objective")

    capability_notes = []
    if manual_steps:
        capability_notes.append("manual_setup_debt")
    if install_now_skill_names or mcp_server_ids or clawhub_slugs or external_skill_refs:
        capability_notes.append("day_one_installs")

    gates = {
        "identity": {
            "label": "Identity gate",
            "status": "missing" if identity_missing else "complete",
            "missing": identity_missing,
        },
        "governance": {
            "label": "Governance gate",
            "status": "missing" if governance_missing else "complete",
            "missing": governance_missing,
            "risk_class": risk_class,
        },
        "activation": {
            "label": "Activation gate",
            "status": "missing" if activation_missing else "complete",
            "missing": activation_missing,
        },
        "capabilities": {
            "label": "Capability / Setup Debt gate",
            "status": "complete",
            "notes": capability_notes,
        },
        "confirmation": {
            "label": "Preview + Confirmation gate",
            "status": "pending",
            "missing": ["explicit_user_confirmation"],
        },
    }
    missing_gates = [gate for gate, data in gates.items() if data["status"] == "missing"]
    return gates, missing_gates


async def _resolve_employee_creation_model(db, tenant_id: uuid.UUID | None):
    """Resolve a valid enabled model for a newly created employee."""
    from sqlalchemy import select

    from app.models.llm import LLMModel
    from app.services.model_resolution import resolve_default_model_for_tenant

    if tenant_id is not None:
        return await resolve_default_model_for_tenant(db, tenant_id)

    model_result = await db.execute(
        select(LLMModel)
        .where(
            LLMModel.tenant_id.is_(None),
            LLMModel.enabled.is_(True),
        )
        .order_by(LLMModel.created_at.asc())
        .limit(1)
    )
    return model_result.scalar_one_or_none()


async def _resolve_employee_refinement_model(
    db,
    tenant_id: uuid.UUID | None,
    *,
    preferred_model_id: uuid.UUID | None,
    creation_model,
) -> tuple[object | None, str]:
    """Use the HR agent model only when it is valid for the current tenant."""
    from sqlalchemy import select

    from app.models.llm import LLMModel

    if preferred_model_id is not None:
        predicates = [
            LLMModel.id == preferred_model_id,
            LLMModel.enabled.is_(True),
        ]
        if tenant_id is None:
            predicates.append(LLMModel.tenant_id.is_(None))
        else:
            predicates.append(LLMModel.tenant_id == tenant_id)
        model_result = await db.execute(select(LLMModel).where(*predicates))
        preferred_model = model_result.scalar_one_or_none()
        if preferred_model is not None:
            return preferred_model, "hr_agent"
        logger.warning(
            "[HR] Ignoring unavailable HR refinement model %s for tenant %s",
            preferred_model_id,
            tenant_id,
        )

    return creation_model, "tenant_default"


_SOUL_REFINE_PROMPT = """\
<role>
You are the identity architect for Hive, a multi-agent collaboration platform.
You craft the foundational identity contract (soul.md) that defines WHO a new
digital employee IS — for their entire lifetime.
</role>

<pipeline_context>
Every agent has a 4-layer memory pyramid that runs automatically:

  T0 (append-only session ledger, 30d) → T2 (episodic learnings) → T3 (semantic memory) → soul.md (identity)

- **soul.md is the TOP**: most permanent, most condensed layer.
- Conversations extract into T2 after each response (automatic).
- Heartbeat (every ~45 min) curates T2 into T3 (feedback, knowledge, strategies, blocked patterns).
- Dream (~4h + 3 sessions) consolidates T3 and may promote insights INTO soul.md.
- User corrections and confirmed patterns are the highest-value signals.

**Why your output matters**: soul.md becomes the frozen prefix of every prompt
this agent ever receives. A vague soul → every downstream layer inherits the
vagueness. A rich, role-specific soul → T2/T3/dream all have a clear semantic
basin to cluster around. You are setting the agent's gravitational center.

**What belongs in soul.md**: durable identity, role mission, personality as
observable behaviors, hard boundaries, user/output contracts, quality standards.

**What does NOT belong** (these are volatile and live in triggers or the agent's
work ledger): current tasks, tool configs, trigger schedules, capability lists,
dates, temporary priorities, current events.
</pipeline_context>

<raw_inputs>
- Name: {name}
- Role description: {role_description}
- Personality/style: {personality}
- Boundaries: {boundaries}
- Primary users: {primary_users}
- Core outputs: {core_outputs}
</raw_inputs>

<output_schema>
Produce a single JSON object with EXACTLY these keys (no extras, no omissions):

  "role_description"   : string (3-5 sentences)
  "personality"        : string (newline-separated, 4-6 observable behaviors)
  "boundaries"         : string (newline-separated, 3-5 hard rules)
  "primary_users"      : list[string] (2-4 items, each with "who + why")
  "core_outputs"       : list[string] (3-5 items, named deliverables with shape)
  "quality_standards"  : list[string] (3-4 items, role-specific criteria)
  "first_tasks"        : list[string] (EXACTLY 3 items, concrete first assignments)

Output valid JSON only. No markdown fences, no prose outside the JSON.
</output_schema>

<field_requirements>
**role_description**
What this agent does, in what domain, supporting what decisions.
- BAD: "Investment Research Analyst" (title, zero depth)
- GOOD: "Covers primary-market investment research in AI infrastructure and
  semiconductor sectors. Core responsibilities: target screening, industry
  mapping, competitive landscape analysis to help investment managers make fast
  judgments during deal sourcing. All research outputs must be traceable to
  primary data sources with no subjective predictions."

**personality** — observable operating behaviors, NOT abstract traits.
- BAD: "Rigorous, professional, efficient" (adjectives tell the agent nothing)
- GOOD:
  "Cite source and date every time data is referenced; flag data older than 30 days as [stale]
  When sources conflict, list all perspectives rather than picking one
  When a task exceeds capability, state the boundary clearly instead of producing low-quality output
  Before delivering, self-check: is every conclusion data-backed? Is every suggestion actionable?"

**boundaries** — hard rules specific to THIS role's risk profile.
Think: what goes wrong if this agent is careless?
- BAD: "Do not lie" (generic, applies to every agent)
- GOOD (research role):
  "Never fabricate data sources, company information, or financial figures
  When referencing non-public information, always label confidence level
  Do not give buy/sell recommendations on specific targets; provide frameworks and evidence only"

**primary_users** — specific groups with what they need.
- BAD: ["The team"]
- GOOD: ["Investment managers (need fast target screening results and sector updates)",
  "Partners (need weekly sector overviews and key deal tracking)"]

**core_outputs** — named deliverables with enough detail to judge quality.
- BAD: ["Reports"]
- GOOD: ["Target screening card (company overview + key metrics + preliminary assessment, 1 page max)",
  "Weekly sector brief (funding events + policy updates + signals worth watching)",
  "Deep-dive research memo (competitive landscape + moat analysis + risk checklist, for IC discussion)"]

**quality_standards** — role-specific criteria. Dream uses these when evaluating work.
- GOOD: ["Every analytical conclusion must be traceable to at least one data source",
  "Recommendations must include actionable next steps and risk flags"]

**first_tasks** — exactly 3 concrete first assignments (drive the boot trigger, not soul).
Each task must start with builtin/default capabilities when possible.
- BAD: ["Read soul.md", "Check capabilities", "Do something useful"]
- GOOD (research role):
  ["Compile an overview of this week's top 5 AI infrastructure funding rounds with source links",
   "Build a competitive landscape draft for one target company in the semiconductor sector",
   "Produce a template for the weekly sector brief and populate it with this week's data"]
</field_requirements>

<few_shot_examples>
**Example 1 — Research role with thin inputs**

Raw inputs:
- Name: 小研
- Role description: 投研分析师
- Personality/style: 严谨
- Boundaries: (not specified)
- Primary users: 投资经理
- Core outputs: 研报

Good output (partial, JSON):
```json
{{
  "role_description": "覆盖一级市场 AI 基础设施与半导体赛道的投研分析师。核心职责：标的筛选、行业图谱、竞品格局分析，帮助投资经理在 deal sourcing 阶段做出快速判断。所有输出必须可回溯到一手数据源，不做主观预测。",
  "personality": "引用数据必附来源和日期，超过 30 天的数据标记 [stale]\\n资料冲突时并列所有口径，不自行取舍\\n能力边界触达时明确说明，而不是交付低质量产物\\n交付前自检：每个结论是否有数据支撑？每个建议是否可执行？",
  "boundaries": "不编造数据源、公司信息或财务数字\\n引用非公开信息（如融资传闻）必须标注置信等级\\n不针对具体标的给出买入/卖出建议，只提供分析框架与证据",
  "primary_users": [
    "投资经理（需要快速的标的筛选结果和赛道动态）",
    "合伙人（需要周度赛道综述和重点 deal 跟踪）"
  ],
  "core_outputs": [
    "标的筛选卡（公司概览+关键指标+初步判断，1 页以内）",
    "周度赛道简报（融资事件+政策更新+值得关注的信号）",
    "深度研究备忘（竞品格局+护城河+风险清单，用于 IC 讨论）"
  ],
  "quality_standards": [
    "每个分析结论必须可回溯到至少一个数据源",
    "建议必须含可执行的下一步动作和风险提示"
  ],
  "first_tasks": [
    "汇总本周 AI 基础设施赛道融资 Top 5，附来源链接",
    "对半导体赛道一家标的公司起草竞品格局初稿",
    "产出周度赛道简报模板并用本周数据填充"
  ]
}}
```

**Example 2 — Execution role with rich inputs**

Raw inputs:
- Name: opsbot
- Role description: DevOps engineer handling CI/CD and infrastructure
- Personality/style: methodical, defensive, document everything
- Boundaries: must not touch prod without approval
- Primary users: engineering team
- Core outputs: deployment reports, postmortems

Good output (partial, JSON):
```json
{{
  "role_description": "DevOps engineer responsible for CI/CD pipelines, infrastructure automation, and incident response across the engineering org. Handles deploy orchestration, configuration drift detection, and post-incident analysis. Optimizes for deployment reliability and blast-radius containment over velocity.",
  "personality": "Read the relevant runbook before touching any production-adjacent system\\nPrefer reversible changes; when irreversible is required, document the rollback path FIRST\\nEvery production change must have a linked ticket, an owner, and a rollback plan\\nWhen diagnosing, reproduce the failure in staging before proposing a fix\\nWrite every postmortem as if a new hire will read it six months from now",
  "boundaries": "Never apply changes to production without explicit approval in the ticket\\nNever skip the staging verification step, even for 'trivial' changes\\nNever disable alerts, safety guards, or CI gates without a follow-up task to restore them\\nNever share secrets, credentials, or access tokens in chat or reports",
  "primary_users": [
    "Backend engineers (need fast, predictable deploys and clear failure signals)",
    "On-call engineers (need actionable runbooks and postmortems that prevent recurrence)",
    "Engineering managers (need deployment-health metrics and incident trends)"
  ],
  "core_outputs": [
    "Deployment report (services touched, verification evidence, rollback plan status)",
    "Incident postmortem (timeline, root cause, contributing factors, action items with owners)",
    "Infrastructure-change proposal (diff, blast radius, verification plan, rollback)"
  ],
  "quality_standards": [
    "Every production change has a reviewable diff, verification evidence, and a tested rollback",
    "Every postmortem identifies at least one systemic fix, not just a patch",
    "Every runbook is dated and links to the latest incident it was validated against"
  ],
  "first_tasks": [
    "Audit the current CI pipeline and produce a diagram of build stages, gates, and blockers",
    "Draft a deployment report template aligned with our existing postmortem format",
    "Survey the three most recent incidents and extract common root-cause patterns"
  ]
}}
```
</few_shot_examples>

<anti_patterns>
DO NOT do any of these:

- ❌ Generic identity that would apply to any agent
  ("Be professional and helpful" — useless; every agent already is)
- ❌ Title-only role_description ("Sales analyst" — tells agent nothing)
- ❌ Adjective soup for personality ("rigorous, efficient, detail-oriented"
  — LLMs can't execute adjectives; behaviors are actionable)
- ❌ Date-anchored content ("Focus on Q3 2026 targets" — soul must survive 6+ months)
- ❌ Tool-name boundaries ("Always use web_search" — tools live in operational guidance,
  not soul. soul is identity, not config.)
- ❌ first_tasks that are self-referential setup
  ("Read your soul.md, introduce yourself, list your capabilities")
- ❌ Empty-string fields — if the user left a field blank, INFER a rich
  default from surrounding context. Name alone carries signal.
- ❌ Output with markdown fences, prose commentary, or trailing text —
  the caller parses raw JSON and any extra chars will break parsing.
</anti_patterns>

<hard_rules>
1. **Language match**: inputs Chinese → output Chinese; inputs English → output
   English. Never mix languages within a single field.
2. **Role specificity**: every field must be recognizable as THIS role's
   content, not generic digital-employee boilerplate.
3. **Infer when empty**: empty/vague inputs trigger inference from whatever
   signal remains (name, any non-empty field, platform defaults). Never emit
   empty strings or placeholders.
4. **Durability**: soul must still make sense 6 months from now. No dates,
   no current events, no temporary priorities.
5. **JSON-only output**: valid JSON, all 7 keys present, no fences, no prose
   outside the object. The caller consumes `json.loads(response)` directly.
</hard_rules>
"""


async def _refine_soul_inputs(
    *,
    name: str,
    role_description: str,
    personality: str,
    boundaries: str,
    primary_users: list[str],
    core_outputs: list[str],
    model_config: dict,
    usage_agent_id: uuid.UUID | None = None,
    usage_tenant_id: uuid.UUID | None = None,
) -> dict:
    """Use LLM to refine raw HR inputs into rich soul contract content.

    The soul is the most important file in an agent's lifecycle — the permanent
    identity at the top of the 4-layer memory pyramid. This function ensures
    every agent is born with a high-quality identity contract, even when the
    HR conversation provides minimal inputs.

    Returns refined dict with same keys + quality_standards. Falls back to raw inputs on failure.
    """
    raw = {
        "role_description": role_description,
        "personality": personality,
        "boundaries": boundaries,
        "primary_users": primary_users,
        "core_outputs": core_outputs,
        "quality_standards": [],
        "first_tasks": [],
    }
    if not model_config:
        return raw

    prompt = _SOUL_REFINE_PROMPT.format(
        name=name,
        role_description=role_description or "(not specified)",
        personality=personality or "(not specified)",
        boundaries=boundaries or "(not specified)",
        primary_users=", ".join(primary_users) if primary_users else "(not specified)",
        core_outputs=", ".join(core_outputs) if core_outputs else "(not specified)",
    )

    try:
        from app.services.llm_client import chat_complete, get_max_tokens

        response = await chat_complete(
            provider=model_config["provider"],
            api_key=model_config["api_key"],
            model=model_config["model"],
            base_url=model_config.get("base_url"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=get_max_tokens(
                str(model_config.get("provider") or ""),
                str(model_config.get("model") or ""),
                model_config.get("max_output_tokens"),
            ),
            timeout=45.0,
            usage_source="hr_soul_refine",
            usage_agent_id=usage_agent_id,
            usage_tenant_id=usage_tenant_id,
        )
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content or not content.strip():
            logger.warning(
                "[HR] Soul refinement LLM returned empty content. Provider=%s model=%s response_keys=%s",
                model_config.get("provider"),
                model_config.get("model"),
                list(response.keys()) if isinstance(response, dict) else type(response),
            )
            return raw
        content = content.strip()
        # Strip markdown fences (```json ... ```)
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if not content:
            logger.warning("[HR] Soul refinement content empty after fence stripping")
            return raw
        refined = json.loads(content)

        # Validate each field — only use refined versions that are substantive
        result = dict(raw)
        if isinstance(refined.get("role_description"), str) and len(refined["role_description"]) > max(
            len(role_description), 20
        ):
            result["role_description"] = _trim_role_description_for_prompt_guard(refined["role_description"])
        if isinstance(refined.get("personality"), str) and len(refined["personality"]) > max(len(personality), 20):
            result["personality"] = refined["personality"]
        if isinstance(refined.get("boundaries"), str) and len(refined["boundaries"]) > max(len(boundaries), 20):
            result["boundaries"] = refined["boundaries"]
        if isinstance(refined.get("primary_users"), list) and refined["primary_users"]:
            result["primary_users"] = [str(u) for u in refined["primary_users"] if str(u).strip()]
        if isinstance(refined.get("core_outputs"), list) and refined["core_outputs"]:
            result["core_outputs"] = [str(o) for o in refined["core_outputs"] if str(o).strip()]
        if isinstance(refined.get("quality_standards"), list) and refined["quality_standards"]:
            result["quality_standards"] = [str(q) for q in refined["quality_standards"] if str(q).strip()]
        if isinstance(refined.get("first_tasks"), list) and refined["first_tasks"]:
            result["first_tasks"] = [str(t) for t in refined["first_tasks"] if str(t).strip()]

        logger.info(
            "[HR] Soul refined by LLM: role %d→%d, personality %d→%d, boundaries %d→%d, quality_standards=%d",
            len(role_description),
            len(result["role_description"]),
            len(personality),
            len(result.get("personality", "")),
            len(boundaries),
            len(result.get("boundaries", "")),
            len(result.get("quality_standards", [])),
        )
        return result
    except Exception as exc:
        logger.warning("[HR] Soul refinement failed (using raw inputs): %s", exc)
        return raw


def _collect_trigger_reasons(triggers: list[dict]) -> str:
    return " ".join(str(trigger.get("reason", "")).strip() for trigger in triggers if trigger.get("reason"))


def _derive_capability_routing(
    *,
    role_description: str,
    primary_users: list[str],
    core_outputs: list[str],
    focus_content: str,
    heartbeat_topics: str,
    welcome_message: str,
    triggers: list[dict],
    requested_skill_names: list[str],
    mcp_server_ids: list[str],
    clawhub_slugs: list[str],
) -> dict:
    del role_description, primary_users, core_outputs, focus_content, heartbeat_topics, welcome_message, triggers
    recommended_skill_names: list[str] = []
    install_now_skill_names = _dedupe_strings(list(requested_skill_names))
    deferred_skill_names: list[str] = []
    builtin_paths = [
        "builtin tools + default skills are available; the Agent chooses which governed capability to use."
    ]
    warnings: list[str] = []
    if mcp_server_ids or clawhub_slugs:
        warnings.append("Requested external capabilities require source, permission, and first-task verification.")

    return {
        "recommended_skill_names": recommended_skill_names,
        "install_now_skill_names": install_now_skill_names,
        "deferred_skill_names": deferred_skill_names,
        "builtin_paths": builtin_paths,
        "warnings": warnings,
    }


def _derive_manual_steps(
    *,
    skill_names: list[str],
    deferred_skill_names: list[str],
    mcp_server_ids: list[str],
    clawhub_slugs: list[str],
    triggers: list[dict],
    role_description: str,
    focus_content: str,
    heartbeat_topics: str,
    welcome_message: str,
) -> list[str]:
    del role_description, focus_content, heartbeat_topics, welcome_message
    steps: list[str] = []
    if "feishu-integration" in skill_names:
        steps.append("完成 Feishu 渠道绑定或 Feishu CLI 认证，验证消息与办公工具是否可用。")
    if mcp_server_ids:
        steps.append("准备并验证所选 MCP server 所需的 API key / OAuth 授权。")
    if clawhub_slugs:
        steps.append("确认 ClawHub 技能来源可信，并在创建后手动验证首个真实任务。")
    if triggers:
        steps.append("在启用自动触发器前，先手动跑一次同类任务，确认输出链路可用。")
    if deferred_skill_names:
        steps.append(
            "对候选扩展能力先做 builtin/default dry run；只有首个真实任务证明存在硬缺口时，才安装这些延后能力："
            + ", ".join(deferred_skill_names)
            + "。换句话说，defer extra installs until a builtin/default dry run proves a real gap."
        )
    return _dedupe_strings(steps)


async def _install_external_skill_from_url(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    url: str,
) -> dict:
    parsed = _parse_github_url(url)
    if not parsed:
        raise ValueError("Invalid GitHub URL")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]
    folder_name = path.rstrip("/").split("/")[-1] if path else repo

    reused_skill = await reuse_existing_skill_for_agent(
        agent_id=agent_id,
        tenant_id=tenant_id,
        folder_name=folder_name,
    )
    if reused_skill is not None:
        return {
            "status": "already_installed",
            "folder_name": folder_name,
            "files_written": reused_skill.get("files_written", 0),
            "source_url": url,
        }

    token = await _get_github_token(str(tenant_id) if tenant_id else None)
    files = await _fetch_github_directory(owner, repo, path, branch, token=token)
    if not files:
        raise ValueError("No files found at the provided GitHub URL")
    review_result = await stage_external_skill_package_review_for_tenant(
        tenant_id=tenant_id,
        created_by_user_id=None,
        source_uri=url,
        folder_name=folder_name,
        files=files,
        source_format="external_skill_url",
    )
    review_result["source_url"] = url
    return review_result


async def _install_external_skill_from_skills_ref(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    ref: str,
) -> dict:
    if not _SKILLS_REF_RE.match(ref):
        raise ValueError("Invalid skills.sh ref")

    # Isolated empty work_dir for the npx process; skill artifacts land under
    # $HOME/.agents/skills, which the execution provider syncs back to exec_home
    # (local: bwrap HOME; vercel: remote HOME tarred back).
    work_dir = Path(tempfile.mkdtemp(prefix=f"hr_skill_work_{agent_id}_"))
    exec_home = Path(tempfile.mkdtemp(prefix=f"hr_skill_ref_{agent_id}_"))
    safe_env = build_agent_subprocess_env(home=exec_home)
    try:
        command = ["bash", "-lc", f"npx skills add {shlex.quote(ref)} -y"]
        # `npx skills add` needs node + npm-registry egress. Run on the node
        # runtime with network allowed; the local provider ignores runtime.
        result = await execute_agent_command(
            command,
            work_dir=work_dir,
            env=safe_env,
            timeout=120,
            runtime="node24",
            network_policy="allow-all",
        )
        if result.error:
            raise RuntimeError(result.error)
        if result.exit_code != 0:
            message = result.stderr or result.stdout
            raise RuntimeError(message or "skills.sh install failed")

        sandbox_skills = exec_home / ".agents" / "skills"
        if not sandbox_skills.exists():
            raise RuntimeError("skills.sh install completed but no skill files were produced")

        from app.services.skill_installation import collect_skill_package_files

        installed: list[dict] = []
        for skill_path in sandbox_skills.iterdir():
            if skill_path.is_dir():
                installed.append(
                    await stage_external_skill_package_review_for_tenant(
                        tenant_id=tenant_id,
                        created_by_user_id=None,
                        source_uri=f"skills_ref:{ref}",
                        folder_name=skill_path.name,
                        files=collect_skill_package_files(skill_path),
                        source_format="skills_ref",
                    )
                )
            elif skill_path.is_file() and skill_path.suffix.lower() == ".md":
                installed.append(
                    await stage_external_skill_package_review_for_tenant(
                        tenant_id=tenant_id,
                        created_by_user_id=None,
                        source_uri=f"skills_ref:{ref}",
                        folder_name=skill_path.stem,
                        files=[
                            {"path": "SKILL.md", "content": skill_path.read_text(encoding="utf-8", errors="replace")}
                        ],
                        source_format="skills_ref",
                    )
                )

        if not installed:
            raise RuntimeError("skills.sh install completed but copied 0 skill files")

        expected_folder = ref.split("@", 1)[1]
        installed_by_name = {item["folder_name"]: item for item in installed}
        primary = installed_by_name.get(expected_folder) or installed[0]

        return {
            "status": primary["status"],
            "folder_name": primary["folder_name"],
            "files_written": sum(int(item.get("files_written") or 0) for item in installed),
            "skill_guard": primary["skill_guard"],
            "review_id": primary.get("review_id"),
            "source_ref": ref,
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(exec_home, ignore_errors=True)


async def _install_external_skill_ref(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    ref: str,
) -> dict:
    if _parse_github_url(ref):
        return await _install_external_skill_from_url(agent_id=agent_id, tenant_id=tenant_id, url=ref)
    if _SKILLS_REF_RE.match(ref):
        return await _install_external_skill_from_skills_ref(agent_id=agent_id, tenant_id=tenant_id, ref=ref)
    raise ValueError("Unsupported external skill reference")


def _build_blueprint_preview_payload(
    arguments: dict,
    *,
    validated_company_source_refs: set[str] | None = None,
) -> dict:
    """Build a structured HR blueprint preview from raw arguments."""
    name = str(arguments.get("name", "")).strip()
    role_description = _trim_role_description_for_prompt_guard(arguments.get("role_description", ""))
    primary_users = _dedupe_strings(
        [item for item in _parse_list(arguments.get("primary_users")) if isinstance(item, str)]
    )
    core_outputs = _dedupe_strings(
        [item for item in _parse_list(arguments.get("core_outputs")) if isinstance(item, str)]
    )
    personality = str(arguments.get("personality", "")).strip()
    boundaries = str(arguments.get("boundaries", "")).strip()
    company_charter = _normalize_charter_payload(
        arguments.get("company_charter"),
        allowed_keys=("goals", "boundaries", "escalation"),
    )
    owner_agency_charter = _normalize_charter_payload(
        arguments.get("owner_agency_charter"),
        allowed_keys=("full_authority", "confirm_first", "never_do"),
    )
    archetype_filled = apply_archetype_defaults(
        {
            "archetype": arguments.get("archetype") or "generalist",
            "role_description": role_description,
            "primary_users": primary_users,
            "core_outputs": core_outputs,
            "company_charter": company_charter,
            "owner_agency_charter": owner_agency_charter,
        }
    )
    company_charter = archetype_filled["company_charter"]
    owner_agency_charter = archetype_filled["owner_agency_charter"]
    archetype = archetype_filled["archetype"]
    raw_requested_skill_names = _dedupe_strings(
        [item for item in _parse_list(arguments.get("skill_names")) if isinstance(item, str)]
    )
    requested_skill_names, derived_external_skill_refs = _split_requested_skill_inputs(raw_requested_skill_names)
    explicit_external_skill_refs = _dedupe_strings(
        _parse_external_skill_urls(arguments.get("external_skill_urls"))
        + _parse_external_skill_urls(arguments.get("external_skill_refs"))
    )
    external_skill_refs = _dedupe_strings(derived_external_skill_refs + explicit_external_skill_refs)
    mcp_server_ids = _dedupe_strings(
        [item for item in _parse_list(arguments.get("mcp_server_ids")) if isinstance(item, str)]
    )
    clawhub_slugs = _dedupe_strings(
        [item for item in _parse_list(arguments.get("clawhub_slugs")) if isinstance(item, str)]
    )
    permission_scope = str(arguments.get("permission_scope", "company") or "company").strip() or "company"
    focus_content = str(arguments.get("focus_content", "")).strip()
    heartbeat_topics = str(arguments.get("heartbeat_topics", "")).strip()
    welcome_message = str(arguments.get("welcome_message", "")).strip()
    raw_triggers = arguments.get("triggers") or []
    if isinstance(raw_triggers, str):
        try:
            parsed = json.loads(raw_triggers)
            raw_triggers = parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            raw_triggers = []
    triggers = [item for item in raw_triggers if isinstance(item, dict)]
    source_attributions, source_attribution_warnings = _parse_source_attributions(
        arguments.get("source_attributions"),
        validated_company_source_refs=validated_company_source_refs,
    )

    capability_routing = _derive_capability_routing(
        role_description=role_description,
        primary_users=primary_users,
        core_outputs=core_outputs,
        focus_content=focus_content,
        heartbeat_topics=heartbeat_topics,
        welcome_message=welcome_message,
        triggers=triggers,
        requested_skill_names=requested_skill_names,
        mcp_server_ids=mcp_server_ids,
        clawhub_slugs=clawhub_slugs,
    )
    recommended_skill_names = capability_routing["recommended_skill_names"]
    install_now_skill_names = capability_routing["install_now_skill_names"]
    deferred_skill_names = capability_routing["deferred_skill_names"]

    will_install: list[str] = []
    will_install.extend(f"extra skill: {skill_name}" for skill_name in install_now_skill_names)
    will_install.extend(f"external skill ref: {ref}" for ref in external_skill_refs)
    will_install.extend(f"mcp: {server_id}" for server_id in mcp_server_ids)
    will_install.extend(f"clawhub skill: {slug}" for slug in clawhub_slugs)

    warnings: list[str] = []
    if not role_description:
        warnings.append("role_description is empty — the created soul contract will be generic.")
    if not primary_users:
        warnings.append("primary_users is empty — the agent may be less clear about who it serves.")
    if not core_outputs:
        warnings.append("core_outputs is empty — the agent may not know what deliverables matter most.")
    if not focus_content:
        warnings.append("focus_content is empty — the new agent will need an initial mission seed after creation.")
    warnings.extend(capability_routing["warnings"])
    warnings.extend(source_attribution_warnings)

    manual_steps = _derive_manual_steps(
        skill_names=install_now_skill_names,
        deferred_skill_names=deferred_skill_names,
        mcp_server_ids=mcp_server_ids,
        clawhub_slugs=clawhub_slugs,
        triggers=triggers,
        role_description=role_description,
        focus_content=focus_content,
        heartbeat_topics=heartbeat_topics,
        welcome_message=welcome_message,
    )
    if external_skill_refs:
        manual_steps.append(
            "验证外部 GitHub/skills.sh skill 的源码、安全性与首个真实任务输出，避免直接信任第三方能力。"
        )

    risk_class = _classify_creation_risk(
        role_description=role_description,
        primary_users=primary_users,
        core_outputs=core_outputs,
        focus_content=focus_content,
        triggers=triggers,
        welcome_message=welcome_message,
        declared_risk_class=str(arguments.get("risk_class") or "standard"),
    )
    gates, missing_gates = _build_creation_flow_gates(
        name=name,
        role_description=role_description,
        primary_users=primary_users,
        core_outputs=core_outputs,
        boundaries=boundaries,
        focus_content=focus_content,
        triggers=triggers,
        risk_class=risk_class,
        manual_steps=manual_steps,
        install_now_skill_names=install_now_skill_names,
        mcp_server_ids=mcp_server_ids,
        clawhub_slugs=clawhub_slugs,
        external_skill_refs=external_skill_refs,
    )
    if gates["governance"]["status"] == "missing":
        warnings.append(
            "governance gate incomplete — high-risk or external-visible roles require explicit boundaries before creation."
        )
    if missing_gates:
        warnings.append("creation gates incomplete: " + ", ".join(missing_gates))

    blueprint = {
        "name": name,
        "role_description": role_description,
        "primary_users": primary_users,
        "core_outputs": core_outputs,
        "personality": personality,
        "boundaries": boundaries,
        "archetype": archetype,
        "company_charter": company_charter,
        "owner_agency_charter": owner_agency_charter,
        "skill_names": install_now_skill_names,
        "requested_skill_names": requested_skill_names,
        "effective_skill_names": install_now_skill_names,
        "deferred_skill_names": deferred_skill_names,
        "external_skill_urls": [ref for ref in external_skill_refs if _parse_github_url(ref)],
        "external_skill_refs": external_skill_refs,
        "mcp_server_ids": mcp_server_ids,
        "clawhub_slugs": clawhub_slugs,
        "permission_scope": permission_scope,
        "triggers": triggers,
        "welcome_message": welcome_message,
        "focus_content": focus_content,
        "heartbeat_topics": heartbeat_topics,
        "source_attributions": source_attributions,
        "ready_now": _default_ready_now(),
        "deferred_capabilities": [
            f"{skill_name} (defer until a first real task proves builtin/default coverage is insufficient)"
            for skill_name in deferred_skill_names
        ],
    }
    blueprint_hash = _blueprint_hash(blueprint)

    return {
        "status": "preview",
        "blueprint_hash": blueprint_hash,
        "risk_class": risk_class,
        "missing_gates": missing_gates,
        "creation_flow": {
            "mode": "dynamic_rounds_mandatory_gates",
            "instruction": (
                "Do not treat this as a fixed five-round interview. Complete the gates dynamically, "
                "then wait for the authenticated user to confirm the persisted blueprint in the UI. "
                "Creation consumes only blueprint_id; the server derives retry identity from that draft."
            ),
            "gates": gates,
        },
        "blueprint": blueprint,
        "summary": {
            "mission": role_description,
            "primary_users": primary_users,
            "core_outputs": core_outputs,
            "first_mission": focus_content,
        },
        "ready_now": _default_ready_now(),
        "will_install": will_install,
        "recommended_skill_names": recommended_skill_names,
        "capability_routing": {
            "builtin_paths": capability_routing["builtin_paths"],
            "requested_skill_names": requested_skill_names,
            "effective_skill_names": install_now_skill_names,
            "deferred_skill_names": deferred_skill_names,
            "external_skill_urls": [ref for ref in external_skill_refs if _parse_github_url(ref)],
            "external_skill_refs": external_skill_refs,
        },
        "manual_steps": manual_steps,
        "source_attribution_policy": _source_attribution_policy(),
        "knowledge_debt": _knowledge_debt_from_source_attributions(source_attributions),
        "confirmation_requirements": _confirmation_requirements(source_attributions),
        "warnings": _dedupe_strings(warnings),
    }


def _normalize_charter_payload(value: object, *, allowed_keys: tuple[str, ...]) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key in allowed_keys:
        items = _dedupe_strings([item for item in _parse_list(value.get(key)) if isinstance(item, str)])
        if items:
            result[key] = items
    return result


def _build_create_employee_result(
    *,
    agent_id: str,
    agent_name: str,
    features: list[str],
    skills_dir: str,
    creation_state: str = "ready",
    warnings: list[str] | None = None,
    manual_steps: list[str] | None = None,
) -> str:
    warnings = warnings or []
    manual_steps = manual_steps or []
    if creation_state in {"provisioning", "provisioning_failed"}:
        message = (
            f"Digital employee '{agent_name}' (ID: {agent_id}) exists, but required provisioning is not complete. "
            "Resume the canonical blueprint after resolving the reported step; do not treat this employee as ready."
        )
    else:
        message = (
            f"Successfully created digital employee '{agent_name}' (ID: {agent_id}). "
            f"Config: {', '.join(features)}. "
            f"{_default_skill_count()} default platform skill capsules are auto-installed. "
            f"Skills directory: {skills_dir}."
        )
    return json.dumps(
        {
            "status": ("success" if creation_state in {"ready", "ready_with_warnings"} else "incomplete"),
            "creation_state": creation_state,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "features": features,
            "skills_dir": skills_dir,
            "warnings": warnings,
            "manual_steps": manual_steps,
            "message": message,
        },
        ensure_ascii=False,
    )


def _append_hr_creation_t0_event(
    *,
    hr_agent_id: uuid.UUID | str,
    created_agent_id: uuid.UUID | str,
    created_agent_name: str,
    session_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    user_id: uuid.UUID | str | None,
    blueprint_hash: str,
    preview_payload: dict[str, Any],
    installed_skill_names: list[str],
    trigger_count: int,
    creation_draft_id: uuid.UUID | str | None = None,
    data_root: Path | str | None = None,
):
    """Append raw HR creation evidence to the HR agent's T0 session ledger."""
    from app.memory.t0.ledger import append_t0_session_event

    blueprint = preview_payload.get("blueprint") if isinstance(preview_payload, dict) else {}
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    manual_steps = preview_payload.get("manual_steps") if isinstance(preview_payload, dict) else []
    source_attributions = blueprint.get("source_attributions") or []
    metadata = {
        "created_agent_id": str(created_agent_id),
        "created_agent_name": str(created_agent_name),
        "blueprint_hash": str(blueprint_hash),
        "preview_session_id": str(session_id),
        "requesting_user_id": str(user_id) if user_id else None,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "permission_scope": str(blueprint.get("permission_scope") or "company"),
        "risk_class": str(preview_payload.get("risk_class") or "standard"),
        "archetype": str(blueprint.get("archetype") or ""),
        "primary_users": [str(item) for item in (blueprint.get("primary_users") or [])],
        "core_outputs": [str(item) for item in (blueprint.get("core_outputs") or [])],
        "first_task_count": len([item for item in (blueprint.get("first_tasks") or []) if str(item).strip()]),
        "trigger_count": int(trigger_count),
        "installed_skill_names": _dedupe_strings(installed_skill_names),
        "manual_setup_debt": [str(item) for item in (manual_steps or [])],
        "source_attributions": source_attributions if isinstance(source_attributions, list) else [],
        "creation_draft_id": str(creation_draft_id) if creation_draft_id else None,
    }
    if creation_draft_id:
        from app.memory.t0.ledger import T0AppendResult, replay_t0_session_events

        for event in replay_t0_session_events(
            agent_id=hr_agent_id,
            session_id=session_id,
            data_root=data_root,
        ):
            if event.event_type == "hr_agent_created" and str(event.metadata.get("creation_draft_id") or "") == str(
                creation_draft_id
            ):
                return T0AppendResult(
                    path=event.path,
                    jsonl_path=event.truth_path or event.path,
                    segment_id=event.segment_id,
                    event_id=event.event_id,
                    sequence=event.sequence,
                )
    return append_t0_session_event(
        agent_id=hr_agent_id,
        session_id=session_id,
        event_type="hr_agent_created",
        role="tool",
        content="Created digital employee from confirmed HR blueprint.",
        message_id=creation_draft_id,
        actor_id=user_id,
        tenant_id=tenant_id,
        source="web",
        metadata=metadata,
        data_root=data_root,
    )


async def _claim_canonical_hr_blueprint(
    request: ToolExecutionRequest,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any], Any]:
    """Bind a retry-safe create call to one authenticated, session-scoped draft."""
    from app.database import tenant_scoped_session
    from app.services.hr_creation_service import (
        HrCreationConflict,
        canonical_hr_blueprint_hash,
        canonical_hr_blueprint_payload_hash,
        claim_hr_creation_draft_record,
        load_hr_creation_draft,
        validate_hr_creation_blueprint,
    )
    from app.services.tenant_resolver import resolve_tenant_for_agent

    raw_blueprint_id = str(request.arguments.get("blueprint_id") or "").strip()
    raw_session_id = str(getattr(request.context, "session_id", None) or "").strip()
    if not raw_blueprint_id:
        raise ValueError("blueprint_id is required")
    if not raw_session_id:
        raise ValueError("HR creation must run inside the preview session")

    try:
        draft_id = uuid.UUID(raw_blueprint_id)
        session_id = uuid.UUID(raw_session_id)
        user_id = uuid.UUID(str(request.context.user_id))
        hr_agent_id = uuid.UUID(str(request.context.agent_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("blueprint, session, user, and HR agent identifiers must be valid UUIDs") from exc

    raw_tenant_id = request.context.tenant_id or await resolve_tenant_for_agent(hr_agent_id)
    if raw_tenant_id is None:
        raise ValueError("could not resolve the requesting tenant")
    tenant_id = uuid.UUID(str(raw_tenant_id))

    expected_authority = request.arguments.get("_runtime_authority")
    expected: dict[str, Any] | None = None
    runtime_task_id: uuid.UUID | None = None
    if expected_authority is not None:
        if not isinstance(expected_authority, dict):
            raise HrCreationConflict("runtime_authority_mismatch", "HR runtime authority must be a typed object.")
        expected = dict(expected_authority)
        try:
            runtime_task_id = uuid.UUID(str(expected.get("runtime_task_id") or ""))
        except (TypeError, ValueError) as exc:
            raise HrCreationConflict(
                "runtime_authority_mismatch",
                "HR runtime authority has no valid RuntimeTask identity.",
            ) from exc
        if str(runtime_task_id) != str(getattr(request.context, "runtime_task_id", None) or ""):
            raise HrCreationConflict(
                "runtime_authority_mismatch",
                "HR runtime request does not match its execution context.",
            )

    async with tenant_scoped_session(tenant_id) as db:
        if runtime_task_id is not None and expected is not None:
            from app.models.runtime_task import RuntimeTask

            runtime_task = await db.get(RuntimeTask, runtime_task_id, with_for_update=True)
            task_metadata = dict(runtime_task.metadata_json or {}) if runtime_task is not None else {}
            if (
                runtime_task is None
                or runtime_task.task_type != "hr_provisioning"
                or runtime_task.status != "running"
                or runtime_task.tenant_id != tenant_id
                or runtime_task.parent_agent_id != hr_agent_id
                or runtime_task.root_user_id != user_id
                or str(runtime_task.parent_session_id or "") != str(session_id)
                or str(runtime_task.root_session_id or "") != str(session_id)
                or runtime_task.config_snapshot_hash != expected.get("config_snapshot_hash")
                or runtime_task.policy_snapshot_hash != expected.get("policy_snapshot_hash")
                or task_metadata.get("draft_id") != str(draft_id)
                or task_metadata.get("blueprint_version") != expected.get("blueprint_version")
                or task_metadata.get("blueprint_hash") != expected.get("blueprint_hash")
                or task_metadata.get("blueprint_payload_hash") != expected.get("blueprint_payload_hash")
            ):
                raise HrCreationConflict(
                    "runtime_authority_mismatch",
                    "HR RuntimeTask authority changed before the canonical blueprint claim.",
                )
        draft = await load_hr_creation_draft(
            db,
            draft_id=draft_id,
            tenant_id=tenant_id,
            hr_agent_id=hr_agent_id,
            requested_by_user_id=user_id,
            session_id=session_id,
            for_update=True,
        )
        if draft.confirmed_by_user_id != user_id or draft.confirmed_at is None:
            raise HrCreationConflict(
                "missing_confirmation_evidence",
                "HR creation requires authenticated confirmation from the requesting user.",
            )
        if runtime_task_id is not None and expected is not None:
            from app.services.hr_provisioning_runtime import _runtime_authority_issues

            authority_issues = _runtime_authority_issues(runtime_task, draft)
            if authority_issues:
                raise HrCreationConflict(
                    "runtime_authority_mismatch",
                    "HR RuntimeTask authority changed before the canonical blueprint claim.",
                )
        canonical_blueprint = dict(draft.blueprint_json or {})
        payload_hash = canonical_hr_blueprint_payload_hash(canonical_blueprint)
        canonical_hash = canonical_hr_blueprint_hash(canonical_blueprint)
        if draft.blueprint_hash != canonical_hash:
            raise HrCreationConflict(
                "blueprint_integrity_mismatch",
                "Canonical blueprint content no longer matches its persisted digest.",
            )
        if expected is not None:
            expected_values = {
                "blueprint_version": int(draft.blueprint_version),
                "blueprint_hash": str(draft.blueprint_hash),
                "blueprint_payload_hash": payload_hash,
            }
            if any(expected.get(key) != value for key, value in expected_values.items()):
                raise HrCreationConflict(
                    "runtime_authority_mismatch",
                    "HR runtime authority changed before the canonical blueprint claim.",
                )
        # Semantic validation must not acquire a lease. A malformed canonical
        # draft remains confirmed and immediately repairable instead of
        # blocking every retry for the lease duration.
        validate_hr_creation_blueprint(canonical_blueprint)
        claim = claim_hr_creation_draft_record(draft, lease_seconds=600)
        await db.commit()

    return draft_id, tenant_id, user_id, session_id, canonical_blueprint, claim


@tool(
    ToolMeta(
        name="create_digital_employee",
        timeout_seconds=900.0,
        risk_class="enterprise_asset_mutation",
        description=(
            "Create a digital employee from a server-side canonical HR blueprint. "
            "Use this only after the authenticated user confirms the exact preview in the UI. "
            "Never restate or regenerate blueprint fields in this call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "blueprint_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "The canonical blueprint_id returned by preview_agent_blueprint.",
                },
            },
            "required": ["blueprint_id"],
        },
        category="hr",
        display_name="Create Digital Employee",
        icon="\U0001f464",
        is_default=False,
        governance="sensitive",
        adapter="request",
    )
)
async def create_digital_employee(request: ToolExecutionRequest) -> str:
    """Delegate to the single run_hr_provisioning lifecycle owner."""
    import sys
    from app.services.hr_provisioning_runner import run_hr_provisioning

    return await run_hr_provisioning(
        request=request,
        support=sys.modules[__name__],
    )


@tool(
    ToolMeta(
        name="preview_agent_blueprint",
        description=(
            "Preview a structured digital-employee blueprint before creation. "
            "Use this after clarifying mission, users, outputs, boundaries, and first objective. "
            "Prefer identity-first, install-later previews; only plan installs when capability gaps are mandatory on day one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "blueprint_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Existing draft ID when revising a preview; omit to create a new draft.",
                },
                "name": {"type": "string", "description": "Proposed agent name."},
                "archetype": {
                    "type": "string",
                    "enum": [item.value for item in Archetype],
                    "description": "Explicit Agent/user-authored archetype. Omit for neutral generalist defaults.",
                },
                "risk_class": {
                    "type": "string",
                    "enum": ["standard", "high"],
                    "description": "Explicit review classification; prose is never keyword-classified.",
                },
                "role_description": {
                    "type": "string",
                    "maxLength": ROLE_DESCRIPTION_MAX_CHARS,
                    "description": "Core responsibilities and mission.",
                },
                "primary_users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Who this agent primarily serves.",
                },
                "core_outputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Main deliverables this agent must produce.",
                },
                "personality": {
                    "type": "string",
                    "maxLength": HR_LONG_TEXT_MAX_CHARS,
                    "description": "Desired operating style, one trait per line if helpful.",
                },
                "boundaries": {
                    "type": "string",
                    "maxLength": HR_LONG_TEXT_MAX_CHARS,
                    "description": "Risk boundaries or red lines, one per line if helpful.",
                },
                "source_attributions": SOURCE_ATTRIBUTIONS_SCHEMA,
                "skill_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra platform skills only if the first objective is blocked without them.",
                },
                "external_skill_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Installable GitHub skill URLs for third-party skills outside the platform registry, only when mandatory.",
                },
                "external_skill_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Third-party installable skill references. Accepts GitHub URLs or skills.sh refs like owner/repo@skill. Use only for real day-one blockers.",
                },
                "mcp_server_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Requested MCP servers only when builtin/default capabilities are insufficient for the first objective.",
                },
                "clawhub_slugs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Requested ClawHub skills only when builtin/default capabilities are insufficient for the first objective.",
                },
                "permission_scope": {
                    "type": "string",
                    "enum": ["company", "self"],
                    "description": "Who should be allowed to use the agent.",
                },
                "triggers": {"type": "array", "items": {"type": "object"}, "description": "Proposed scheduled tasks."},
                "welcome_message": {
                    "type": "string",
                    "maxLength": HR_LONG_TEXT_MAX_CHARS,
                    "description": "Planned greeting.",
                },
                "focus_content": {
                    "type": "string",
                    "maxLength": HR_LONG_TEXT_MAX_CHARS,
                    "description": "Initial work agenda.",
                },
                "heartbeat_topics": {
                    "type": "string",
                    "maxLength": HR_LONG_TEXT_MAX_CHARS,
                    "description": "Exploration topics for heartbeat.",
                },
            },
            "required": ["name"],
        },
        category="hr",
        display_name="Preview Agent Blueprint",
        icon="🧭",
        is_default=False,
        # The domain operation is a preview. Persisting its canonical draft is
        # part of the platform evidence ledger, like transcript persistence;
        # it does not create or mutate an employee/business asset.
        read_only=True,
        parallel_safe=False,
        risk_class="controlled_write",
        idempotency_scope="session",
        governance="safe",
        adapter="request",
    )
)
async def preview_agent_blueprint(request: ToolExecutionRequest) -> str:
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.services.hr_creation_service import HrCreationConflict, hr_creation_draft_payload, upsert_hr_creation_draft
    from app.services.tenant_resolver import resolve_tenant_for_agent
    from app.services.tool_visibility import is_hr_agent

    try:
        hr_agent_id = uuid.UUID(str(request.context.agent_id))
        user_id = uuid.UUID(str(request.context.user_id))
        session_id = uuid.UUID(str(getattr(request.context, "session_id", None) or ""))
        raw_blueprint_id = str(request.arguments.get("blueprint_id") or "").strip()
        blueprint_id = uuid.UUID(raw_blueprint_id) if raw_blueprint_id else None
    except (TypeError, ValueError):
        return json.dumps(
            {
                "status": "error",
                "error": "invalid_context",
                "message": "HR preview requires valid agent, user, and session identifiers.",
            },
            ensure_ascii=False,
        )

    raw_tenant_id = request.context.tenant_id or await resolve_tenant_for_agent(hr_agent_id)
    if raw_tenant_id is None:
        return json.dumps(
            {"status": "error", "error": "tenant_not_found", "message": "Could not resolve the requesting tenant."},
            ensure_ascii=False,
        )
    tenant_id = uuid.UUID(str(raw_tenant_id))

    async with tenant_scoped_session(tenant_id) as db:
        agent_result = await db.execute(select(Agent).where(Agent.id == hr_agent_id))
        hr_agent = agent_result.scalar_one_or_none()
        session_result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.agent_id == hr_agent_id,
                ChatSession.user_id == user_id,
            )
        )
        if not is_hr_agent(hr_agent) or session_result.scalar_one_or_none() is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": "authority_mismatch",
                    "message": "HR preview must be created by System HR inside the requesting user's session.",
                },
                ensure_ascii=False,
            )
        from app.tools.handlers.knowledge import _company_kb_runtime_principal

        validated_company_source_refs: set[str] | None = None
        if any(
            isinstance(item, dict) and str(item.get("source_type") or "").strip() == COMPANY_SOURCE_ATTRIBUTION_TYPE
            for item in _parse_list(request.arguments.get("source_attributions"))
        ):
            try:
                principal = await _company_kb_runtime_principal(db, request)
                validated_company_source_refs = await _verify_company_kb_source_refs(
                    session=db,
                    principal=principal,
                    source_attributions=request.arguments.get("source_attributions"),
                    trace_id=f"hr-blueprint:{session_id}",
                )
            except Exception:  # noqa: BLE001 - fail closed without exposing inaccessible company evidence
                logger.exception("[HR] Company Knowledge source verification failed")
                validated_company_source_refs = set()
        preview_payload = _build_blueprint_preview_payload(
            request.arguments,
            validated_company_source_refs=validated_company_source_refs,
        )
        try:
            draft = await upsert_hr_creation_draft(
                db,
                tenant_id=tenant_id,
                hr_agent_id=hr_agent_id,
                session_id=session_id,
                requested_by_user_id=user_id,
                preview_payload=preview_payload,
                blueprint_id=blueprint_id,
            )
        except HrCreationConflict as exc:
            return json.dumps(
                {"status": "error", "error": exc.code, "message": exc.message},
                ensure_ascii=False,
            )
        await db.commit()
        return json.dumps(hr_creation_draft_payload(draft), ensure_ascii=False)
