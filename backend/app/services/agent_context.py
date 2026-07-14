"""Build rich system prompt context for agents.

Loads soul, layered memory context, skills summary, and live A2A collaborators
and composes a comprehensive system prompt.
"""

from dataclasses import dataclass
import hashlib
import json
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
AGENT_CONTEXT_RESOURCE_REFS: tuple[str, ...] = (
    "index",
    "soul",
    "company",
    "organization",
    "channels",
    "a2a-collaborators",
)
_AGENT_CONTEXT_CONTENT_RESOURCE_REFS = AGENT_CONTEXT_RESOURCE_REFS[1:]


@dataclass(frozen=True, slots=True)
class AgentContextResource:
    """One governed, agent-bound source that may be previewed in the prompt.

    The full text is never caller-addressable by filesystem path, tenant id, or
    agent id. ``read_context_resource`` resolves this object from the trusted
    tool execution context and uses ``sha256`` as the continuation version.
    """

    ref: str
    source_ref: str
    content: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


def _context_resource_continuation(resource: AgentContextResource, *, offset: int) -> str:
    call = json.dumps(
        {
            "ref": resource.ref,
            "offset": offset,
            "expected_sha256": resource.sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        f"\n[context_ref={resource.source_ref} sha256={resource.sha256} "
        f"shown_chars={offset} total_chars={len(resource.content)}; "
        f"continue with `read_context_resource({call})`.]"
    )


def render_context_resource_excerpt(resource: AgentContextResource, *, budget_chars: int) -> str:
    """Render complete authorized context; the final provider gate owns capacity."""

    del budget_chars
    return resource.content.strip()


def _read_file_safe(path: Path) -> str:
    """Read a complete file and return empty if it is missing or unreadable."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def _read_identity_file(paths: tuple[Path, ...], *, source_name: str) -> str:
    """Read an optional identity file, distinguishing missing from unreadable."""
    failures: list[Exception] = []
    for path in paths:
        try:
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as exc:
            failures.append(exc)
            continue
        if content:
            return content
    if failures:
        from app.kernel.contracts import ContextDependencyUnavailable

        logger.error("Required identity source {} is unreadable", source_name)
        raise ContextDependencyUnavailable(
            dependency="soul",
            code="soul_context_unavailable",
            user_message="Required agent identity is temporarily unavailable. This turn was stopped safely.",
            retryable=True,
        ) from failures[-1]
    return ""


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
                        description = val
            if description:
                return name, description

    # Fallback: use first non-empty, non-heading line as description
    for line in stripped.split("\n"):
        line = line.strip()
        # Skip frontmatter delimiters and YAML lines
        if line in ("---",) or line.startswith("name:") or line.startswith("description:"):
            continue
        if line and not line.startswith("#"):
            description = line
            break
    if not description:
        lines = stripped.split("\n")
        if lines:
            description = lines[0].strip().lstrip("# ")

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

    catalog = registry.render_catalog(budget_chars=budget_chars)
    provisional_names = [decision.skill.metadata.name for decision in ranked if decision.state == "provisional"]
    if provisional_names:
        catalog += (
            "\n\n**Skills on provisional trial:** "
            + ", ".join(provisional_names)
            + ". These versions are loadable and monitored; successful use promotes them, "
            + "while repeated negative evidence rolls them back."
        )
    return catalog


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
    budget_chars: int | None = 6000,
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


async def _load_configured_channel_types(
    agent_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | str | None,
) -> tuple[str, ...]:
    if tenant_id is None:
        return ()
    try:
        from app.database import tenant_scoped_session
        from app.models.channel_config import ChannelConfig
        from sqlalchemy import select as sa_select

        async with tenant_scoped_session(
            tenant_id,
            require_tenant=True,
            source="agent_context_resource_channels",
        ) as db:
            result = await db.execute(
                sa_select(ChannelConfig.channel_type)
                .where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.tenant_id == tenant_id,
                    ChannelConfig.is_configured,
                )
                .order_by(ChannelConfig.channel_type)
            )
            values = result.scalars().all()
            return tuple(
                str(getattr(value, "channel_type", value))
                for value in values
                if str(getattr(value, "channel_type", value)).strip()
            )
    except Exception as exc:
        logger.debug("Failed to query channel configs for agent {}: {}", agent_id, exc)
        return ()


async def _load_company_intro(
    agent_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | str | None,
) -> str:
    if tenant_id is None:
        return ""
    try:
        from app.database import tenant_scoped_session
        from app.models.system_settings import SystemSetting
        from sqlalchemy import select as sa_select

        async with tenant_scoped_session(
            tenant_id,
            require_tenant=True,
            source="agent_context_resource_company",
        ) as db:
            company_intro = ""
            try:
                from app.models.tenant_setting import TenantSetting

                result = await db.execute(
                    sa_select(TenantSetting).where(
                        TenantSetting.tenant_id == tenant_id,
                        TenantSetting.key == "company_intro",
                    )
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = str(setting.value["content"]).strip()
            except Exception as exc:
                logger.debug("Failed to load tenant company intro for agent {}: {}", agent_id, exc)

            if not company_intro:
                tenant_key = f"company_intro_{tenant_id}"
                result = await db.execute(sa_select(SystemSetting).where(SystemSetting.key == tenant_key))
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = str(setting.value["content"]).strip()

            if not company_intro:
                result = await db.execute(sa_select(SystemSetting).where(SystemSetting.key == "company_intro"))
                setting = result.scalar_one_or_none()
                if setting and setting.value and setting.value.get("content"):
                    company_intro = str(setting.value["content"]).strip()
            return company_intro
    except Exception as exc:
        logger.debug("Failed to load company intro for agent {}: {}", agent_id, exc)
        return ""


async def load_agent_context_resource(
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | str | None,
    resource_ref: str,
) -> AgentContextResource:
    """Resolve an agent-bound full context resource from trusted authority."""

    if resource_ref not in AGENT_CONTEXT_RESOURCE_REFS:
        raise ValueError(f"unsupported agent context resource: {resource_ref}")

    source_ref = f"agent-context://{resource_ref}"
    tool_ws = TOOL_WORKSPACE / str(agent_id)
    data_ws = PERSISTENT_DATA / str(agent_id)

    if resource_ref == "soul":
        content = _read_identity_file(
            (tool_ws / "soul.md", data_ws / "soul.md"),
            source_name="soul.md",
        )
        content = _strip_primary_heading(content).strip()
        return AgentContextResource(ref=resource_ref, source_ref=source_ref, content=content)

    if resource_ref == "company":
        content = await _load_company_intro(agent_id, tenant_id=tenant_id)
        return AgentContextResource(ref=resource_ref, source_ref=source_ref, content=content)

    if resource_ref == "organization":
        content = ""
        if tenant_id is not None:
            org_path = PERSISTENT_DATA / f"enterprise_info_{tenant_id}" / "org_structure.md"
            content = _read_file_safe(org_path)
            if "尚未同步" in content or "尚未填写" in content:
                content = ""
            content = _strip_primary_heading(content).strip()
        return AgentContextResource(ref=resource_ref, source_ref=source_ref, content=content)

    if resource_ref == "channels":
        channels = await _load_configured_channel_types(agent_id, tenant_id=tenant_id)
        return AgentContextResource(ref=resource_ref, source_ref=source_ref, content=", ".join(channels))

    if resource_ref == "a2a-collaborators":
        content = await _build_a2a_collaborators_context(
            agent_id,
            tenant_id=tenant_id,
            budget_chars=None,
        )
        return AgentContextResource(ref=resource_ref, source_ref=source_ref, content=content)

    resources = [
        await load_agent_context_resource(
            agent_id=agent_id,
            tenant_id=tenant_id,
            resource_ref=ref,
        )
        for ref in _AGENT_CONTEXT_CONTENT_RESOURCE_REFS
    ]
    lines = [
        "Agent context resources are read-only runtime context, not Personal Knowledge Base content.",
        "Call read_context_resource with one ref and the listed sha256 when more detail is relevant.",
    ]
    lines.extend(
        f"- {resource.ref}: available={bool(resource.content)}, chars={len(resource.content)}, "
        f"sha256={resource.sha256}, source_ref={resource.source_ref}"
        for resource in resources
    )
    return AgentContextResource(ref=resource_ref, source_ref=source_ref, content="\n".join(lines))


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
    _agent_tenant_id = tenant_id
    if _agent_tenant_id is None:
        try:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            _agent_tenant_id = await resolve_tenant_for_agent(agent_id)
        except Exception as exc:
            logger.warning("Failed to resolve tenant for frozen agent context {}: {}", agent_id, exc)

    # --- Soul ---
    soul_resource = await load_agent_context_resource(
        agent_id=agent_id,
        tenant_id=_agent_tenant_id,
        resource_ref="soul",
    )
    soul = soul_resource.content.strip()

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

    # These previews and the on-demand tool share one loader, so no second
    # context truth can drift from what the model can recover later.
    channels_resource = await load_agent_context_resource(
        agent_id=agent_id,
        tenant_id=_agent_tenant_id,
        resource_ref="channels",
    )
    if channels_resource.content:
        context_parts.append(
            "### Channel Integrations\n"
            f"You have {channels_resource.content} channel(s) configured. "
            "Read the matching integration skill before using channel-specific tools."
        )

    company_resource = await load_agent_context_resource(
        agent_id=agent_id,
        tenant_id=_agent_tenant_id,
        resource_ref="company",
    )
    if company_resource.content:
        context_parts.append(f"### Company Information\n{company_resource.content.strip()}")

    organization_resource = await load_agent_context_resource(
        agent_id=agent_id,
        tenant_id=_agent_tenant_id,
        resource_ref="organization",
    )
    if organization_resource.content:
        context_parts.append(f"### Organization Structure\n{organization_resource.content.strip()}")

    # soul personality is now rendered inside identity_section (build_identity_section)

    # Skills and relationships use modular section builders.
    # Step 9: catalog defaults to the dynamic suffix (invoker passes
    # include_skill_catalog=False); only inline-context callers render it here.
    skills_section = (
        build_skill_catalog_section_for_agent(agent_id, budget_profile=budget_profile) if include_skill_catalog else ""
    )
    a2a_resource = await load_agent_context_resource(
        agent_id=agent_id,
        tenant_id=_agent_tenant_id,
        resource_ref="a2a-collaborators",
    )
    a2a_section = a2a_resource.content.strip()

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
