"""HR tools — create digital employees through conversational guidance."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path
import asyncio

from app.api.skills import _fetch_github_directory, _get_github_token, _parse_github_url
from app.config import get_settings
from app.services.capability_reuse_service import reuse_existing_skill_for_agent
from app.services.skill_seeder import BUILTIN_SKILLS
from app.tools.decorator import ToolMeta, tool
from app.tools.runtime import ToolExecutionRequest

logger = logging.getLogger(__name__)


def _default_skill_count() -> int:
    return sum(1 for skill in BUILTIN_SKILLS if skill.get("is_default"))


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
    {
        "skill_name": "atlassian-rovo",
        "keywords": ("jira", "confluence", "atlassian", "compass"),
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


_SOUL_REFINE_PROMPT = """\
<role>
You are the identity architect for Hive, a multi-agent collaboration platform.
You craft the foundational identity contract (soul.md) that defines WHO a new
digital employee IS — for their entire lifetime.
</role>

<pipeline_context>
Every agent has a 4-layer memory pyramid that runs automatically:

  T0 (raw logs, 30d) → T2 (episodic learnings) → T3 (semantic memory) → soul.md (identity)

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

**What does NOT belong** (these go to the objective ledger / focus projection, which is volatile):
current tasks, tool configs, trigger schedules, capability lists, dates,
temporary priorities, current events.
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

**first_tasks** — exactly 3 concrete first assignments (go to the objective ledger, not soul).
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
            max_tokens=3000,
            timeout=45.0,
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
            result["role_description"] = refined["role_description"]
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

    agent_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    skill_dir = agent_dir / "skills" / folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for item in files:
        file_path = (skill_dir / item["path"]).resolve()
        if not str(file_path).startswith(str(agent_dir.resolve())):
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(item["content"], encoding="utf-8")
        written.append(item["path"])

    return {
        "status": "installed",
        "folder_name": folder_name,
        "files_written": len(written),
        "source_url": url,
    }


