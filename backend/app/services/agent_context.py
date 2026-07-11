"""Build rich system prompt context for agents.

Loads soul, layered memory context, skills summary, and live A2A collaborators
and composes a comprehensive system prompt.
"""

import re
import uuid
from pathlib import Path

from loguru import logger

from app.config import get_settings
from app.runtime.context_budget import ContextBudget
from app.services.agent_team_context import build_prompt_facing_team_context
from app.services.skill_catalog_ranker import rank_skills_for_prompt_with_reasons
from app.skills import SkillRegistry, WorkspaceSkillLoader

settings = get_settings()

# Two workspace roots exist — tool workspace and persistent data
TOOL_WORKSPACE = Path("/tmp/hive_workspaces")
PERSISTENT_DATA = Path(settings.AGENT_DATA_DIR)
_PROMPT_INJECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"ignore\s+(previous|all|above|prior)\s+instructions", "prompt_injection"),
    (r"do\s+not\s+tell\s+the\s+user", "deception_hide"),
    (r"system\s+prompt\s+override", "sys_prompt_override"),
    (r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", "disregard_rules"),
    (r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)", "bypass_restrictions"),
)


def _read_file_safe(path: Path, max_chars: int = 3000) -> str:
    """Read a file, return empty string if missing. Truncate if too long."""
    if not path.exists():
        return ""
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...(truncated)"
        return content
    except Exception:
        return ""


