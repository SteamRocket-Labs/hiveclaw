"""Resolve governance context and dependencies for tool execution."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.policy import write_audit_event
from app.database import async_session, tenant_scoped_session
from app.models.agent import Agent
from app.services.approval_service import approval_service
from app.services.capability_gate import check_capability
from app.tools.governance import GovernanceDependencies, ToolGovernanceContext
from app.tools.hook_governance import (
    PRE_TOOL_USE_EVENTS,
    GovernanceHookSpec,
    HookVerdict,
    spec_from_registration,
)
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

    @staticmethod
    def _governance_hooks_disabled() -> bool:
        return os.environ.get("HIVE_DISABLE_ALL_GOVERNANCE_HOOKS", "").strip() == "1"

    @staticmethod
    def _managed_hooks_only() -> bool:
        return os.environ.get("HIVE_ALLOW_MANAGED_HOOKS_ONLY", "").strip() == "1"

    def __init__(self, *, lookup_cache_ttl_seconds: float = _GOVERNANCE_LOOKUP_CACHE_TTL_SECONDS) -> None:
        self._security_zone_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)
        self._capability_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)
        self._mcp_tool_mode_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)
        self._governance_hooks_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)
        self._guard_policy_cache = _TtlSingleFlightCache(ttl_seconds=lookup_cache_ttl_seconds)

    def clear_lookup_cache(self) -> None:
        self._security_zone_cache.clear()
        self._capability_cache.clear()
        self._mcp_tool_mode_cache.clear()
        self._governance_hooks_cache.clear()
        self._guard_policy_cache.clear()

    async def build_context(
        self,
        *,
        runtime_context: ToolExecutionContext,
        tool_name: str,
        arguments: dict,
        tool_call_id: str | None = None,
        delegation_token: Any | None = None,
    ) -> ToolGovernanceContext:
        from app.services.approval_ticket import build_approval_execution_envelope

        execution_envelope = build_approval_execution_envelope(
            context=runtime_context,
            tool_call_id=str(tool_call_id or ""),
            emit_runtime_hooks=runtime_context.emit_runtime_hooks,
            plan_mode_interactive_available=runtime_context.plan_mode_interactive_available,
            plan_mode_unattended_available=runtime_context.plan_mode_unattended_available,
        )
        return ToolGovernanceContext(
            agent_id=runtime_context.agent_id,
            user_id=runtime_context.user_id,
            tenant_id=runtime_context.tenant_id,
            tool_name=tool_name,
            arguments=arguments,
            session_id=runtime_context.session_id,
            tool_call_id=tool_call_id,
            decision_id=(
                str(runtime_context.approval_decision.decision_id)
                if runtime_context.approval_decision is not None
                else (f"decision:{tool_call_id}" if tool_call_id else None)
            ),
            approval_id=(
                str(runtime_context.approval_decision.approval_id)
                if runtime_context.approval_decision is not None
                else None
            ),
            delegation_token=delegation_token,
            permission_profile=runtime_context.permission_profile,
            turn_id=runtime_context.turn_id,
            runtime_task_id=runtime_context.runtime_task_id,
            budget_run_id=runtime_context.budget_run_id,
            origin_channel=runtime_context.origin_channel,
            round_state=dict(runtime_context.round_state or {}),
            t0_refs=tuple(runtime_context.t0_refs or ()),
            workspace=str(runtime_context.workspace),
            execution_envelope=execution_envelope,
            approval_decision=runtime_context.approval_decision,
        )

    def build_dependencies(self) -> GovernanceDependencies:
        async def _resolve_security_zone(agent_id: uuid.UUID) -> str:
            async def _load() -> str:
                try:
                    from app.services.tenant_resolver import resolve_tenant_for_agent

                    tenant_id = await resolve_tenant_for_agent(
                        agent_id,
                        session_factory=async_session,
                    )
                    if tenant_id is None:
                        logger.warning(
                            "[Governance] Agent %s has no tenant — defaulting to 'restricted'",
                            agent_id,
                        )
                        return "restricted"
                    async with tenant_scoped_session(
                        tenant_id,
                        session_factory=async_session,
                        require_tenant=True,
                        source="tool_governance_security_zone",
                    ) as db:
                        result = await db.execute(
                            select(Agent).where(
                                Agent.id == agent_id,
                                Agent.tenant_id == tenant_id,
                            )
                        )
                        agent = result.scalar_one_or_none()
                        zone = getattr(agent, "security_zone", None)
                        if not zone:
                            logger.warning(
                                "[Governance] Agent %s has no security_zone set — defaulting to 'restricted'",
                                agent_id,
                            )
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
            decision_id: str | None = None,
            execution_envelope: dict[str, Any] | None = None,
        ) -> dict:
            from app.services.approval_ticket import build_live_approval_policy_snapshot
            from app.services.tenant_resolver import resolve_tenant_for_agent

            tenant_id = await resolve_tenant_for_agent(agent_id)
            if tenant_id is None:
                return {"allowed": False, "message": "Agent tenant not found"}
            async with tenant_scoped_session(tenant_id) as db:
                result = await db.execute(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id))
                agent = result.scalar_one_or_none()
                if not agent:
                    return {"allowed": False, "message": "Agent not found"}
                origin_type = approval_origin_type or ("agent_session" if session_id else "approval_request")
                policy_snapshot = await build_live_approval_policy_snapshot(
                    db=db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                )
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
                        "decision_id": decision_id,
                        "policy_snapshot": policy_snapshot,
                        "execution_envelope": execution_envelope,
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
                from app.services.tenant_resolver import resolve_tenant_for_agent

                tenant_id = await resolve_tenant_for_agent(
                    agent_id,
                    session_factory=async_session,
                )
                if tenant_id is None:
                    return None
                async with tenant_scoped_session(
                    tenant_id,
                    session_factory=async_session,
                    require_tenant=True,
                    source="tool_governance_mcp_mode",
                ) as db:
                    result = await db.execute(
                        select(Tool)
                        .join(AgentTool, AgentTool.tool_id == Tool.id)
                        .where(
                            AgentTool.agent_id == agent_id,
                            AgentTool.enabled.is_(True),
                            Tool.name == target,
                            Tool.type == "mcp",
                            Tool.enabled.is_(True),
                            Tool.tenant_id == tenant_id,
                        )
                    )
                    tool = result.scalar_one_or_none()
                    if tool is None:
                        return None
                    return await resolve_agent_mcp_tool_mode(db, agent_id, tool)

            return await self._mcp_tool_mode_cache.get((agent_id, target), _load)

        async def _load_governance_hooks(
            tenant_id: str | None,
            agent_id: uuid.UUID,
            _tool_name: str,
        ) -> list[GovernanceHookSpec]:
            # §1.5 fast/slow-lane hook specs. Loading is tenant-scoped and TTL
            # cached; matcher filtering happens inside the governance pipeline.
            if self._governance_hooks_disabled() or not tenant_id:
                return []

            async def _load() -> list[GovernanceHookSpec]:
                from app.models.external_capability import ExternalExtensionHookRegistration

                tenant_uuid = uuid.UUID(str(tenant_id))
                async with tenant_scoped_session(tenant_uuid) as db:
                    result = await db.execute(
                        select(ExternalExtensionHookRegistration).where(
                            ExternalExtensionHookRegistration.tenant_id == tenant_uuid,
                            ExternalExtensionHookRegistration.status == "approved",
                            ExternalExtensionHookRegistration.event.in_(tuple(PRE_TOOL_USE_EVENTS)),
                        )
                    )
                    rows = list(result.scalars().all())
                specs: list[GovernanceHookSpec] = []
                for row in rows:
                    spec = spec_from_registration(row)
                    if spec is None:
                        # Config-parse failure: management-plane problem, surfaced
                        # through audit — runtime failures are what fail closed.
                        logger.warning(
                            "[Governance] Skipping malformed governance hook registration %s (tenant %s)",
                            getattr(row, "qualified_name", getattr(row, "id", "?")),
                            tenant_id,
                        )
                        await _write_audit_event(
                            event_type="governance_hook.malformed",
                            severity="warn",
                            actor_type="system",
                            actor_id=agent_id,
                            tenant_id=tenant_uuid,
                            action="governance_hook_skipped",
                            resource_type="hook_registration",
                            resource_id=getattr(row, "id", None),
                            details={"qualified_name": getattr(row, "qualified_name", None)},
                        )
                        continue
                    specs.append(spec)
                if self._managed_hooks_only():
                    specs = [spec for spec in specs if spec.layer == "managed"]
                return specs

            return await self._governance_hooks_cache.get(("governance_hooks", str(tenant_id)), _load)

        async def _run_command_hook(spec: GovernanceHookSpec, payload: dict[str, Any]) -> HookVerdict:
            return await run_sandboxed_governance_hook(spec, payload)

        async def _load_guard_policy(
            tenant_id: uuid.UUID,
            _agent_id: uuid.UUID,
            _tool_name: str,
        ) -> dict[str, Any]:
            async def _load() -> dict[str, Any]:
                from app.models.guard_policy import GuardPolicy

                async with tenant_scoped_session(tenant_id) as db:
                    result = await db.execute(select(GuardPolicy).where(GuardPolicy.tenant_id == tenant_id))
                    policy = result.scalar_one_or_none()
                if policy is None:
                    return {"version": 0, "zone_guard": {}, "egress_guard": {}}
                return {
                    "version": int(policy.version),
                    "zone_guard": dict(policy.zone_guard or {}),
                    "egress_guard": dict(policy.egress_guard or {}),
                }

            return await self._guard_policy_cache.get(("guard_policy", tenant_id), _load)

        return GovernanceDependencies(
            resolve_security_zone=_resolve_security_zone,
            check_capability=_check_capability,
            write_audit_event=_write_audit_event,
            request_approval=_request_approval,
            resolve_mcp_tool_mode=_resolve_mcp_tool_mode,
            load_governance_hooks=_load_governance_hooks,
            run_command_hook=_run_command_hook,
            load_guard_policy=_load_guard_policy,
        )


async def run_sandboxed_governance_hook(spec: GovernanceHookSpec, payload: dict[str, Any]) -> HookVerdict:
    """Slow lane (§1.4): run one command governance hook in the code-execution
    sandbox and translate the outcome into a fail-closed verdict (D1/D2).

    Protocol (documented for hook authors):
    - stdin receives the JSON payload (tool_name, tool_args, agent/session ids);
    - exit 0 with a JSON object on stdout: ``{"decision": "deny"|"ask"|"allow",
      "reason": "..."}`` — the verdict; exit 0 without JSON = no opinion;
    - exit 2 = deny (CC blocking exit-code parity), stdout/stderr as reason;
    - any other exit code, timeout, or provider error = deny (fail-closed).
    """
    from app.services.code_execution.service import execute_agent_command
    from app.tools.workspace import ensure_workspace

    agent_id_raw = payload.get("agent_id")
    tenant_id_raw = payload.get("tenant_id")
    try:
        work_dir = await ensure_workspace(uuid.UUID(str(agent_id_raw)), str(tenant_id_raw) if tenant_id_raw else None)
    except Exception as exc:  # noqa: BLE001 - fail-closed: no workspace, no verdict
        return HookVerdict(
            decision="deny",
            reason=f"governance hook workspace unavailable ({type(exc).__name__})",
            hook_key=spec.key,
            layer=spec.layer,
            source="failure",
        )

    payload_dir = Path(work_dir) / ".hive" / "hook_payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_path = payload_dir / f"{uuid.uuid4().hex}.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    command = f"{spec.command} < {shlex.quote(str(payload_path))}"
    try:
        result = await execute_agent_command(
            ["bash", "-lc", command],
            work_dir=Path(work_dir),
            env={},
            timeout=spec.timeout_seconds,
            runtime="governance_hook",
            network_policy="deny",
        )
    except Exception as exc:  # noqa: BLE001 - D1 fail-closed
        return HookVerdict(
            decision="deny",
            reason=f"governance hook execution failed ({type(exc).__name__})",
            hook_key=spec.key,
            layer=spec.layer,
            source="failure",
        )
    finally:
        try:
            payload_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("[Governance] Failed to remove hook payload %s: %s", payload_path, exc)

    output_text = "\n".join(part for part in ((result.stdout or "").strip(), (result.stderr or "").strip()) if part)
    if result.error or result.timed_out:
        return HookVerdict(
            decision="deny",
            reason=result.error or f"governance hook '{spec.key}' timed out",
            hook_key=spec.key,
            layer=spec.layer,
            source="failure",
        )
    if result.exit_code == 2:
        return HookVerdict(
            decision="deny",
            reason=output_text or "governance hook returned blocking exit code 2",
            hook_key=spec.key,
            layer=spec.layer,
            source="command",
        )
    if result.exit_code != 0:
        return HookVerdict(
            decision="deny",
            reason=output_text or f"governance hook '{spec.key}' exited with code {result.exit_code}",
            hook_key=spec.key,
            layer=spec.layer,
            source="failure",
        )
    verdict_payload: dict[str, Any] | None = None
    stripped = (result.stdout or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                verdict_payload = parsed
        except json.JSONDecodeError:
            # exit 0 with malformed JSON: the hook tried to speak the protocol
            # and failed — fail closed rather than guessing (D1).
            return HookVerdict(
                decision="deny",
                reason=f"governance hook '{spec.key}' emitted malformed JSON",
                hook_key=spec.key,
                layer=spec.layer,
                source="failure",
            )
    if verdict_payload is None:
        return HookVerdict(
            decision="no_opinion",
            reason=output_text,
            hook_key=spec.key,
            layer=spec.layer,
            source="command",
        )
    decision = str(verdict_payload.get("decision") or "").strip().lower()
    reason = str(verdict_payload.get("reason") or output_text or spec.reason)
    if decision not in {"deny", "ask", "allow"}:
        return HookVerdict(
            decision="deny",
            reason=f"governance hook '{spec.key}' returned unknown decision {decision!r}",
            hook_key=spec.key,
            layer=spec.layer,
            source="failure",
        )
    return HookVerdict(
        decision=decision,
        reason=reason,
        hook_key=spec.key,
        layer=spec.layer,
        source="command",
    )