async def _install_external_skill_from_skills_ref(
    *,
    agent_id: uuid.UUID,
    ref: str,
) -> dict:
    if not _SKILLS_REF_RE.match(ref):
        raise ValueError("Invalid skills.sh ref")

    agent_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    work_dir = agent_dir / "workspace"
    work_dir.mkdir(parents=True, exist_ok=True)

    exec_home = Path(tempfile.mkdtemp(prefix=f"hr_skill_ref_{agent_id}_"))
    safe_env = dict(os.environ)
    safe_env["HOME"] = str(exec_home)
    safe_env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-lc",
        f"npx skills add {ref} -y",
        cwd=str(work_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=safe_env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("skills.sh install timed out after 120s")

    if proc.returncode != 0:
        message = stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace")
        raise RuntimeError(message[:300] or "skills.sh install failed")

    sandbox_skills = exec_home / ".agents" / "skills"
    if not sandbox_skills.exists():
        raise RuntimeError("skills.sh install completed but no skill files were produced")

    copied: list[str] = []
    agent_skills = agent_dir / "skills"
    agent_skills.mkdir(parents=True, exist_ok=True)
    for skill_path in sandbox_skills.iterdir():
        dest = agent_skills / skill_path.name
        if skill_path.is_dir():
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(skill_path, dest)
            copied.append(skill_path.name)
        elif skill_path.is_file() and skill_path.suffix.lower() == ".md":
            shutil.copy2(skill_path, dest)
            copied.append(skill_path.name)

    if not copied:
        raise RuntimeError("skills.sh install completed but copied 0 skill files")

    expected_folder = ref.split("@", 1)[1]
    folder_name = (
        expected_folder if expected_folder in copied or (agent_skills / expected_folder).exists() else copied[0]
    )
    shutil.rmtree(exec_home, ignore_errors=True)

    return {
        "status": "installed",
        "folder_name": folder_name,
        "files_written": len(copied),
        "source_ref": ref,
    }


async def _install_external_skill_ref(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    ref: str,
) -> dict:
    if _parse_github_url(ref):
        return await _install_external_skill_from_url(agent_id=agent_id, tenant_id=tenant_id, url=ref)
    if _SKILLS_REF_RE.match(ref):
        return await _install_external_skill_from_skills_ref(agent_id=agent_id, ref=ref)
    raise ValueError("Unsupported external skill reference")


def _build_blueprint_preview_payload(arguments: dict) -> dict:
    """Build a structured HR blueprint preview from raw arguments."""
    name = str(arguments.get("name", "")).strip()
    role_description = str(arguments.get("role_description", "")).strip()
    primary_users = _dedupe_strings(
        [item for item in _parse_list(arguments.get("primary_users")) if isinstance(item, str)]
    )
    core_outputs = _dedupe_strings(
        [item for item in _parse_list(arguments.get("core_outputs")) if isinstance(item, str)]
    )
    personality = str(arguments.get("personality", "")).strip()
    boundaries = str(arguments.get("boundaries", "")).strip()
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

    return {
        "status": "preview",
        "blueprint": {
            "name": name,
            "role_description": role_description,
            "primary_users": primary_users,
            "core_outputs": core_outputs,
            "personality": personality,
            "boundaries": boundaries,
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
            "ready_now": _default_ready_now(),
            "deferred_capabilities": [
                f"{skill_name} (defer until a first real task proves builtin/default coverage is insufficient)"
                for skill_name in deferred_skill_names
            ],
        },
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
        "warnings": _dedupe_strings(warnings),
    }


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
        f"{_default_skill_count()} default skills auto-installed. "
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


@tool(
    ToolMeta(
        name="create_digital_employee",
        description=(
            "Create a new digital employee with the given configuration. "
            "Use this ONLY after confirming the full plan with the user. "
            "Includes heartbeat schedule and custom heartbeat instructions. "
            "Returns the new employee's name and ID on success."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name for the new digital employee (2-100 characters)",
                },
                "role_description": {
                    "type": "string",
                    "description": "What this employee does — mission, core responsibilities, and domain expertise. Write 2-3 sentences minimum, be specific about the role.",
                },
                "personality": {
                    "type": "string",
                    "description": "Operating style as concrete behaviors (e.g. 'Always cite sources when presenting data', 'Proactively flag risks before they escalate'). One per line, 3-5 lines.",
                },
                "primary_users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Who this agent primarily serves (e.g. ['investment team', 'research team']).",
                },
                "core_outputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Main deliverables this agent must produce (e.g. ['daily report', 'weekly report', 'feishu notification']).",
                },
                "boundaries": {
                    "type": "string",
                    "description": "Hard rules and red lines specific to this role's risk profile (e.g. 'Never fabricate financial data', 'Do not share user PII externally'). One per line, 3-5 lines.",
                },
                "skill_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": f"ONLY platform-registered skill folder_names. Available: feishu-integration, dingtalk-integration, atlassian-rovo. {_default_skill_count()} default skills are auto-installed. Only include extra platform skills that are mandatory on day one; otherwise let the agent evolve later. Do NOT put ClawHub or external skills here — use clawhub_slugs instead.",
                },
                "external_skill_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "GitHub skill package URLs for third-party skills. Backward-compatible alias of external_skill_refs.",
                },
                "external_skill_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Third-party installable skill references. Accepts GitHub URLs or skills.sh refs like owner/repo@skill.",
                },
                "mcp_server_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Smithery MCP server IDs to install only when the first mission is blocked without them (e.g. ['LinkupPlatform/linkup-mcp-server']). Found via discover_resources.",
                },
                "clawhub_slugs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ClawHub skill slugs to install only when builtin/default capabilities and platform skills are insufficient on day one (e.g. ['market-research-agent', 'competitor-analyst']). Found via web_search on clawhub.ai.",
                },
                "permission_scope": {
                    "type": "string",
                    "enum": ["company", "self"],
                    "description": "'company' (everyone) or 'self' (creator only). Default: 'company'.",
                },
                "heartbeat_enabled": {
                    "type": "boolean",
                    "description": "Enable heartbeat (self-evolution cycle: observe performance, act on priorities, learn from outcomes). Default: true.",
                },
                "heartbeat_interval_minutes": {
                    "type": "integer",
                    "description": "Heartbeat interval in minutes. Default: 120.",
                },
                "heartbeat_active_hours": {
                    "type": "string",
                    "description": "Heartbeat active hours (e.g. '09:00-18:00'). Default: '09:00-18:00'.",
                },
                "triggers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Trigger name (e.g. 'daily_news_report')"},
                            "type": {"type": "string", "enum": ["cron", "interval"], "description": "cron or interval"},
                            "config": {
                                "type": "object",
                                "description": 'For cron: {"expr": "0 9 * * *"}. For interval: {"minutes": 30}',
                            },
                            "reason": {
                                "type": "string",
                                "description": "What the agent should do when triggered (the instruction)",
                            },
                        },
                        "required": ["name", "type", "config", "reason"],
                    },
                    "description": "Scheduled tasks. Use cron for fixed times, interval for recurring.",
                },
                "welcome_message": {
                    "type": "string",
                    "description": "Greeting shown when someone first chats with this agent. Should introduce the agent's role and capabilities.",
                },
                "focus_content": {
                    "type": "string",
                    "description": "Initial objective/focus projection content — what should the agent work on first? Written as a task list or agenda in markdown.",
                },
                "heartbeat_topics": {
                    "type": "string",
                    "description": "Role-specific exploration topics, written into the initial objective/focus projection. E.g. 'Focus on AI/VC funding news, semiconductor breakthroughs, and founder movements.'",
                },
            },
            "required": ["name"],
        },
        category="hr",
        display_name="Create Digital Employee",
        icon="\U0001f464",
        governance="sensitive",
        adapter="request",
    )
)
async def create_digital_employee(request: ToolExecutionRequest) -> str:
    args = request.arguments
    user_id = request.context.user_id
    tenant_id = request.context.tenant_id

    name = (args.get("name") or "").strip()
    if not name or len(name) < 2:
        return "Error: name is required and must be at least 2 characters."
    if len(name) > 100:
        return "Error: name must be 100 characters or less."

    role_description = args.get("role_description", "")
    personality = args.get("personality", "")
    boundaries = args.get("boundaries", "")

    skill_names = _dedupe_strings([s for s in _parse_list(args.get("skill_names")) if isinstance(s, str)])
    mcp_server_ids = _dedupe_strings([s for s in _parse_list(args.get("mcp_server_ids")) if isinstance(s, str)])
    clawhub_slugs = _dedupe_strings([s for s in _parse_list(args.get("clawhub_slugs")) if isinstance(s, str)])
    permission_scope = args.get("permission_scope", "company")

    # Heartbeat config (self-awareness cycle) — LLM may pass strings for numeric fields
    _hb_raw = args.get("heartbeat_enabled", True)
    # Validate boolean type — LLM may pass string "false" which is truthy in Python
    if isinstance(_hb_raw, str):
        heartbeat_enabled = _hb_raw.lower() not in ("false", "no", "0", "off", "disabled")
    else:
        heartbeat_enabled = bool(_hb_raw) if _hb_raw is not None else True
    try:
        heartbeat_interval = int(args.get("heartbeat_interval_minutes", 120))
    except (ValueError, TypeError):
        heartbeat_interval = 120
    raw_hours = str(args.get("heartbeat_active_hours", "09:00-18:00"))
    # Normalize common LLM responses: "24/7" → "00:00-23:59", strip whitespace
    if "24" in raw_hours and "7" in raw_hours:
        heartbeat_active_hours = "00:00-23:59"
    elif "-" in raw_hours and ":" in raw_hours:
        heartbeat_active_hours = raw_hours.strip()
    else:
        heartbeat_active_hours = "09:00-18:00"
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

    from app.database import async_session
    from app.models.agent import Agent, AgentPermission
    from app.models.participant import Participant
    from app.models.skill import Skill
    from app.models.user import User
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

    try:
        async with async_session() as db:
            # Look up the calling user
            user_result = await db.execute(select(User).where(User.id == user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                return "Error: could not identify the requesting user."

            effective_tenant_id = uuid.UUID(tenant_id) if tenant_id else user.tenant_id

            # Resolve default model for this tenant (TenantSetting → fallback to first enabled)
            primary_model_id = None
            from app.models.llm import LLMModel
            from app.models.tenant_setting import TenantSetting

            ts_r = await db.execute(
                select(TenantSetting.value).where(
                    TenantSetting.tenant_id == effective_tenant_id,
                    TenantSetting.key == "default_model_id",
                )
            )
            ts_val = ts_r.scalar_one_or_none()
            if isinstance(ts_val, dict) and ts_val.get("model_id"):
                primary_model_id = uuid.UUID(ts_val["model_id"])
            if not primary_model_id:
                model_result = await db.execute(
                    select(LLMModel)
                    .where(LLMModel.tenant_id == effective_tenant_id, LLMModel.enabled.is_(True))
                    .order_by(LLMModel.created_at)
                    .limit(1)
                )
                default_model = model_result.scalar_one_or_none()
                if default_model:
                    primary_model_id = default_model.id
            if not primary_model_id:
                return (
                    f"❌ Cannot create agent '{name}': no LLM model configured for this tenant. "
                    "Please add at least one enabled LLM model in Enterprise Settings → LLM Pool."
                )

            # LLM soul refinement — use the HR agent's own model (proven capable,
            # it just ran the entire hiring conversation). Falls back to new agent's
            # default model if HR agent model is unavailable.
            _hr_agent_r = await db.execute(select(Agent).where(Agent.id == request.context.agent_id))
            _hr_agent = _hr_agent_r.scalar_one_or_none()
            _refine_model_id = (_hr_agent.primary_model_id if _hr_agent else None) or primary_model_id
            _model_r = await db.execute(select(LLMModel).where(LLMModel.id == _refine_model_id))
            _llm_obj = _model_r.scalar_one_or_none()
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
                "hr_agent" if _hr_agent and _hr_agent.primary_model_id else "tenant_default",
            )
            _refined = await _refine_soul_inputs(
                name=name,
                role_description=role_description,
                personality=personality,
                boundaries=boundaries,
                primary_users=_parse_list(args.get("primary_users")),
                core_outputs=_parse_list(args.get("core_outputs")),
                model_config=_model_cfg,
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
            await db.flush()

            # Participant identity
            db.add(
                Participant(
                    type="agent",
                    ref_id=agent.id,
                    display_name=agent.name,
                    avatar_url=None,
                )
            )
            await db.flush()

            # Permissions
            if permission_scope == "self":
                db.add(
                    AgentPermission(
                        agent_id=agent.id,
                        scope_type="user",
                        scope_id=user.id,
                        access_level="manage",
                    )
                )
            else:
                db.add(
                    AgentPermission(
                        agent_id=agent.id,
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
            # Override blueprint with LLM-refined values for soul/focus rendering
            _bp = {
                **preview_payload["blueprint"],
                "primary_users": _refined.get("primary_users", preview_payload["blueprint"].get("primary_users", [])),
                "core_outputs": _refined.get("core_outputs", preview_payload["blueprint"].get("core_outputs", [])),
                "quality_standards": _refined.get("quality_standards", []),
                "first_tasks": _refined.get("first_tasks", []),
                "ready_now": list(preview_payload["ready_now"]),
                "deferred_capabilities": list(preview_payload["blueprint"].get("deferred_capabilities", [])),
                "manual_steps": manual_steps,
            }
            await agent_manager.initialize_agent_files(
                db,
                agent,
                personality=personality,
                boundaries=boundaries,
                blueprint=_bp,
            )

            agent_dir = agent_manager._agent_dir(agent.id)

            # P4: HR-confirmed first tasks are active objectives, not merely
            # focus.md projection rows. Keep the objective ledger as source of truth.
            from app.models.objective import AgentObjective
            from app.services.focus_state import normalize_focus_task_id as _normalize_focus_task_id
            from app.services import objective_intake as _objective_intake
            from app.services.objective_wake_reconciler import build_objective_trigger_payload

            _objective_rows = await db.execute(select(AgentObjective).where(AgentObjective.agent_id == agent.id))
            _initial_objectives = list(_objective_rows.scalars().all())
            for _objective in _initial_objectives:
                if _objective.status not in {"completed", "rejected"}:
                    _objective.status = "active"
                _objective.source = "hr_blueprint"
                _meta = dict(_objective.metadata_json or {})
                _meta.update(
                    {
                        "intake_source": "hr_blueprint",
                        "autonomy_class": "confirmed_hr_blueprint",
                        "risk_level": "low",
                        "confidence": 1.0,
                        "requires_approval": False,
                    }
                )
                _objective.metadata_json = _meta

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
                    _trigger_key = _normalize_focus_task_id(_trigger_name)
                    _trigger_objective = await _objective_intake.upsert_objective_candidate(
                        db,
                        agent,
                        _objective_intake.ObjectiveCandidate(
                            agent_id=agent.id,
                            tenant_id=effective_tenant_id,
                            description=_trigger_reason or _trigger_name,
                            source="hr_blueprint",
                            autonomy_class="confirmed_hr_blueprint",
                            risk_level="low",
                            confidence=1.0,
                            evidence={"trigger": trig},
                            objective_key=_trigger_key,
                            success_criteria=_trigger_reason or None,
                            priority=5,
                            wake_policy={"type": trig_type, "config": raw_config},
                        ),
                        commit=False,
                    )
                    raw_config = dict(raw_config)
                    raw_config["trigger_class"] = "objective_task"
                    raw_config["objective_id"] = str(_trigger_objective.id)
                    db.add(
                        AgentTrigger(
                            agent_id=agent.id,
                            name=_trigger_name,
                            type=trig_type,
                            config=raw_config,
                            reason=_trigger_reason,
                            focus_ref=_trigger_key,
                        )
                    )
                await db.flush()

            # Kick-start: create ONE trigger for the first active objective.
            # After completing it, the agent must call complete_objective with evidence;
            # the objective wake reconciler handles follow-up wake policies.
            _first_tasks = _refined.get("first_tasks", [])
            _boot_task = next((str(t).strip() for t in _first_tasks if str(t).strip()), "")
            if not _boot_task:
                _boot_task = str(args.get("focus_content", "")).strip()
            if _boot_task:
                from app.models.trigger import AgentTrigger

                _first_key = "task_1"
                _first_objective = next(
                    (
                        obj
                        for obj in _initial_objectives
                        if _normalize_focus_task_id(str(getattr(obj, "objective_key", ""))) == _first_key
                    ),
                    _initial_objectives[0] if _initial_objectives else None,
                )
                if _first_objective is not None:
                    _boot_payload = build_objective_trigger_payload(_first_objective)
                    _boot_payload["name"] = "focus_boot"
                    _boot_payload["reason"] = (
                        f"Read the objective ledger and focus projection for your full mission. "
                        f"Start with this first task: {_boot_task}\n\n"
                        "When genuinely complete, call complete_objective with concrete evidence."
                    )
                    db.add(
                        AgentTrigger(
                            agent_id=agent.id,
                            name=_boot_payload["name"],
                            type=_boot_payload["type"],
                            config=_boot_payload["config"],
                            reason=_boot_payload["reason"],
                            focus_ref=_boot_payload["focus_ref"],
                        )
                    )
                else:
                    _fire_at = (_dt.now(_tz.utc) + __import__("datetime").timedelta(seconds=30)).isoformat()
                    db.add(
                        AgentTrigger(
                            agent_id=agent.id,
                            name="focus_boot",
                            type="once",
                            config={"at": _fire_at, "trigger_class": "scheduled_job"},
                            reason=(
                                f"Read focus.md for your full mission and task list. Start with this first task: {_boot_task}\n\n"
                                "After completing it, update the objective ledger with evidence."
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

            skills_dir = agent_dir / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)

            for skill in all_skills_to_copy:
                skill_folder = skills_dir / skill.folder_name
                skill_folder.mkdir(parents=True, exist_ok=True)
                for sf in skill.files:
                    file_path = skill_folder / sf.path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(sf.content)

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

            await db.commit()

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
                from pathlib import Path as _Path
                from app.api.skills import CLAWHUB_BASE, _fetch_github_directory, _get_github_token
                from app.config import get_settings as _get_settings

                agent_dir = _Path(_get_settings().AGENT_DATA_DIR) / str(agent.id)
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
                        skill_dir = agent_dir / "skills" / slug
                        skill_dir.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            fp = (skill_dir / f["path"]).resolve()
                            if not str(fp).startswith(str(agent_dir.resolve())):
                                continue
                            fp.parent.mkdir(parents=True, exist_ok=True)
                            fp.write_text(f["content"], encoding="utf-8")
                        clawhub_results.append(f"✅ {slug}")
                        await record_capability_install(
                            agent_id=agent.id,
                            kind="clawhub_skill",
                            source_key=slug,
                            status="installed",
                            installed_via="hr_agent",
                            metadata_json={"phase": "downloaded_to_agent"},
                        )
                        logger.info(f"[HR] Installed ClawHub skill {slug} for agent {agent.id}")
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
                        external_skill_results.append(
                            f"✅ {result['folder_name']}"
                            if result["status"] == "installed"
                            else f"⏭️ {result['folder_name']}: reused"
                        )
                        await record_capability_install(
                            agent_id=agent.id,
                            kind="external_skill_url",
                            source_key=ref,
                            status="installed",
                            installed_via="hr_agent",
                            display_name=result["folder_name"],
                            metadata_json={"phase": "downloaded_to_agent", "files_written": result["files_written"]},
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
            if heartbeat_enabled:
                features.append(f"heartbeat={heartbeat_active_hours} every {heartbeat_interval}min")
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
        return "Error: failed to create the digital employee. Please try again or contact support."


@tool(
    ToolMeta(
        name="preview_agent_blueprint",
        description=(
            "Preview a structured digital-employee blueprint before creation. "
            "Use this after clarifying mission, users, outputs, boundaries, and first mission. "
            "Prefer identity-first, install-later previews; only plan installs when capability gaps are mandatory on day one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Proposed agent name."},
                "role_description": {"type": "string", "description": "Core responsibilities and mission."},
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
                    "description": "Desired operating style, one trait per line if helpful.",
                },
                "boundaries": {
                    "type": "string",
                    "description": "Risk boundaries or red lines, one per line if helpful.",
                },
                "skill_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra platform skills only if the first mission is blocked without them.",
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
                    "description": "Requested MCP servers only when builtin/default capabilities are insufficient for the first mission.",
                },
                "clawhub_slugs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Requested ClawHub skills only when builtin/default capabilities are insufficient for the first mission.",
                },
                "permission_scope": {
                    "type": "string",
                    "enum": ["company", "self"],
                    "description": "Who should be allowed to use the agent.",
                },
                "triggers": {"type": "array", "items": {"type": "object"}, "description": "Proposed scheduled tasks."},
                "welcome_message": {"type": "string", "description": "Planned greeting."},
                "focus_content": {"type": "string", "description": "Initial work agenda."},
                "heartbeat_topics": {"type": "string", "description": "Exploration topics for heartbeat."},
            },
            "required": ["name"],
        },
        category="hr",
        display_name="Preview Agent Blueprint",
        icon="🧭",
        is_default=False,
        read_only=True,
        parallel_safe=True,
        governance="safe",
        adapter="request",
    )
)
async def preview_agent_blueprint(request: ToolExecutionRequest) -> str:
    return json.dumps(_build_blueprint_preview_payload(request.arguments), ensure_ascii=False)