def _sanitize_prompt_context(content: str, *, source_name: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""

    findings: list[str] = []
    for pattern, label in _PROMPT_INJECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            findings.append(label)

    if findings:
        logger.warning("Blocked prompt context from {} due to {}", source_name, ", ".join(findings))
        return f"[BLOCKED: {source_name} contained potential prompt injection ({', '.join(findings)}). Content not loaded.]"

    return text


def _parse_skill_frontmatter(content: str, filename: str) -> tuple[str, str]:
    """Parse YAML frontmatter from a skill .md file.

    Returns (name, description).
    If no frontmatter, falls back to filename-based name and first-line description.
    """
    name = filename.replace("_", " ").replace("-", " ")
    description = ""

    stripped = content.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            frontmatter = stripped[3:end].strip()
            for line in frontmatter.split("\n"):
                line = line.strip()
                if line.lower().startswith("name:"):
                    val = line[5:].strip().strip('"').strip("'")
                    if val:
                        name = val
                elif line.lower().startswith("description:"):
                    val = line[12:].strip().strip('"').strip("'")
                    if val:
                        description = val[:200]
            if description:
                return name, description

    # Fallback: use first non-empty, non-heading line as description
    for line in stripped.split("\n"):
        line = line.strip()
        # Skip frontmatter delimiters and YAML lines
        if line in ("---",) or line.startswith("name:") or line.startswith("description:"):
            continue
        if line and not line.startswith("#"):
            description = line[:200]
            break
    if not description:
        lines = stripped.split("\n")
        if lines:
            description = lines[0].strip().lstrip("# ")[:200]

    return name, description


def _strip_primary_heading(content: str) -> str:
    if content.startswith("# "):
        return "\n".join(content.split("\n")[1:]).strip()
    return content


def _load_skills_index(
    agent_id: uuid.UUID,
    *,
    budget_chars: int = 8000,
    scenario_text: str | None = None,
    session_id: object | None = None,
    active_skill_names: tuple[str, ...] = (),
    path_triggered_skill_names: tuple[str, ...] = (),
    ranking_manifest: list[dict] | None = None,
) -> str:
    """Load skill index (name + description) from skills/ directory.

    Supports the canonical folder format plus legacy flat-file compatibility:
    - Canonical:   skills/my-skill/SKILL.md  (with optional scripts/, references/)
    - Legacy read-only fallback: skills/my-skill.md

    Uses progressive disclosure: only name+description go into the system
    prompt. The model is instructed to call read_file to load full content
    when a skill is relevant.
    """
    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    loaded_skills = []

    for ws_root in [TOOL_WORKSPACE / str(agent_id), PERSISTENT_DATA / str(agent_id)]:
        loaded_skills.extend(loader.load_from_workspace(ws_root, session_id=session_id))

    rank_workspace = PERSISTENT_DATA / str(agent_id)
    if not rank_workspace.exists():
        rank_workspace = TOOL_WORKSPACE / str(agent_id)
    ranked = rank_skills_for_prompt_with_reasons(
        rank_workspace,
        loaded_skills,
        scenario_text=scenario_text,
        active_skill_names=active_skill_names,
        path_triggered_skill_names=path_triggered_skill_names,
    )
    registry.register_many([decision.skill for decision in ranked])
    if ranking_manifest is not None:
        ranking_manifest[:] = [
            {
                "rank": index,
                "skill_name": decision.skill.metadata.name,
                "source": decision.skill.relative_path,
                "score": decision.score,
                "reasons": list(decision.reasons),
                "state": decision.state,
                "use_count": decision.use_count,
                "activation_keys": decision.activation_keys,
            }
            for index, decision in enumerate(ranked, start=1)
        ]

    return registry.render_catalog(budget_chars=budget_chars)


def build_skill_catalog_section_for_agent(
    agent_id: uuid.UUID | None,
    *,
    budget_profile: ContextBudget | None = None,
    scenario_text: str | None = None,
    session_id: object | None = None,
    active_skill_names: tuple[str, ...] = (),
    path_triggered_skill_names: tuple[str, ...] = (),
    ranking_manifest: list[dict] | None = None,
) -> str:
    """Render the ## Skills catalog section for an agent's workspace.

    Step 9 (CC parity): the catalog is progressive-disclosure metadata that
    changes whenever skills are added/distilled. It belongs in the dynamic
    suffix (a per-round, non-cached reminder), NOT baked into the frozen prefix
    where it would bust the prompt-cache boundary on every skill change. This
    helper is the single source for that section; the invoker loads it once per
    invocation and threads it through `InvocationRequest.skill_catalog`.
    """
    if agent_id is None:
        return ""
    from app.runtime.prompt_sections import build_skills_catalog_section

    skill_budget = budget_profile.skill_catalog_budget_chars if budget_profile else 4000
    skills_text = _load_skills_index(
        agent_id,
        budget_chars=max(skill_budget, 800),
        scenario_text=scenario_text,
        session_id=session_id,
        active_skill_names=active_skill_names,
        path_triggered_skill_names=path_triggered_skill_names,
        ranking_manifest=ranking_manifest,
    )
    if len(skills_text) > skill_budget:
        skills_text = (
            skills_text[:skill_budget] + "\n\n...(skill catalog truncated — use `load_skill` to see full details)"
        )
    return build_skills_catalog_section(skills_text, budget_chars=skill_budget)


async def _build_runtime_metadata_sections(
    agent_id: uuid.UUID,
    *,
    current_user_name: str | None = None,
    triggers_budget_chars: int = 3000,
) -> list[str]:
    parts: list[str] = []

    from app.services.timezone_utils import get_agent_timezone, now_in_timezone

    agent_tz_name = await get_agent_timezone(agent_id)
    agent_local_now = now_in_timezone(agent_tz_name)
    now_str = agent_local_now.strftime(f"%Y-%m-%d %H:%M:%S ({agent_tz_name})")
    parts.append(f"\n## Current Time\n{now_str}")
    parts.append(
        f"Your timezone is **{agent_tz_name}**. When setting cron triggers, use this timezone for time references."
    )

    try:
        from app.database import tenant_scoped_session
        from app.models.trigger import AgentTrigger
        from app.services.tenant_resolver import resolve_tenant_for_agent
        from sqlalchemy import select as sa_select

        # RLS 阶段2b: agent_triggers now bears a USING-only policy. Pin the GUC
        # to the agent's tenant so this read survives the non-owner role; a bare
        # session would fail closed and silently drop the triggers section.
        _trigger_tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(_trigger_tenant_id) as db:
            result = await db.execute(
                sa_select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.is_enabled,
                )
            )
            triggers = result.scalars().all()
            if triggers:
                from app.runtime.prompt_sections import build_triggers_section

                trigger_rows = [
                    {
                        "name": t.name,
                        "type": t.type,
                        "config": t.config or {},
                        "reason": t.reason or "",
                    }
                    for t in triggers
                ]
                trigger_section = build_triggers_section(trigger_rows, budget_chars=triggers_budget_chars)
                if trigger_section:
                    parts.append("\n" + trigger_section)
    except Exception as exc:
        logger.debug("Failed to load active triggers for agent {}: {}", agent_id, exc)

    if current_user_name:
        parts.append(
            f"\n## Current Conversation\nYou are currently chatting with **{current_user_name}**. Address them by name when appropriate."
        )

    return parts


async def _build_a2a_collaborators_context(
    agent_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | str | None = None,
    budget_chars: int = 6000,
) -> str:
    """Build live A2A collaborator prompt context from DB policy state."""

    try:
        from app.database import tenant_scoped_session
        from app.runtime.prompt_sections import build_a2a_collaborators_section
        from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model

        if tenant_id is None:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            read_model = await build_a2a_collaboration_read_model(db, agent_id)
        return build_a2a_collaborators_section(read_model, max_chars=budget_chars)
    except Exception as exc:
        logger.debug("Failed to build A2A collaborator context for agent {}: {}", agent_id, exc)
        return ""


