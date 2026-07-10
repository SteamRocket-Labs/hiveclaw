"""HR tools — create digital employees through conversational guidance."""

from __future__ import annotations

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

from app.services.archetype import apply_archetype_defaults

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

SOURCE_ATTRIBUTION_TYPES = [
    "confirmed_by_user",
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
                    "Whether the value is user-confirmed, historical, general, or unresolved. "
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
        "Source attribution for substantive blueprint content. Company knowledge is not implemented yet; "
        "history is advisory, and all non-current-session suggestions must be shown to the user for confirmation."
    ),
}


def _trim_role_description_for_prompt_guard(value: object) -> str:
    role_description = str(value or "").strip()
    if len(role_description) <= ROLE_DESCRIPTION_MAX_CHARS:
        return role_description
    return role_description[:ROLE_DESCRIPTION_MAX_CHARS].rstrip()


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


_PLATFORM_SKILL_RULES = (
    {
        "skill_name": "feishu-integration",
        "keywords": ("飞书", "lark", "feishu", "飞书通知", "飞书文档", "飞书表格", "base", "wiki"),
    },
    {
        "skill_name": "dingtalk-integration",
        "keywords": ("钉钉", "dingtalk"),
    },
)

_OFFICE_DELIVERABLE_KEYWORDS = (
    "pdf",
    "ppt",
    "pptx",
    "slides",
    "演示文稿",
    "汇报材料",
    "汇报",
    "word",
    "docx",
    "文档",
    "excel",
    "xlsx",
    "表格",
)

_RESEARCH_WORKFLOW_KEYWORDS = (
    "日报",
    "周报",
    "研究",
    "投研",
    "研报",
    "行业动态",
    "融资动态",
    "扫描",
    "monitor",
    "report",
)

_SKILLS_REF_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")

_HIGH_RISK_KEYWORDS = (
    "美股",
    "股票",
    "证券",
    "金融",
    "投资",
    "投研",
    "交易",
    "财报",
    "个股",
    "荐股",
    "喊单",
    "medical",
    "health",
    "legal",
    "finance",
    "stock",
    "trading",
    "investment",
    "securities",
)

_EXTERNAL_VISIBLE_KEYWORDS = (
    "telegram",
    "twitter",
    " x ",
    "推特",
    "飞书群",
    "微信群",
    "社群",
    "社区",
    "发布",
    "推送",
    "对外",
    "public",
    "publish",
    "post",
    "send",
)


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


