"""Resolve governance context and dependencies for tool execution."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.policy import write_audit_event
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.services.approval_service import approval_service
from app.services.capability_gate import check_capability
from app.tools.governance import GovernanceDependencies, ToolGovernanceContext
from app.tools.runtime import ToolExecutionContext

logger = logging.getLogger(__name__)

_GOVERNANCE_LOOKUP_CACHE_TTL_SECONDS = 15.0
_GOVERNANCE_LOOKUP_CACHE_MAX_ENTRIES = 2048


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: Any


class _TtlSingleFlightCache:
    """Short-lived async cache that coalesces concurrent lookups by key."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _GOVERNANCE_LOOKUP_CACHE_TTL_SECONDS,
        max_entries: int = _GOVERNANCE_LOOKUP_CACHE_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[object, _CacheEntry] = {}
        self._locks: dict[object, asyncio.Lock] = {}

    async def get(self, key: object, loader: Callable[[], Awaitable[Any]]) -> Any:
        now = time.monotonic()
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.value

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > now:
                return entry.value

            value = await loader()
            self._entries[key] = _CacheEntry(expires_at=time.monotonic() + self._ttl_seconds, value=value)
            self._prune()
            return value

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()

    def _prune(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
            self._locks.pop(key, None)
        while len(self._entries) > self._max_entries:
            key = next(iter(self._entries))
            self._entries.pop(key, None)
            self._locks.pop(key, None)


class ToolGovernanceResolver:
    """Build governance context and dependency wrappers for tool runtime."""

    def __init__(self, *, lookup_cache_ttl_seconds: float = _GOVERNANCE_LOOKUP_CACHE_TTL_SECONDS) -> None:
        self._security_zone_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)
        self._capability_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)
        self._mcp_tool_mode_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)

    def clear_lookup_cache(self) -> None:
        self._security_zone_cache.clear()
        self._capability_cache.clear()
        self._mcp_tool_mode_cache.clear()

    async def build_context(
        self,
        *,
        runtime_context: ToolExecutionContext,
        tool_name: str,
        arguments: dict,
        tool_call_id: str | None = None,
        delegation_token: Any | None = None,
    ) -> ToolGovernanceContext:
        return ToolGovernanceContext(
            agent_id=runtime_context.agent_id,
            user_id=runtime_context.user_id,
            tenant_id=runtime_context.tenant_id,
            tool_name=tool_name,
            arguments=arguments,
            session_id=runtime_context.session_id,
            tool_call_id=tool_call_id,
            delegation_token=delegation_token,
            permission_profile=runtime_context.permission_profile,
            turn_id=runtime_context.turn_id,
            runtime_task_id=runtime_context.runtime_task_id,
            origin_channel=runtime_context.origin_channel,
            round_state=dict(runtime_context.round_state or {}),
            t0_refs=tuple(runtime_context.t0_refs or ()),
        )

    def build_dependencies(self) -> GovernanceDependencies:
        async def _resolve_security_zone(agent_id: uuid.UUID) -> str:
            async def _load() -> str:
                try:
                    async with async_session() as db:
                        async with enter_rls_bypass(db, reason=f"security-zone resolution for agent {agent_id}"):
                            result = await db.execute(select(Agent).where(Agent.id == agent_id))
                            agent = result.scalar_one_or_none()
                            zone = getattr(agent, "security_zone", None)
                            if not zone:
                                logger.warning(
                                    "[Governance] Agent %s has no security_zone set — defaulting to 'restricted'",
                                    agent_id,
                                )
                        await db.rollback()
                        return zone or "restricted"
                except Exception as exc:
                    logger.error(
                        "[Governance] Failed to resolve security zone for %s: %s — defaulting to 'restricted'",
                        agent_id,
                        exc,
                    )
                    return "restricted"

            return await self._security_zone_cache.get(agent_id, _load)

        async def _check_capability(tenant_id: uuid.UUID, agent_id: uuid.UUID, tool_name: str):
            async def _load():
                async with tenant_scoped_session(tenant_id) as db:
                    return await check_capability(db, tenant_id, agent_id, tool_name)

            return await self._capability_cache.get((tenant_id, agent_id, tool_name), _load)

        async def _write_audit_event(**kwargs) -> None:
            # RLS: security_audit_events is policied (stage-2a). Scope to the
            # event's tenant so the event-hash SELECT + INSERT survive the
            # non-owner role flip (a bare session fail-closes the hash-chain read).
            async with tenant_scoped_session(kwargs.get("tenant_id")) as db:
                await write_audit_event(db, **kwargs)
                await db.commit()

        async def _request_approval(
            *,
            agent_id: uuid.UUID,
            user_id: uuid.UUID,
            tool_name: str,
            arguments: dict,
            capability: str,
            reason: str | None = None,
            session_id: str | None = None,
            approval_origin_type: str | None = None,
        ) -> dict:
            async with async_session() as db, enter_rls_bypass(db, reason=f"approval request for agent {agent_id}"):
                result = await db.execute(select(Agent).where(Agent.id == agent_id))
                agent = result.scalar_one_or_none()
                if not agent:
                    return {"allowed": False, "message": "Agent not found"}
                origin_type = approval_origin_type or ("agent_session" if session_id else "approval_request")
                outcome = await approval_service.request_approval(
                    db,
                    agent,
                    action_type=capability,
                    details={
                        "tool": tool_name,
                        "args": arguments,
                        "requested_by": str(user_id),
                        "reason": reason,
                        "session_id": session_id,
                        "origin": {
                            "type": origin_type,
                            "session_id": session_id,
                        },
                    },
                )
                await db.commit()
                return outcome

        async def _resolve_mcp_tool_mode(
            agent_id: uuid.UUID,
            tool_name: str,
            arguments: dict,
        ) -> str | None:
            # Closure A2: feed the governance MCP gate. call_mcp_tool is the
            # generic entry — the governed object is the target tool inside
            # its arguments; dynamic MCP tool names govern themselves.
            target = arguments.get("tool_name") if tool_name == "call_mcp_tool" else tool_name
            if not target or not isinstance(target, str):
                return None

            async def _load() -> str | None:
                from app.models.tool import AgentTool, Tool
                from app.services.mcp_server_service import resolve_agent_mcp_tool_mode

                async with async_session() as db:
                    async with enter_rls_bypass(db, reason=f"MCP tool-mode resolution for agent {agent_id}"):
                        result = await db.execute(
                            select(Tool)
                            .join(AgentTool, AgentTool.tool_id == Tool.id)
                            .where(
                                AgentTool.agent_id == agent_id,
                                AgentTool.enabled.is_(True),
                                Tool.name == target,
                                Tool.type == "mcp",
                                Tool.enabled.is_(True),
                            )
                        )
                        tool = result.scalar_one_or_none()
                        mode = None
                        if tool is not None:
                            mode = await resolve_agent_mcp_tool_mode(db, agent_id, tool)
                    await db.rollback()
                    return mode

            return await self._mcp_tool_mode_cache.get((agent_id, target), _load)

        return GovernanceDependencies(
            resolve_security_zone=_resolve_security_zone,
            check_capability=_check_capability,
            write_audit_event=_write_audit_event,
            request_approval=_request_approval,
            resolve_mcp_tool_mode=_resolve_mcp_tool_mode,
        )
