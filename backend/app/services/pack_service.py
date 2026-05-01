"""Pack service — compute pack availability, capability mapping, and session runtime state."""

import json
import logging
import uuid
from collections import defaultdict
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.llm import LLMModel
from app.packs.catalog_reader import PackCatalogReader, PackManifest
from app.services.agent_tools import CORE_TOOL_NAMES, get_combined_openai_tools
from app.services.capability_gate import CAPABILITY_MAP
from app.services.llm_client import get_provider_spec
from app.services.pack_policy_service import get_tenant_pack_policies, is_pack_enabled
from app.services.token_tracker import estimate_tokens_from_chars
from app.skills.types import ParsedSkill
from app.tools import ensure_workspace
from app.tools.packs import TOOL_PACKS, ToolPackSpec, infer_static_pack_names, pack_for_name

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKS_DIR = _REPO_ROOT / "packs"

# Kernel tools must mirror the runtime's real minimal toolset.
# Lazy-computed from the canonical collected tool surface.
_KERNEL_TOOLS: tuple[str, ...] | None = None


def _compute_kernel_tools() -> tuple[str, ...]:
    global _KERNEL_TOOLS
    if _KERNEL_TOOLS is None:
        _KERNEL_TOOLS = tuple(
            tool["function"]["name"]
            for tool in get_combined_openai_tools()
            if tool["function"]["name"] in CORE_TOOL_NAMES
        )
    return _KERNEL_TOOLS


# Public tuple-like export resolved lazily on first access.
class _LazyKernelTools(tuple):
    """Tuple subclass that populates on first iteration / membership test."""

    _resolved: tuple[str, ...] | None = None

    def _ensure(self) -> tuple[str, ...]:
        if self._resolved is None:
            self._resolved = _compute_kernel_tools()
        return self._resolved

    def __contains__(self, item: object) -> bool:
        return item in self._ensure()

    def __iter__(self):
        return iter(self._ensure())

    def __len__(self) -> int:
        return len(self._ensure())

    def __repr__(self) -> str:
        return repr(self._ensure())

    def __eq__(self, other: object) -> bool:
        return self._ensure() == other

    def __hash__(self) -> int:
        return hash(self._ensure())


KERNEL_TOOLS: tuple[str, ...] = _LazyKernelTools()

# Channel type → pack name mapping
_CHANNEL_PACK_MAP = {
    "feishu": "feishu_pack",
}


def _pack_to_dict(pack: ToolPackSpec) -> dict:
    """Serialize a ToolPackSpec with capability annotations."""
    capabilities = set()
    for tool in pack.tools:
        cap = CAPABILITY_MAP.get(tool)
        if cap:
            capabilities.add(cap)

    requires_channel = None
    if pack.source == "channel":
        for ch, pname in _CHANNEL_PACK_MAP.items():
            if pname == pack.name:
                requires_channel = ch
                break

    return {
        "name": pack.name,
        "summary": pack.summary,
        "source": pack.source,
        "activation_mode": pack.activation_mode,
        "tools": list(pack.tools),
        "capabilities": sorted(capabilities),
        "requires_channel": requires_channel,
    }


def _manifest_pack_to_dict(manifest: PackManifest) -> dict:
    """Serialize a pack.yaml manifest for catalog surfaces only."""
    capabilities = set()
    for tool in manifest.tool_names:
        cap = CAPABILITY_MAP.get(tool)
        if cap:
            capabilities.add(cap)

    return {
        "name": manifest.name,
        "summary": manifest.description,
        "source": "manifest",
        "activation_mode": "通过 pack.yaml catalog 展示；runtime 仍由 @tool(ToolMeta.pack) 决定",
        "tools": list(manifest.tool_names),
        "capabilities": sorted(capabilities),
        "requires_channel": None,
        "version": manifest.version,
        "license": manifest.license,
        "author": manifest.author,
        "skills": list(manifest.skills),
        "data_sources": manifest.data_sources,
        "mcp_servers": list(manifest.mcp_servers),
        "credential_requirements": list(manifest.credential_requirements),
        "activation": manifest.activation,
        "sandbox_requirements": manifest.sandbox_requirements,
        "runtime_source_of_truth": "tool_decorator",
    }


def _load_manifest_pack_catalog() -> list[dict]:
    reader = PackCatalogReader(_PACKS_DIR)
    reader.discover()
    return [_manifest_pack_to_dict(manifest) for manifest in reader.list_packs()]