async def build_agent_runtime_context(
    agent_id: uuid.UUID,
    *,
    current_user_name: str | None = None,
    budget_profile: ContextBudget | None = None,
    tenant_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
) -> str:
    """Build volatile runtime context that should be refreshed every round."""
    triggers_budget = budget_profile.runtime_triggers_budget_chars if budget_profile else 3000
    sections = await _build_runtime_metadata_sections(
        agent_id,
        current_user_name=current_user_name,
        triggers_budget_chars=triggers_budget,
    )
    try:
        team_context = await build_prompt_facing_team_context(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        if team_context:
            sections.append("\n" + team_context)
    except Exception as exc:
        logger.debug("Failed to build team context for agent {}: {}", agent_id, exc)
    return "\n".join(sections)


async def build_agent_context(
    agent_id: uuid.UUID,
    agent_name: str,
    role_description: str = "",
    current_user_name: str | None = None,
    *,
    include_memory_file: bool = True,  # deprecated: memory flows via 4-layer retriever
    include_runtime_metadata: bool = False,
    include_focus: bool = True,  # deprecated: no longer projected into the prompt
    include_skill_catalog: bool = True,  # Step 9: invoker sets False — catalog flows via dynamic suffix
    budget_profile: ContextBudget | None = None,
    invocation_scope: str = "conversation",
    tenant_id: uuid.UUID | str | None = None,
) -> str:
    """Build a rich system prompt incorporating agent's full context.

    Reads from workspace files and live governance state:
    - soul.md → personality
    - skills/ → skill names + summaries
    - A2A collaborator read model → same-owner/public/group callable agents

    NOTE: canonical memory files are NOT loaded here. They flow through the
    4-layer retrieval pipeline (MemoryRetriever), which reads canonical T3
    markdown files as Semantic Memory. Loading them here as well would cause
    double-injection into the prompt.
    """
    tool_ws = TOOL_WORKSPACE / str(agent_id)
    data_ws = PERSISTENT_DATA / str(agent_id)
    _agent_tenant_id = tenant_id
    if _agent_tenant_id is None:
        try:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            _agent_tenant_id = await resolve_tenant_for_agent(agent_id)
        except Exception as exc:
            logger.warning("Failed to resolve tenant for frozen agent context {}: {}", agent_id, exc)

    # --- Soul ---
    soul_budget = budget_profile.soul_budget_chars if budget_profile else 16000
    a2a_budget = budget_profile.relationships_budget_chars if budget_profile else 6000
    company_info_budget = budget_profile.company_info_budget_chars if budget_profile else 5000
    org_structure_budget = budget_profile.org_structure_budget_chars if budget_profile else 2000
    soul = _read_file_safe(tool_ws / "soul.md", soul_budget) or _read_file_safe(data_ws / "soul.md", soul_budget)
    soul = _strip_primary_heading(soul)
    soul = _sanitize_prompt_context(soul, source_name="soul.md")

    # --- Memory ---
    # NOTE: canonical memory files are no longer loaded here. T3 markdown memory flows
    # through the 4-layer retrieval pipeline (MemoryRetriever → [Semantic Memory] section).
    # Loading them here would double-inject the same data.

    # --- Skills index ---
    # Step 9: the skill catalog is no longer baked into agent_context (= frozen
    # prefix). The invoker loads it via build_skill_catalog_section_for_agent and
    # threads it through the dynamic suffix (CC parity, prompt-cache stable). This
    # path stays for backward-compatible callers that still want it inline.

    # --- Compose system prompt using modular sections ---
    from app.runtime.prompt_sections import (
        build_identity_section,
        build_executing_actions_section,
        build_subagent_listing_section,
        build_tone_style_section,
    )

    identity_section = build_identity_section(
        agent_name=agent_name,
        role_description=role_description,
        invocation_scope=invocation_scope,
        soul_text=soul,
    )
    context_parts: list[str] = []

    # --- Channel integration skills (agent reads on demand from skills/ directory) ---
    _configured_channels = []
    try:
        if _agent_tenant_id is None:
            raise RuntimeError("agent tenant is required for channel context")
        from app.models.channel_config import ChannelConfig
        from app.database import tenant_scoped_session
        from sqlalchemy import select as sa_select

        async with tenant_scoped_session(
            _agent_tenant_id,
            require_tenant=True,
            source="frozen_agent_channel_context",
        ) as _ctx_db:
            _cfgs = await _ctx_db.execute(
                sa_select(ChannelConfig)
                .where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.tenant_id == _agent_tenant_id,
                    ChannelConfig.is_configured,
                )
                .order_by(ChannelConfig.channel_type)
            )
            _configured_channels = [c.channel_type for c in _cfgs.scalars().all()]
    except Exception as exc:
        logger.debug("Failed to query channel configs for agent {}: {}", agent_id, exc)

    if _configured_channels:
        channel_names = ", ".join(_configured_channels)
        context_parts.append(
            "### Channel Integrations\n"
            f"You have {channel_names} channel(s) configured. "
            "Read the matching integration skill before using channel-specific tools."
        )

    # --- Company Intro (from system settings) ---
    try:
        from app.database import tenant_scoped_session
        from app.models.system_settings import SystemSetting
        from sqlalchemy import select as sa_select

        if _agent_tenant_id is None:
            raise RuntimeError("agent tenant is required for company context")
        async with tenant_scoped_session(
            _agent_tenant_id,
            require_tenant=True,
            source="frozen_agent_company_context",
        ) as db:
            company_intro = ""

            # Priority 1: tenant_settings table (new)
            if _agent_tenant_id:
                try:
                    from app.models.tenant_setting import TenantSetting

                    result = await db.execute(
                        sa_select(TenantSetting).where(
                            TenantSetting.tenant_id == _agent_tenant_id,
                            TenantSetting.key == "company_intro",
                        )
                    )
                    ts = result.scalar_one_or_none()
                    if ts and ts.value and ts.value.get("content"):
                        company_intro = ts.value["content"].strip()
                except Exception as exc:
                    logger.debug("Failed to load tenant_settings company_intro for agent {}: {}", agent_id, exc)

            # Priority 2: system_settings with tenant-scoped key (backward compat)
            if not company_intro and _agent_tenant_id:
                tenant_key = f"company_intro_{_agent_tenant_id}"
                result = await db.execute(sa_select(SystemSetting).where(SystemSetting.key == tenant_key))
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = setting.value["content"].strip()

            # Priority 3: global system_settings fallback
            if not company_intro:
                result = await db.execute(sa_select(SystemSetting).where(SystemSetting.key == "company_intro"))
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = setting.value["content"].strip()

            if company_intro:
                # Cap to prevent unbounded prompt growth from large tenant metadata
                if len(company_intro) > company_info_budget:
                    company_intro = company_intro[:company_info_budget] + "\n...(company info truncated)"
                company_intro = _sanitize_prompt_context(company_intro, source_name="company_intro")
                context_parts.append(f"### Company Information\n{company_intro}")
    except Exception as exc:
        logger.debug("Failed to load company intro for agent {}: {}", agent_id, exc)

    # --- Organization Structure (from synced workspace file) ---
    if _agent_tenant_id:
        org_path = PERSISTENT_DATA / f"enterprise_info_{_agent_tenant_id}" / "org_structure.md"
        org_structure = _read_file_safe(org_path, org_structure_budget)
        if org_structure and "尚未同步" not in org_structure and "尚未填写" not in org_structure:
            if org_structure.startswith("# "):
                org_structure = "\n".join(org_structure.split("\n")[1:]).strip()
            if org_structure:
                org_structure = _sanitize_prompt_context(org_structure, source_name="org_structure.md")
                context_parts.append(f"### Organization Structure\n{org_structure}")

    # soul personality is now rendered inside identity_section (build_identity_section)

    # Skills and relationships use modular section builders.
    # Step 9: catalog defaults to the dynamic suffix (invoker passes
    # include_skill_catalog=False); only inline-context callers render it here.
    skills_section = (
        build_skill_catalog_section_for_agent(agent_id, budget_profile=budget_profile) if include_skill_catalog else ""
    )
    a2a_section = await _build_a2a_collaborators_context(
        agent_id,
        tenant_id=_agent_tenant_id,
        budget_chars=a2a_budget,
    )

    # Operating contract via modular section
    operating_contract = build_executing_actions_section(invocation_scope)
    subagent_listing = build_subagent_listing_section(agent_id=agent_id, tenant_id=_agent_tenant_id)
    tone_style = build_tone_style_section()

    if include_runtime_metadata:
        context_parts.extend(
            await _build_runtime_metadata_sections(
                agent_id,
                current_user_name=current_user_name,
                triggers_budget_chars=budget_profile.runtime_triggers_budget_chars if budget_profile else 3000,
            )
        )

    rendered_parts = [
        identity_section,
        operating_contract,
        subagent_listing,
        tone_style,
    ]
    # Context material (company info, org structure, channels)
    context_block = "\n\n".join(context_parts) if context_parts else "(no additional context material loaded)"
    rendered_parts.append("## Context Material\n\n" + context_block)
    # Skills catalog
    if skills_section:
        rendered_parts.append(skills_section)
    # A2A collaborators
    if a2a_section:
        rendered_parts.append(a2a_section)

    return "\n\n".join(part.strip() for part in rendered_parts if part and part.strip())
