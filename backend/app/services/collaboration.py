"""Agent collaboration service — Agent-to-Agent communication."""

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.execution_context import ExecutionPrincipal
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model


class CollaborationService:
    """Enable digital employees to collaborate with each other.

    Collaboration patterns:
    1. Delegate — Agent A sends a task to Agent B
    2. Consult — Agent A asks Agent B a question and waits for response
    3. Notify — Agent A sends information to Agent B (fire-and-forget)
    """

    async def delegate_task(
        self,
        db: AsyncSession,
        from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID,
        task_title: str,
        task_description: str,
        *,
        principal: ExecutionPrincipal,
        confirmed_plan_id: str | None = None,
        confirmed_plan_version: int | None = None,
        confirmed_plan_hash: str | None = None,
        confirmed_plan_session_id: str | None = None,
        plan_authorization: dict | None = None,
    ) -> dict:
        """Delegate work through the runtime async delegation path."""
        from app.services.agent_tool_domains.messaging import _delegate_to_agent_async_outcome

        from_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        from_agent = from_result.scalar_one_or_none()
        to_result = await db.execute(select(Agent).where(Agent.id == to_agent_id))
        to_agent = to_result.scalar_one_or_none()

        if not from_agent or not to_agent:
            raise ValueError("Agent not found")
        principal.assert_scope(tenant_id=from_agent.tenant_id, source_agent_id=from_agent_id)
        if principal.requester_user_id is None:
            raise ValueError("A2A REST delegation requires an authenticated requester")
        if str(to_agent.tenant_id) != str(from_agent.tenant_id):
            raise ValueError("Cross-tenant A2A delegation is forbidden")
        if to_agent.status in {"expired", "stopped", "archived"}:
            raise ValueError(f"Target agent '{to_agent.name}' is currently {to_agent.status}")

        task_message = task_title.strip()
        if task_description.strip():
            task_message = f"{task_message}\n\n{task_description.strip()}"

        outcome = await _delegate_to_agent_async_outcome(
            from_agent_id,
            {
                "agent_name": to_agent.name,
                "target_agent_id": str(to_agent.id),
                "message": task_message,
                "confirmed_plan_id": confirmed_plan_id,
                "confirmed_plan_version": confirmed_plan_version,
                "confirmed_plan_hash": confirmed_plan_hash,
                "confirmed_plan_session_id": confirmed_plan_session_id,
                "plan_authorization": dict(plan_authorization or {}),
            },
            principal=principal,
        )
        if not outcome.ok:
            raise ValueError(outcome.message)
        payload = outcome.to_payload()

        db.add(
            AuditLog(
                user_id=principal.requester_user_id,
                agent_id=from_agent_id,
                tenant_id=from_agent.tenant_id,
                action="collaboration:delegate",
                details={
                    "from_agent": str(from_agent_id),
                    "to_agent": str(to_agent_id),
                    "task_title": task_title,
                    "runtime_task_id": payload.get("task_id"),
                    "trace_id": payload.get("trace_id"),
                    "plan_authorization": dict(plan_authorization or {}),
                    "execution_principal": principal.to_evidence(),
                    "outcome_status": outcome.status,
                },
            )
        )
        await db.flush()

        logger.info(f"Agent {from_agent.name} delegated task to {to_agent.name}: {task_title}")
        payload["from_agent"] = from_agent.name
        payload["to_agent"] = to_agent.name
        payload["child_session_id"] = payload.get("child_session_id") or payload.get("session_id")
        return payload

    async def list_collaborators(self, db: AsyncSession, agent_id: uuid.UUID) -> list[dict]:
        """List governed collaborators: same-owner, public, plus active A2A groups."""
        read_model = await build_a2a_collaboration_read_model(db, agent_id)
        collaborators = list(read_model.get("same_owner_agents") or [])
        collaborators.extend(read_model.get("public_agents") or [])
        for group in read_model.get("collaboration_groups") or []:
            for member in group.get("members") or []:
                collaborators.append(
                    {
                        "id": member.get("agent_id") or member.get("id"),
                        "name": member.get("name"),
                        "role": member.get("role_description", ""),
                        "status": member.get("status"),
                        "relation": "collaboration_group",
                        "group_id": group.get("group_id"),
                        "group_name": group.get("group_name"),
                    }
                )
        return collaborators

    async def send_message_between_agents(
        self,
        db: AsyncSession,
        from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID,
        message: str,
        msg_type: str = "notify",
        *,
        principal: ExecutionPrincipal,
    ) -> dict:
        """Send an inter-agent message through the governed session-backed A2A path."""
        from app.services.agent_tool_domains.messaging import (
            _delegate_to_agent_async_outcome,
            _send_message_to_agent_outcome,
        )

        from_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        from_agent = from_result.scalar_one_or_none()
        to_result = await db.execute(select(Agent).where(Agent.id == to_agent_id))
        to_agent = to_result.scalar_one_or_none()
        if not from_agent or not to_agent:
            raise ValueError("Agent not found")
        principal.assert_scope(tenant_id=from_agent.tenant_id, source_agent_id=from_agent_id)
        if principal.requester_user_id is None:
            raise ValueError("A2A REST messaging requires an authenticated requester")
        if str(to_agent.tenant_id) != str(from_agent.tenant_id):
            raise ValueError("Cross-tenant A2A messaging is forbidden")
        if msg_type not in {"consult", "notify"}:
            raise ValueError("msg_type must be consult or notify")

        if msg_type == "consult":
            outcome = await _send_message_to_agent_outcome(
                from_agent_id,
                {"target_agent_id": str(to_agent_id), "agent_name": to_agent.name, "message": message},
                principal=principal,
            )
        else:
            outcome = await _delegate_to_agent_async_outcome(
                from_agent_id,
                {"target_agent_id": str(to_agent_id), "agent_name": to_agent.name, "message": message},
                principal=principal,
            )
        if not outcome.ok:
            raise ValueError(outcome.message)
        payload = outcome.to_payload()
        payload["type"] = msg_type

        db.add(
            AuditLog(
                user_id=principal.requester_user_id,
                agent_id=from_agent_id,
                tenant_id=from_agent.tenant_id,
                action=f"collaboration:{msg_type}",
                details={
                    "to_agent": str(to_agent_id),
                    "message_preview": message[:100],
                    "session_id": payload.get("session_id") or payload.get("child_session_id"),
                    "runtime_task_id": payload.get("task_id"),
                    "execution_principal": principal.to_evidence(),
                    "outcome_status": outcome.status,
                },
            )
        )
        await db.flush()

        return payload


collaboration_service = CollaborationService()