def get_pack_catalog() -> list[dict]:
    """Return full pack catalog with capability annotations."""
    catalog = [_pack_to_dict(p) for p in TOOL_PACKS]
    existing_names = {pack["name"] for pack in catalog}
    for manifest_pack in _load_manifest_pack_catalog():
        if manifest_pack["name"] not in existing_names:
            catalog.append(manifest_pack)
    return catalog


async def get_tenant_pack_catalog(db: AsyncSession, tenant_id: uuid.UUID | None) -> list[dict]:
    """Return pack catalog annotated with tenant enablement state."""
    policies = await get_tenant_pack_policies(db, tenant_id)
    catalog: list[dict] = []
    for pack in get_pack_catalog():
        catalog.append({**pack, "enabled": is_pack_enabled(policies, pack["name"])})
    return catalog


def _resolve_session_conversation_id(session) -> str:
    """Resolve the persisted ChatMessage conversation_id for a session.

    ChatMessage.conversation_id is stored as the ChatSession UUID across web,
    Feishu, and agent sessions. external_conv_id is only for find-or-create.
    """
    return str(session.id)


def _load_json_content(content: str) -> dict:
    try:
        data = json.loads(content or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_isoformat(value) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return None
    try:
        return isoformat()
    except Exception:
        return None


def _summarize_chat_messages(messages: list) -> dict:
    """Summarize runtime events and tool usage from persisted ChatMessage rows."""
    activated_packs: list[str] = []
    used_tools: set[str] = set()
    blocked_capabilities: list[dict] = []
    compaction_count = 0
    permission_event_count = 0
    team_memory_hit_count = 0
    last_compaction: dict | None = None
    last_team_memory_hit: dict | None = None
    last_tool_budget_event: dict | None = None
    last_retry_reason: str | None = None

    for msg in messages:
        if getattr(msg, "role", None) == "tool_call":
            tool_data = _load_json_content(getattr(msg, "content", ""))
            tool_name = tool_data.get("name")
            if tool_name:
                used_tools.add(tool_name)
            continue

        if getattr(msg, "role", None) != "system":
            continue

        event_data = _load_json_content(getattr(msg, "content", ""))
        event_type = event_data.get("event_type") or event_data.get("type")

        if event_type == "pack_activation":
            for pack in event_data.get("packs", []):
                name = pack.get("name") if isinstance(pack, dict) else str(pack)
                if name and name not in activated_packs:
                    activated_packs.append(name)
            continue

        if event_type == "permission":
            permission_event_count += 1
            status = event_data.get("status")
            if event_data.get("retry_reason"):
                last_retry_reason = event_data.get("retry_reason")
            if status in {"blocked", "capability_denied", "approval_required"}:
                blocked_capabilities.append(
                    {
                        "tool": event_data.get("tool_name"),
                        "status": status,
                        "capability": event_data.get("capability"),
                    }
                )
            continue

        if event_type == "team_memory":
            team_memory_hit_count += 1
            last_team_memory_hit = {
                "workspace_key": event_data.get("workspace_key"),
                "query": event_data.get("query"),
                "matched_keys": list(event_data.get("matched_keys") or []),
                "created_at": _safe_isoformat(getattr(msg, "created_at", None)),
            }
            continue

        if event_type == "session_compact":
            compaction_count += 1
            if event_data.get("budget_reason"):
                last_tool_budget_event = {
                    "reason": event_data.get("budget_reason"),
                    "tool_name": event_data.get("tool_name"),
                    "created_at": _safe_isoformat(getattr(msg, "created_at", None)),
                }
            last_compaction = {
                "summary": event_data.get("summary"),
                "original_message_count": event_data.get("original_message_count"),
                "kept_message_count": event_data.get("kept_message_count"),
                "continuity_sections_injected": list(event_data.get("continuity_sections_injected") or []),
                "created_at": _safe_isoformat(getattr(msg, "created_at", None)),
            }

    return {
        "activated_packs": activated_packs,
        "used_tools": sorted(used_tools),
        "blocked_capabilities": blocked_capabilities,
        "compaction_count": compaction_count,
        "permission_event_count": permission_event_count,
        "team_memory_hit_count": team_memory_hit_count,
        "last_compaction": last_compaction,
        "last_team_memory_hit": last_team_memory_hit,
        "last_tool_budget_event": last_tool_budget_event,
        "last_retry_reason": last_retry_reason,
    }


def _estimate_session_input_tokens(messages: list) -> int:
    total_chars = 0
    for msg in messages:
        total_chars += len(getattr(msg, "content", "") or "")
        total_chars += len(getattr(msg, "thinking", "") or "")
    if total_chars <= 0:
        return 0
    return estimate_tokens_from_chars(total_chars)


def _resolve_context_window_tokens(model: LLMModel | None, agent: Agent | None) -> int | None:
    if model and getattr(model, "max_input_tokens", None):
        return model.max_input_tokens
    provider_spec = get_provider_spec(getattr(model, "provider", "")) if model else None
    if provider_spec and getattr(provider_spec, "max_input_tokens", None):
        return provider_spec.max_input_tokens
    context_window = getattr(agent, "context_window_size", None) if agent else None
    if isinstance(context_window, int) and context_window > 0:
        return context_window
    return None


def collect_skill_declared_packs(skills: list[ParsedSkill]) -> list[dict]:
    """Merge explicit and inferred pack declarations from parsed skills."""
    grouped: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"skills": set(), "tools": set()})

    for skill in skills:
        explicit_packs = tuple(skill.metadata.declared_packs or ())
        inferred_packs = infer_static_pack_names(skill.metadata.declared_tools)
        pack_names: list[str] = []
        seen: set[str] = set()
        for pack_name in [*explicit_packs, *inferred_packs]:
            if pack_name and pack_name not in seen:
                pack_names.append(pack_name)
                seen.add(pack_name)
        for pack_name in pack_names:
            grouped[pack_name]["skills"].add(skill.metadata.name)
            grouped[pack_name]["tools"].update(skill.metadata.declared_tools)

    result: list[dict] = []
    for pack_name in sorted(grouped):
        bucket = grouped[pack_name]
        result.append(
            {
                "name": pack_name,
                "skills": sorted(bucket["skills"]),
                "tools": sorted(bucket["tools"]),
            }
        )
    return result