def _parse_source_attributions(value: object) -> tuple[list[dict[str, Any]], list[str]]:
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
        if not source_type:
            source_type = UNKNOWN_SOURCE_ATTRIBUTION_TYPE
            defaulted_source_type_count += 1
        elif source_type == "supported_by_company_kb":
            source_type = UNKNOWN_SOURCE_ATTRIBUTION_TYPE
            warnings.append(
                "supported_by_company_kb was downgraded to unknown_or_needs_company_source because Company KB is not implemented"
            )
        elif source_type not in SOURCE_ATTRIBUTION_TYPES:
            invalid_count += 1
            continue
        entry: dict[str, Any] = {
            "field": field,
            "source_type": source_type,
            "value_summary": str(item.get("value_summary") or "").strip(),
            "source_refs": _dedupe_strings(
                [ref for ref in _parse_list(item.get("source_refs")) if isinstance(ref, str)]
            ),
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
        "company_knowledge_lane": "known_missing_not_available_for_attribution",
        "history_suggestion_lane": "advisory",
        "general_knowledge_lane": "fallback",
        "confirmation_rule": (
            "All substantive blueprint content from history or general knowledge must be presented to the user "
            "and explicitly confirmed. Company KB claims are unavailable and remain knowledge debt."
        ),
        "source_type_precedence": [
            "confirmed_by_user",
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
        "company_kb_attribution_available": False,
        "must_confirm_before_create": True,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _blueprint_hash(blueprint: dict) -> str:
    digest = hashlib.sha256(_canonical_json(blueprint).encode("utf-8")).hexdigest()[:24]
    return f"bp_{digest}"


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
) -> str:
    blob = _text_blob(role_description, primary_users, core_outputs, focus_content, triggers, welcome_message)
    high_domain = any(keyword.lower() in blob for keyword in _HIGH_RISK_KEYWORDS)
    external_visible = any(keyword.lower() in blob for keyword in _EXTERNAL_VISIBLE_KEYWORDS)
    if high_domain or external_visible:
        return "high"
    return "standard"


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
        from app.services.llm_client import chat_complete

        response = await chat_complete(
            provider=model_config["provider"],
            api_key=model_config["api_key"],
            model=model_config["model"],
            base_url=model_config.get("base_url"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=8192,  # CC-standard auxiliary-call floor
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


def _build_capability_text_blob(
    *,
    role_description: str,
    primary_users: list[str],
    core_outputs: list[str],
    focus_content: str,
    heartbeat_topics: str,
    welcome_message: str,
    triggers: list[dict],
) -> str:
    trigger_names = " ".join(str(trigger.get("name", "")).strip() for trigger in triggers if trigger.get("name"))
    return " ".join(
        [
            role_description,
            " ".join(primary_users),
            " ".join(core_outputs),
            focus_content,
            heartbeat_topics,
            welcome_message,
            _collect_trigger_reasons(triggers),
            trigger_names,
        ]
    ).lower()


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


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
    text_blob = _build_capability_text_blob(
        role_description=role_description,
        primary_users=primary_users,
        core_outputs=core_outputs,
        focus_content=focus_content,
        heartbeat_topics=heartbeat_topics,
        welcome_message=welcome_message,
        triggers=triggers,
    )

    recommended_skill_names: list[str] = []
    for rule in _PLATFORM_SKILL_RULES:
        if _contains_any(text_blob, rule["keywords"]):
            recommended_skill_names.append(rule["skill_name"])

    recommended_skill_names = _dedupe_strings(recommended_skill_names)
    install_now_skill_names = _dedupe_strings(list(requested_skill_names))
    deferred_skill_names = [
        skill_name for skill_name in recommended_skill_names if skill_name not in install_now_skill_names
    ]

    builtin_paths: list[str] = []
    if _contains_any(text_blob, _OFFICE_DELIVERABLE_KEYWORDS):
        builtin_paths.append("default productivity skills already cover PDF/DOCX/XLSX/PPTX document workflows.")
    if _contains_any(text_blob, _RESEARCH_WORKFLOW_KEYWORDS):
        builtin_paths.append(
            "builtin workspace + web research + trigger stack already cover recurring research/report workflows."
        )
    if not builtin_paths:
        builtin_paths.append("builtin tools + default skills already cover the first version of this workflow.")

    warnings: list[str] = []
    if (mcp_server_ids or clawhub_slugs) and _contains_any(text_blob, _OFFICE_DELIVERABLE_KEYWORDS):
        warnings.append(
            "Requested external installs for office deliverables that default productivity skills already cover. "
            "Keep MCP/ClawHub only if a builtin dry run proves insufficient."
        )

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
    text_blob = " ".join(
        [
            role_description.lower(),
            focus_content.lower(),
            heartbeat_topics.lower(),
            welcome_message.lower(),
            _collect_trigger_reasons(triggers).lower(),
        ]
    )
    steps: list[str] = []
    if "feishu-integration" in skill_names or "飞书" in text_blob or "lark" in text_blob:
        steps.append("完成 Feishu 渠道绑定或 Feishu CLI 认证，验证消息与办公工具是否可用。")
    if "email" in text_blob or "邮件" in text_blob:
        steps.append("完成 Email SMTP/IMAP 配置，并先用 Test Connection 验证发送链路。")
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
            raise RuntimeError(message[:300] or "skills.sh install failed")

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


def _build_blueprint_preview_payload(arguments: dict) -> dict:
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
    source_attributions, source_attribution_warnings = _parse_source_attributions(arguments.get("source_attributions"))

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
    message = (
        f"Successfully created digital employee '{agent_name}' (ID: {agent_id}). "
        f"Config: {', '.join(features)}. "
        f"{_default_skill_count()} default platform skill capsules are auto-installed. "
        f"Skills directory: {skills_dir}. "
        "The employee is now being initialized and will be ready shortly."
    )
    return json.dumps(
        {
            "status": "success",
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
    }
    return append_t0_session_event(
        agent_id=hr_agent_id,
        session_id=session_id,
        event_type="hr_agent_created",
        role="tool",
        content="Created digital employee from confirmed HR blueprint.",
        actor_id=user_id,
        tenant_id=tenant_id,
        source="web",
        metadata=metadata,
        data_root=data_root,
    )


async def _claim_canonical_hr_blueprint(
    request: ToolExecutionRequest,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, dict[str, Any]]:
    """Bind a retry-safe create call to one authenticated, session-scoped draft."""
    from app.database import tenant_scoped_session
    from app.services.hr_creation_service import claim_hr_creation_draft_record, load_hr_creation_draft
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

    async with tenant_scoped_session(tenant_id) as db:
        draft = await load_hr_creation_draft(
            db,
            draft_id=draft_id,
            tenant_id=tenant_id,
            hr_agent_id=hr_agent_id,
            requested_by_user_id=user_id,
            session_id=session_id,
            for_update=True,
        )
        claim_hr_creation_draft_record(draft)
        canonical_blueprint = dict(draft.blueprint_json or {})
        await db.commit()

    return draft_id, tenant_id, user_id, session_id, canonical_blueprint


@tool(
    ToolMeta(
        name="create_digital_employee",
        timeout_seconds=120.0,
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
    from app.services.hr_creation_service import HrCreationConflict

    try:
        draft_id, scope_tenant_id, user_id, session_id, args = await _claim_canonical_hr_blueprint(request)
    except (HrCreationConflict, ValueError) as exc:
        code = getattr(exc, "code", "invalid_request")
        message = getattr(exc, "message", str(exc))
        return json.dumps({"status": "error", "error": code, "message": message}, ensure_ascii=False)

    tenant_id = str(scope_tenant_id)

    name = (args.get("name") or "").strip()
    if not name or len(name) < 2:
        return "Error: name is required and must be at least 2 characters."
    if len(name) > 100:
        return "Error: name must be 100 characters or less."

    role_description = _trim_role_description_for_prompt_guard(args.get("role_description", ""))
    personality = args.get("personality", "")
    boundaries = args.get("boundaries", "")

    skill_names = _dedupe_strings([s for s in _parse_list(args.get("skill_names")) if isinstance(s, str)])
    mcp_server_ids = _dedupe_strings([s for s in _parse_list(args.get("mcp_server_ids")) if isinstance(s, str)])
    clawhub_slugs = _dedupe_strings([s for s in _parse_list(args.get("clawhub_slugs")) if isinstance(s, str)])
    permission_scope = args.get("permission_scope", "company")

    from app.services.heartbeat_policy import MANAGED_HEARTBEAT_ACTIVE_HOURS, managed_heartbeat_interval_minutes

    heartbeat_enabled = True
    heartbeat_interval = managed_heartbeat_interval_minutes()
    heartbeat_active_hours = MANAGED_HEARTBEAT_ACTIVE_HOURS
    # Triggers (scheduled tasks) — LLM may pass string or malformed data
    raw_triggers = args.get("triggers") or []
    if isinstance(raw_triggers, str):
        try:
            import json as _json

            raw_triggers = _json.loads(raw_triggers)
        except (ValueError, TypeError) as _trig_err:
            logger.warning("[HR] Failed to parse triggers JSON: %s — raw: %s", _trig_err, str(raw_triggers)[:100])
            raw_triggers = []
    triggers = [t for t in raw_triggers if isinstance(t, dict)]
    if raw_triggers and not triggers:
        logger.warning("[HR] All %d triggers dropped (not dict): %s", len(raw_triggers), str(raw_triggers)[:200])
    # New customization params
    welcome_message = args.get("welcome_message", "")
    preview_payload = _build_blueprint_preview_payload(args)
    skill_names = list(preview_payload["blueprint"]["effective_skill_names"])
    external_skill_refs = list(preview_payload["blueprint"]["external_skill_refs"])
    manual_steps = list(preview_payload["manual_steps"])
    warnings = list(preview_payload["warnings"])
    install_plan = []

    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.agent import Agent, AgentPermission
    from app.models.skill import Skill
    from app.models.user import User
    from app.services.agent_identity_lifecycle import ensure_agent_identity
    from app.services.agent_manager import agent_manager
    from app.services.capability_install_service import (
        build_capability_install_plan,
        record_capability_install,
        record_capability_install_plan,
    )
    from app.services.capability_reuse_service import (
        reuse_existing_mcp_server_for_agent,
        reuse_existing_skill_for_agent,
    )
    from app.services.hr_creation_service import (
        load_hr_creation_draft,
        mark_hr_creation_completed_record,
        mark_hr_creation_failed_record,
    )

    try:
        # RLS 阶段1: agent creation reads/writes many policy-bearing tables
        # (users, agents, skills, llm_models, tenant_settings, tenants). Scope
        # the whole flow to the requesting tenant. The context tenant_id is the
        # primary source; when absent, fall back to the calling HR agent's
        # tenant via the audited single-row bypass (same tenant as the user for
        # self-service creation), so the User read below is still scoped.
        async with tenant_scoped_session(scope_tenant_id) as db:
            # Look up the calling user
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                return "Error: could not identify the requesting user."

            effective_tenant_id = scope_tenant_id
            draft = await load_hr_creation_draft(
                db,
                draft_id=draft_id,
                tenant_id=scope_tenant_id,
                hr_agent_id=uuid.UUID(str(request.context.agent_id)),
                requested_by_user_id=user_id,
                session_id=session_id,
                for_update=True,
            )
            existing_agent_result = await db.execute(select(Agent).where(Agent.id == draft.created_agent_id))
            existing_agent = existing_agent_result.scalar_one_or_none()
            if existing_agent is not None:
                mark_hr_creation_completed_record(
                    draft,
                    agent_id=existing_agent.id,
                    provisioning=dict(draft.provisioning_json or {"core": "completed", "recovered": True}),
                )
                await db.commit()
                return _build_create_employee_result(
                    agent_id=str(existing_agent.id),
                    agent_name=existing_agent.name,
                    features=["idempotent_replay=true"],
                    skills_dir=str(agent_manager._agent_dir(existing_agent.id) / "skills"),
                    creation_state="ready",
                )

            # Resolve default model through the shared tenant-aware model resolver. The
            # tenant setting may point at a deleted/cross-tenant/disabled model; never
            # write that raw UUID into agents.primary_model_id.
            creation_model = await _resolve_employee_creation_model(db, effective_tenant_id)
            if not creation_model:
                mark_hr_creation_failed_record(
                    draft,
                    code="missing_creation_model",
                    message="No enabled LLM model is configured for this tenant.",
                )
                await db.commit()
                return (
                    f"❌ Cannot create agent '{name}': no LLM model configured for this tenant. "
                    "Please add at least one enabled LLM model in Enterprise Settings → LLM Pool."
                )
            primary_model_id = creation_model.id

            # LLM soul refinement — use the HR agent's own model (proven capable,
            # it just ran the entire hiring conversation). Falls back to new agent's
            # default model if HR agent model is unavailable.
            _hr_agent_r = await db.execute(select(Agent).where(Agent.id == request.context.agent_id))
            _hr_agent = _hr_agent_r.scalar_one_or_none()
            _llm_obj, _refine_model_source = await _resolve_employee_refinement_model(
                db,
                effective_tenant_id,
                preferred_model_id=_hr_agent.primary_model_id if _hr_agent else None,
                creation_model=creation_model,
            )
            _model_cfg = (
                {
                    "provider": _llm_obj.provider,
                    "model": _llm_obj.model,
                    "api_key": _llm_obj.api_key,
                    "base_url": _llm_obj.base_url,
                }
                if _llm_obj
                else {}
            )
            logger.info(
                "[HR] Soul refinement using model: %s/%s (from %s)",
                _model_cfg.get("provider", "?"),
                _model_cfg.get("model", "?"),
                _refine_model_source,
            )
            _refined = await _refine_soul_inputs(
                name=name,
                role_description=role_description,
                personality=personality,
                boundaries=boundaries,
                primary_users=_parse_list(args.get("primary_users")),
                core_outputs=_parse_list(args.get("core_outputs")),
                model_config=_model_cfg,
                usage_agent_id=_hr_agent.id if _hr_agent else None,
                usage_tenant_id=tenant_id,
            )
            role_description = _refined["role_description"]
            personality = _refined.get("personality", personality)
            boundaries = _refined.get("boundaries", boundaries)

            install_plan = build_capability_install_plan(
                skill_names=skill_names,
                mcp_server_ids=mcp_server_ids,
                clawhub_slugs=clawhub_slugs,
                external_skill_refs=external_skill_refs,
            )

            resolved_extra_skills: list[Skill] = []
            if skill_names:
                from sqlalchemy import or_
                from sqlalchemy.orm import selectinload

                missing_skill_names: list[str] = []
                for sname in skill_names:
                    sr = await db.execute(
                        select(Skill)
                        .where(
                            Skill.folder_name == sname,
                            or_(
                                Skill.tenant_id == effective_tenant_id,
                                Skill.tenant_id.is_(None),
                            ),
                        )
                        .options(selectinload(Skill.files))
                    )
                    skill = sr.scalar_one_or_none()
                    if skill is None:
                        missing_skill_names.append(sname)
                    else:
                        resolved_extra_skills.append(skill)
                if missing_skill_names:
                    logger.warning("[HR] Skipping unavailable extra skills: %s", missing_skill_names)
                    warnings.append(
                        f"Skipped {len(missing_skill_names)} unavailable skill(s): "
                        + ", ".join(missing_skill_names)
                        + ". Use clawhub_slugs or external_skill_refs for marketplace skills."
                    )

            # Resolve tenant defaults
            default_max_triggers = 20
            default_min_poll = 5
            default_webhook_rate = 5
            tenant_obj = None
            if effective_tenant_id:
                from app.models.tenant import Tenant

                tenant_result = await db.execute(select(Tenant).where(Tenant.id == effective_tenant_id))
                tenant_obj = tenant_result.scalar_one_or_none()
                if tenant_obj:
                    default_max_triggers = tenant_obj.default_max_triggers or 20
                    default_min_poll = tenant_obj.min_poll_interval_floor or 5
                    default_webhook_rate = tenant_obj.max_webhook_rate_ceiling or 5

            # Create the agent — set last_heartbeat_at to now so the first
            # heartbeat fires after a full interval, giving MCP/workspace init time.
            from datetime import datetime as _dt, timezone as _tz

            agent = Agent(
                name=name,
                role_description=role_description,
                welcome_message=welcome_message or None,
                creator_id=user.id,
                owner_user_id=user.id,
                sponsor_user_id=user.id,
                tenant_id=effective_tenant_id,
                agent_type="native",
                agent_class="internal_tenant",
                security_zone="standard",
                primary_model_id=primary_model_id,
                status="creating",
                max_triggers=default_max_triggers,
                min_poll_interval_min=default_min_poll,
                webhook_rate_limit=default_webhook_rate,
                heartbeat_enabled=heartbeat_enabled,
                heartbeat_interval_minutes=heartbeat_interval,
                heartbeat_active_hours=heartbeat_active_hours,
                last_heartbeat_at=_dt.now(_tz.utc),
            )
            db.add(agent)
            await ensure_agent_identity(
                db,
                agent,
                display_name=agent.name,
                avatar_url=None,
                rls_bypass_reason=f"HR digital employee identity bootstrap for tenant {effective_tenant_id}",
                rls_bypass_actor_id=str(user.id),
            )

            # Permissions
            if permission_scope == "self":
                db.add(
                    AgentPermission(
                        agent_id=agent.id,
                        tenant_id=agent.tenant_id,
                        scope_type="user",
                        scope_id=user.id,
                        access_level="manage",
                    )
                )
            else:
                db.add(
                    AgentPermission(
                        agent_id=agent.id,
                        tenant_id=agent.tenant_id,
                        scope_type="company",
                        access_level="use",
                    )
                )
            await db.flush()

            # Assign default platform tools
            from app.services.tool_seeder import assign_default_tools_to_agent

            await assign_default_tools_to_agent(db, agent.id)
            await db.flush()

            # Initialize agent file system (standard template)
            # Override blueprint with LLM-refined values for soul rendering and first-work setup.
            _bp = {
                **preview_payload["blueprint"],
                "primary_users": _refined.get("primary_users", preview_payload["blueprint"].get("primary_users", [])),
                "core_outputs": _refined.get("core_outputs", preview_payload["blueprint"].get("core_outputs", [])),
                "quality_standards": _refined.get("quality_standards", []),
                "first_tasks": _refined.get("first_tasks", []),
                "ready_now": list(preview_payload["ready_now"]),
                "deferred_capabilities": list(preview_payload["blueprint"].get("deferred_capabilities", [])),
                "manual_steps": manual_steps,
                "company_id": str(effective_tenant_id) if effective_tenant_id else "",
                "company_name": getattr(tenant_obj, "name", None) or "the company",
                "owner_id": str(user.id),
                "owner_name": user.display_name or user.username or str(user.id),
                "company_charter": preview_payload["blueprint"].get("company_charter", {}),
                "owner_agency_charter": preview_payload["blueprint"].get("owner_agency_charter", {}),
            }
            await agent_manager.initialize_agent_files(
                db,
                agent,
                personality=personality,
                boundaries=boundaries,
                blueprint=_bp,
            )

            agent_dir = agent_manager._agent_dir(agent.id)

            # Create triggers (scheduled tasks)
            if triggers:
                from app.models.trigger import AgentTrigger

                for trig in triggers:
                    raw_config = trig.get("config", {})
                    trig_type = trig.get("type", "cron")
                    # LLM may pass config as cron string instead of {"expr": "..."}
                    if isinstance(raw_config, str):
                        raw_config = {"expr": raw_config}
                    elif isinstance(raw_config, dict) and "expr" not in raw_config and "minutes" not in raw_config:
                        # Try to find cron-like value in the dict
                        for v in raw_config.values():
                            if isinstance(v, str) and v.count(" ") >= 3:
                                raw_config = {"expr": v}
                                break
                    # Infer cron expr from trigger name if LLM omitted it
                    if trig_type == "cron" and not raw_config.get("expr"):
                        trig_name = (trig.get("name") or "").lower()
                        inferred = None
                        if "every_2h" in trig_name or "2h" in trig_name:
                            inferred = "0 */2 * * *"
                        elif "every_4h" in trig_name or "4h" in trig_name:
                            inferred = "0 */4 * * *"
                        elif "hourly" in trig_name or "every_hour" in trig_name:
                            inferred = "0 * * * *"
                        elif "weekly" in trig_name:
                            inferred = "0 9 * * 1"
                        elif "daily" in trig_name:
                            inferred = "0 9 * * *"
                        if inferred:
                            raw_config = {"expr": inferred}
                            logger.info("Inferred cron expr '%s' for trigger '%s' from name", inferred, trig_name)
                        else:
                            logger.warning(
                                "Skipping cron trigger '%s' — no expr in config and cannot infer", trig.get("name")
                            )
                            continue
                    _trigger_name = str(trig.get("name", "task") or "task").strip()
                    _trigger_reason = str(trig.get("reason", "") or "").strip()
                    raw_config = dict(raw_config)
                    raw_config.setdefault(
                        "trigger_class",
                        "event_wait" if trig_type in {"poll", "on_message", "webhook"} else "scheduled_job",
                    )
                    raw_config = _stamp_hr_blueprint_trigger_exemption(raw_config)
                    db.add(
                        AgentTrigger(
                            agent_id=agent.id,
                            tenant_id=agent.tenant_id,
                            name=_trigger_name,
                            type=trig_type,
                            config=raw_config,
                            reason=_trigger_reason,
                        )
                    )
                await db.flush()

            # Kick-start: create ONE 'once' boot trigger so the new agent wakes
            # shortly after creation and starts its first task.
            _first_tasks = _refined.get("first_tasks", [])
            _boot_task = next((str(t).strip() for t in _first_tasks if str(t).strip()), "")
            if not _boot_task:
                _boot_task = str(args.get("focus_content", "")).strip()
            if _boot_task:
                from app.models.trigger import AgentTrigger

                _fire_at = (_dt.now(_tz.utc) + __import__("datetime").timedelta(seconds=30)).isoformat()
                db.add(
                    AgentTrigger(
                        agent_id=agent.id,
                        tenant_id=agent.tenant_id,
                        name="first_task_boot",
                        type="once",
                        config=_stamp_hr_blueprint_trigger_exemption(
                            {"at": _fire_at, "trigger_class": "scheduled_job"}
                        ),
                        reason=(
                            f"Read soul.md for your full mission. Start with this first task: {_boot_task}\n\n"
                            "Record progress and evidence in your work ledger as you go."
                        ),
                    )
                )
                await db.flush()
                logger.info("[HR] Created boot trigger for agent %s: %s", agent.id, _boot_task[:80])

            # Copy default skills + requested skills
            from sqlalchemy.orm import selectinload

            default_skill_result = await db.execute(
                select(Skill).where(Skill.is_default).options(selectinload(Skill.files))
            )
            all_skills_to_copy: list[Skill] = list(default_skill_result.scalars().all())

            for skill in resolved_extra_skills:
                if skill not in all_skills_to_copy:
                    all_skills_to_copy.append(skill)

            from app.services.skill_installation import install_active_skill_package

            for skill in all_skills_to_copy:
                install_active_skill_package(
                    workspace=agent_dir,
                    folder_name=skill.folder_name,
                    files=[{"path": sf.path, "content": sf.content} for sf in skill.files],
                    source=f"hr_registry_skill:{skill.id}",
                    overwrite=True,
                )

            # Start container
            try:
                await agent_manager.start_container(db, agent)
            except Exception as _container_exc:
                logger.warning("[HR] Container start failed (non-fatal): %s", _container_exc)
            await db.flush()

            # Transition from "creating" → "idle" so heartbeat can pick up this agent
            if agent.status == "creating":
                agent.status = "idle"
                await db.flush()

            # Audit
            try:
                from app.core.policy import write_audit_event

                await write_audit_event(
                    db,
                    event_type="agent.created",
                    severity="info",
                    actor_type="user",
                    actor_id=user.id,
                    tenant_id=effective_tenant_id or user.tenant_id or uuid.UUID(int=0),
                    action="create_agent",
                    resource_type="agent",
                    resource_id=agent.id,
                    details={"name": agent.name, "created_via": "hr_agent"},
                )
            except Exception as _audit_exc:
                logger.warning("Audit write failed for hr agent.created: %s", _audit_exc)

            from app.services.ai_assets import register_agent_asset

            await register_agent_asset(
                db,
                agent,
                change_source="create",
                actor_user_id=user.id,
                change_message="Digital employee created by HR Agent",
            )
            draft.status = "provisioning"
            draft.created_agent_id = agent.id
            draft.provisioning_json = {
                "core": "completed",
                "workspace": "completed",
                "default_skills": "completed",
                "optional_capabilities": "running",
                "t0_evidence": "pending",
            }
            await db.commit()

            session_id = getattr(request.context, "session_id", None)
            if session_id:
                try:
                    _append_hr_creation_t0_event(
                        hr_agent_id=request.context.agent_id,
                        created_agent_id=agent.id,
                        created_agent_name=agent.name,
                        session_id=session_id,
                        tenant_id=effective_tenant_id or user.tenant_id,
                        user_id=user.id,
                        blueprint_hash=preview_payload["blueprint_hash"],
                        preview_payload=preview_payload,
                        installed_skill_names=skill_names,
                        trigger_count=len(triggers),
                    )
                except Exception as t0_exc:
                    warnings.append(
                        "HR creation evidence projection failed; the agent exists and the draft remains auditable."
                    )
                    draft.provisioning_json = {**dict(draft.provisioning_json or {}), "t0_evidence": "failed"}
                    logger.warning(
                        "[HR] Failed to append hr_agent_created T0 event for agent %s: %s",
                        agent.id,
                        t0_exc,
                    )
                else:
                    draft.provisioning_json = {**dict(draft.provisioning_json or {}), "t0_evidence": "completed"}

            if install_plan:
                try:
                    await record_capability_install_plan(
                        agent_id=agent.id,
                        plan=install_plan,
                        installed_via="hr_agent",
                    )
                except Exception as install_plan_err:
                    logger.warning("[HR] Failed to persist capability install plan: %s", install_plan_err)

            for skill in resolved_extra_skills:
                try:
                    await record_capability_install(
                        agent_id=agent.id,
                        kind="platform_skill",
                        source_key=skill.folder_name,
                        status="installed",
                        installed_via="hr_agent",
                        display_name=skill.name,
                        metadata_json={"phase": "copied_to_agent"},
                    )
                except Exception as skill_record_err:
                    logger.warning("[HR] Failed to record installed skill %s: %s", skill.folder_name, skill_record_err)

            # Install MCP servers (after commit, so agent exists in DB)
            logger.info(f"[HR] Post-commit install phase: mcp={mcp_server_ids}, clawhub={clawhub_slugs}")
            mcp_results = []
            if mcp_server_ids:
                from app.services.resource_discovery import import_mcp_from_smithery, _get_smithery_api_key

                # Pre-fetch API key from global config (not from the new agent which has empty config)
                _smithery_key = await _get_smithery_api_key(None)
                for server_id in mcp_server_ids:
                    try:
                        reused = await reuse_existing_mcp_server_for_agent(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            server_id=server_id,
                            config={"smithery_api_key": _smithery_key} if _smithery_key else None,
                        )
                        if reused is not None:
                            mcp_results.append(f"⏭️ {server_id}: reused existing tenant MCP tools")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="installed",
                                installed_via="hr_agent",
                                metadata_json={
                                    "phase": "reused_existing_tenant_tools",
                                    "tool_count": reused["tool_count"],
                                },
                            )
                            logger.info(f"[HR] Reused existing MCP {server_id} for agent {agent.id}")
                            continue
                        _mcp_config = {"smithery_api_key": _smithery_key} if _smithery_key else None
                        result = await import_mcp_from_smithery(server_id, agent.id, config=_mcp_config)
                        if isinstance(result, str) and "❌" in result:
                            mcp_results.append(f"⚠️ {server_id}: {result[:100]}")
                            warnings.append(f"MCP install not ready: {server_id}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="install_rejected",
                                error_message=result[:300],
                            )
                            logger.warning(f"[HR] MCP install rejected for {server_id}: {result[:100]}")
                        elif isinstance(result, dict) and result.get("error"):
                            mcp_results.append(f"⚠️ {server_id}: {result['error'][:100]}")
                            warnings.append(f"MCP install not ready: {server_id}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="install_error",
                                error_message=str(result["error"])[:300],
                            )
                            logger.warning(f"[HR] MCP install error for {server_id}: {result['error'][:100]}")
                        else:
                            mcp_results.append(f"✅ {server_id}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="installed",
                                installed_via="hr_agent",
                                metadata_json={"phase": "post_commit"},
                            )
                            logger.info(f"[HR] Installed MCP {server_id} for agent {agent.id}")
                    except Exception as mcp_err:
                        mcp_results.append(f"⚠️ {server_id}: {mcp_err}")
                        warnings.append(f"MCP install failed: {server_id}")
                        try:
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="mcp_server",
                                source_key=server_id,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="exception",
                                error_message=str(mcp_err)[:300],
                            )
                        except Exception as record_err:
                            logger.warning(
                                "[HR] Failed to record MCP install failure for %s: %s", server_id, record_err
                            )
                        logger.warning(f"[HR] MCP install failed for {server_id}: {mcp_err}")

            # Install ClawHub skills (after commit, so agent exists on disk)
            logger.info(f"[HR] ClawHub install phase: {len(clawhub_slugs)} slugs to install: {clawhub_slugs}")
            clawhub_results = []
            if clawhub_slugs:
                import httpx
                from app.api.skills import CLAWHUB_BASE, _fetch_github_directory, _get_github_token

                ch_tenant = str(effective_tenant_id) if effective_tenant_id else None
                ch_token = await _get_github_token(ch_tenant)
                for slug in clawhub_slugs:
                    try:
                        reused_skill = await reuse_existing_skill_for_agent(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            folder_name=slug,
                        )
                        if reused_skill is not None:
                            clawhub_results.append(f"⏭️ {slug}: reused existing platform skill")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="clawhub_skill",
                                source_key=slug,
                                status="installed",
                                installed_via="hr_agent",
                                metadata_json={"phase": "reused_existing_registry_skill"},
                            )
                            logger.info(f"[HR] Reused existing skill {slug} for agent {agent.id}")
                            continue
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.get(f"{CLAWHUB_BASE}/v1/skills/{slug}")
                            if resp.status_code == 429:
                                import asyncio as _asyncio

                                await _asyncio.sleep(2)
                                resp = await client.get(f"{CLAWHUB_BASE}/v1/skills/{slug}")
                            if resp.status_code != 200:
                                clawhub_results.append(f"⚠️ {slug}: ClawHub HTTP {resp.status_code}")
                                warnings.append(f"ClawHub install not ready: {slug}")
                                await record_capability_install(
                                    agent_id=agent.id,
                                    kind="clawhub_skill",
                                    source_key=slug,
                                    status="failed",
                                    installed_via="hr_agent",
                                    error_code=f"http_{resp.status_code}",
                                    error_message=f"ClawHub HTTP {resp.status_code}",
                                )
                                logger.warning(f"[HR] ClawHub API returned {resp.status_code} for {slug}")
                                continue
                            try:
                                meta = resp.json()
                            except Exception as _json_err:
                                logger.warning("[HR] ClawHub JSON parse failed for %s: %s", slug, _json_err)
                                clawhub_results.append(f"⚠️ {slug}: invalid ClawHub response")
                                warnings.append(f"ClawHub install not ready: {slug}")
                                await record_capability_install(
                                    agent_id=agent.id,
                                    kind="clawhub_skill",
                                    source_key=slug,
                                    status="failed",
                                    installed_via="hr_agent",
                                    error_code="invalid_response",
                                    error_message=str(_json_err)[:300],
                                )
                                continue
                        handle = meta.get("owner", {}).get("handle", "").lower()
                        if not handle:
                            clawhub_results.append(f"⚠️ {slug}: no owner handle")
                            warnings.append(f"ClawHub install not ready: {slug}")
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="clawhub_skill",
                                source_key=slug,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="missing_owner_handle",
                                error_message="ClawHub metadata missing owner handle",
                            )
                            continue
                        github_path = f"skills/{handle}/{slug}"
                        files = await _fetch_github_directory("openclaw", "skills", github_path, "main", ch_token)
                        review_result = await stage_external_skill_package_review_for_tenant(
                            tenant_id=effective_tenant_id,
                            created_by_user_id=None,
                            source_uri=f"clawhub:{slug}",
                            folder_name=slug,
                            files=files,
                            source_format="clawhub_skill",
                        )
                        clawhub_results.append(f"{slug}: {review_result['status']}")
                        await record_capability_install(
                            agent_id=agent.id,
                            kind="clawhub_skill",
                            source_key=slug,
                            status=review_result["status"],
                            installed_via="hr_agent",
                            metadata_json={
                                "phase": "trust_gate_review",
                                "review_id": review_result.get("review_id"),
                                "skill_guard": review_result.get("skill_guard"),
                                "files_written": 0,
                            },
                        )
                        logger.info("[HR] Staged ClawHub skill %s for Trust Gate review on agent %s", slug, agent.id)
                    except Exception as ch_err:
                        clawhub_results.append(f"⚠️ {slug}: {ch_err}")
                        warnings.append(f"ClawHub install failed: {slug}")
                        try:
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="clawhub_skill",
                                source_key=slug,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="exception",
                                error_message=str(ch_err)[:300],
                            )
                        except Exception as record_err:
                            logger.warning("[HR] Failed to record ClawHub install failure for %s: %s", slug, record_err)
                        logger.warning(f"[HR] ClawHub install failed for {slug}: {ch_err}")

            external_skill_results = []
            if external_skill_refs:
                for ref in external_skill_refs:
                    try:
                        result = await _install_external_skill_ref(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            ref=ref,
                        )
                        external_skill_results.append(f"{result['folder_name']}: {result['status']}")
                        await record_capability_install(
                            agent_id=agent.id,
                            kind="external_skill_url",
                            source_key=ref,
                            status=result["status"],
                            installed_via="hr_agent",
                            display_name=result["folder_name"],
                            metadata_json={
                                "phase": "trust_gate_review",
                                "files_written": result["files_written"],
                                "review_id": result.get("review_id"),
                            },
                        )
                    except Exception as ext_err:
                        external_skill_results.append(f"⚠️ {ref}: {ext_err}")
                        warnings.append(f"External skill install failed: {ref}")
                        try:
                            await record_capability_install(
                                agent_id=agent.id,
                                kind="external_skill_url",
                                source_key=ref,
                                status="failed",
                                installed_via="hr_agent",
                                error_code="exception",
                                error_message=str(ext_err)[:300],
                            )
                        except Exception as record_err:
                            logger.warning("[HR] Failed to record external skill failure for %s: %s", ref, record_err)

            # Build response
            features = [f"name='{agent.name}'"]
            if triggers:
                trigger_names = [t.get("name", "?") for t in triggers]
                features.append(f"triggers={trigger_names}")
            if skill_names:
                features.append(f"extra_skills={skill_names}")
            if mcp_results:
                features.append(f"mcp={mcp_results}")
            if clawhub_results:
                features.append(f"clawhub={clawhub_results}")
            if external_skill_results:
                features.append(f"external_skills={external_skill_results}")

            mark_hr_creation_completed_record(
                draft,
                agent_id=agent.id,
                provisioning={
                    **dict(draft.provisioning_json or {}),
                    "optional_capabilities": "completed_with_warnings" if warnings else "completed",
                },
            )
            await db.commit()
            return _build_create_employee_result(
                agent_id=str(agent.id),
                agent_name=agent.name,
                features=features,
                skills_dir=str(agent_dir / "skills"),
                creation_state="ready_with_warnings" if warnings or manual_steps else "ready",
                warnings=_dedupe_strings(warnings),
                manual_steps=manual_steps,
            )

    except Exception as e:
        logger.error(f"[HR] create_digital_employee failed: {e}", exc_info=True)
        try:
            async with tenant_scoped_session(scope_tenant_id) as failure_db:
                failed_draft = await load_hr_creation_draft(
                    failure_db,
                    draft_id=draft_id,
                    tenant_id=scope_tenant_id,
                    hr_agent_id=uuid.UUID(str(request.context.agent_id)),
                    requested_by_user_id=user_id,
                    session_id=session_id,
                    for_update=True,
                )
                if failed_draft.status != "completed":
                    mark_hr_creation_failed_record(
                        failed_draft,
                        code="provisioning_failed",
                        message=str(e),
                    )
                    await failure_db.commit()
        except Exception as state_exc:
            logger.error("[HR] Failed to persist HR creation failure state: %s", state_exc, exc_info=True)
        return "Error: failed to create the digital employee. Please try again or contact support."


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

    preview_payload = _build_blueprint_preview_payload(request.arguments)
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