async def _load_agent_skill_declared_packs(agent_id: uuid.UUID) -> list[dict]:
    try:
        from app.skills.loader import WorkspaceSkillLoader

        workspace = await ensure_workspace(agent_id)
        parsed = WorkspaceSkillLoader().load_from_workspace(workspace)
        return collect_skill_declared_packs(parsed)
    except Exception as exc:
        logger.debug("Failed to load workspace skills for agent %s: %s", agent_id, exc)
        return []


async def get_agent_packs(db: AsyncSession, agent_id: uuid.UUID) -> dict:
    """Compute which packs are available for a specific agent.

    Returns:
        {
            "kernel_tools": [...],
            "available_packs": [...],
            "channel_backed_packs": [...],
            "skill_declared_packs": [],
        }
    """
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return {
            "kernel_tools": list(KERNEL_TOOLS),
            "available_packs": [],
            "channel_backed_packs": [],
            "skill_declared_packs": [],
        }

    pack_policies = await get_tenant_pack_policies(db, agent.tenant_id)

    # Check which channels are configured for this agent
    channel_result = await db.execute(
        select(ChannelConfig.channel_type).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.is_configured == True,  # noqa: E712
        )
    )
    configured_channels = {row[0] for row in channel_result.all()}

    available = []
    channel_backed = []
    for pack in TOOL_PACKS:
        pack_dict = _pack_to_dict(pack)
        pack_dict["enabled"] = is_pack_enabled(pack_policies, pack.name)
        if not pack_dict["enabled"]:
            continue

        if pack.source == "channel":
            required_ch = pack_dict.get("requires_channel")
            if required_ch and required_ch in configured_channels:
                channel_backed.append(pack_dict)
            # Channel packs only in channel_backed — frontend merges both lists
        else:
            available.append(pack_dict)

    skill_declared_packs = []
    for pack in await _load_agent_skill_declared_packs(agent_id):
        if not is_pack_enabled(pack_policies, pack["name"]):
            continue
        base = pack_for_name(pack["name"])
        skill_declared_packs.append(
            {
                **pack,
                "summary": base.summary if base else "",
                "source": base.source if base else "skill",
                "activation_mode": base.activation_mode if base else "通过 skill 激活",
                "enabled": True,
            }
        )

    return {
        "kernel_tools": list(KERNEL_TOOLS),
        "available_packs": available,
        "channel_backed_packs": channel_backed,
        "skill_declared_packs": skill_declared_packs,
    }


async def get_capability_summary(db: AsyncSession, agent_id: uuid.UUID) -> dict:
    """Build comprehensive capability summary for an agent.

    Returns:
        {
            "kernel_tools": [...],
            "available_packs": [...],
            "capability_policies": [...],
            "pending_approvals": int,
        }
    """
    from app.models.audit import ApprovalRequest
    from app.models.capability_policy import CapabilityPolicy

    # Get agent info
    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return {
            "kernel_tools": list(KERNEL_TOOLS),
            "available_packs": [],
            "channel_backed_packs": [],
            "skill_declared_packs": [],
            "capability_policies": [],
            "pending_approvals": 0,
        }

    # Get packs
    packs_data = await get_agent_packs(db, agent_id)

    # Get capability policies for this agent + tenant defaults
    policies = []
    if agent.tenant_id:
        tenant_uuid = uuid.UUID(agent.tenant_id) if isinstance(agent.tenant_id, str) else agent.tenant_id
        policy_result = await db.execute(
            select(CapabilityPolicy)
            .where(
                CapabilityPolicy.tenant_id == tenant_uuid,
                (CapabilityPolicy.agent_id == agent_id) | (CapabilityPolicy.agent_id.is_(None)),
            )
            .order_by(CapabilityPolicy.capability)
        )
        for p in policy_result.scalars().all():
            policies.append(
                {
                    "id": str(p.id),
                    "capability": p.capability,
                    "allowed": p.allowed,
                    "requires_approval": p.requires_approval,
                    "scope": "agent" if p.agent_id else "tenant",
                }
            )

    # Count pending approvals
    pending_result = await db.execute(
        select(func.count())
        .select_from(ApprovalRequest)
        .where(
            ApprovalRequest.agent_id == agent_id,
            ApprovalRequest.status == "pending",
        )
    )
    pending_count = pending_result.scalar() or 0

    return {
        "kernel_tools": packs_data["kernel_tools"],
        "available_packs": packs_data["available_packs"],
        "channel_backed_packs": packs_data["channel_backed_packs"],
        "skill_declared_packs": packs_data["skill_declared_packs"],
        "capability_policies": policies,
        "pending_approvals": pending_count,
    }


async def get_session_runtime_summary(db: AsyncSession, session_id: uuid.UUID) -> dict:
    """Build runtime summary for a chat session.

    Finds messages via ChatSession.id → conversation_id → ChatMessage.conversation_id,
    then scans parts for pack activations, tool calls, and permission events.
    """
    from app.models.audit import ChatMessage
    from app.models.chat_session import ChatSession

    # Resolve conversation_id from session
    sess_result = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
    session = sess_result.scalar_one_or_none()
    if not session:
        return {
            "activated_packs": [],
            "used_tools": [],
            "blocked_capabilities": [],
            "compaction_count": 0,
            "permission_event_count": 0,
            "team_memory_hit_count": 0,
            "last_compaction": None,
            "last_team_memory_hit": None,
            "last_tool_budget_event": None,
            "last_retry_reason": None,
        }

    agent_result = await db.execute(select(Agent).where(Agent.id == session.agent_id))
    agent = agent_result.scalar_one_or_none()

    model = None
    if agent and getattr(agent, "primary_model_id", None):
        model_result = await db.execute(select(LLMModel).where(LLMModel.id == agent.primary_model_id))
        model = model_result.scalar_one_or_none()

    conv_id = _resolve_session_conversation_id(session)
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == session.agent_id,
            ChatMessage.conversation_id == conv_id,
        )
        .order_by(ChatMessage.created_at)
    )
    messages = result.scalars().all()
    summary = _summarize_chat_messages(messages)
    context_window_tokens = _resolve_context_window_tokens(model, agent)
    estimated_input_tokens = _estimate_session_input_tokens(messages)

    summary["model"] = {
        "label": getattr(model, "label", None),
        "provider": getattr(model, "provider", None),
        "name": getattr(model, "model", None),
        "supports_vision": getattr(model, "supports_vision", None),
        "context_window_tokens": context_window_tokens,
    }
    summary["runtime"] = {
        "estimated_input_tokens": estimated_input_tokens,
        "remaining_tokens_estimate": (
            max(context_window_tokens - estimated_input_tokens, 0)
            if isinstance(context_window_tokens, int)
            else None
        ),
    }
    return summary
