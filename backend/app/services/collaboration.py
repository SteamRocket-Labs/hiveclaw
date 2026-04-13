"""Agent collaboration service — Agent-to-Agent communication."""

import json
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import AuditLog


class CollaborationService:
    """Enable digital employees to collaborate with each other.

    Collaboration patterns:
    1. Delegate — Agent A sends a task to Agent B
    2. Consult — Agent A asks Agent B a question and waits for response
    3. Notify — Agent A sends information to Agent B in a synchronous A2A round
    """

    async def delegate_task(
        self, db: AsyncSession, from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID, task_title: str, task_description: str
    ) -> dict:
        """Delegate work through the runtime async delegation path."""
        from app.services.agent_tool_domains.messaging import _delegate_to_agent_async

        from_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        from_agent = from_result.scalar_one_or_none()
        to_result = await db.execute(select(Agent).where(Agent.id == to_agent_id))
        to_agent = to_result.scalar_one_or_none()

        if not from_agent or not to_agent:
            raise ValueError("Agent not found")
        if to_agent.status in {"expired", "stopped", "archived"}:
            raise ValueError(f"Target agent '{to_agent.name}' is currently {to_agent.status}")

        task_message = task_title.strip()
        if task_description.strip():
            task_message = f"{task_message}\n\n{task_description.strip()}"

        raw_result = await _delegate_to_agent_async(
            from_agent_id,
            {
                "agent_name": to_agent.name,
                "target_agent_id": str(to_agent.id),
                "message": task_message,
            },
        )
        if raw_result.startswith(("❌", "⚠️")):
            raise ValueError(raw_result.lstrip("❌⚠️ ").strip())
        payload = json.loads(raw_result)

        db.add(AuditLog(
            agent_id=from_agent_id,
            action="collaboration:delegate",
            details={
                "from_agent": str(from_agent_id),
                "to_agent": str(to_agent_id),
                "task_title": task_title,
                "runtime_task_id": payload.get("task_id"),
                "trace_id": payload.get("trace_id"),
            },
        ))
        await db.flush()

        logger.info(f"Agent {from_agent.name} delegated task to {to_agent.name}: {task_title}")
        payload["from_agent"] = from_agent.name
        payload["to_agent"] = to_agent.name
        return payload

    async def list_collaborators(self, db: AsyncSession, agent_id: uuid.UUID) -> list[dict]:
        """List agents that can collaborate with the given agent.

        Returns agents from the same enterprise (same creator's org).
        """
        result = await db.execute(select(Agent).where(Agent.id == agent_id))
        agent = result.scalar_one_or_none()
        if not agent:
            return []

        # Find agents within the same tenant (tenant isolation)
        collaborators_result = await db.execute(
            select(Agent).where(
                Agent.id != agent_id,
                Agent.tenant_id == agent.tenant_id,
                Agent.status.in_(["running", "stopped"]),
            ).order_by(Agent.name)
        )
        agents = collaborators_result.scalars().all()

        return [
            {
                "id": str(a.id),
                "name": a.name,
                "role": a.role_description,
                "status": a.status,
            }
            for a in agents
        ]

    async def send_message_between_agents(
        self, db: AsyncSession, from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID, message: str, msg_type: str = "notify"
    ) -> dict:
        """Send an inter-agent message through the canonical runtime A2A path."""
        from app.services.agent_tool_domains.messaging import _send_message_to_agent

        from_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        from_agent = from_result.scalar_one_or_none()
        to_result = await db.execute(select(Agent).where(Agent.id == to_agent_id))
        to_agent = to_result.scalar_one_or_none()

        if not from_agent or not to_agent:
            raise ValueError("Agent not found")
        if to_agent.status in {"expired", "stopped", "archived"}:
            raise ValueError(f"Target agent '{to_agent.name}' is currently {to_agent.status}")

        raw_result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_name": to_agent.name,
                "target_agent_id": str(to_agent.id),
                "message": message,
                "msg_type": msg_type,
            },
        )
        if raw_result.startswith(("❌", "⚠️")):
            raise ValueError(raw_result.lstrip("❌⚠️ ").strip())

        db.add(AuditLog(
            agent_id=from_agent_id,
            action=f"collaboration:{msg_type}",
            details={
                "to_agent": str(to_agent_id),
                "message_preview": message[:100],
                "route": "runtime_agent_message",
                "result_preview": raw_result[:200],
            },
        ))
        await db.flush()

        logger.info("Collab message routed through runtime: %s -> %s", from_agent_id, to_agent_id)
        return {
            "status": "completed",
            "type": msg_type,
            "from_agent": from_agent.name,
            "to_agent": to_agent.name,
            "result": raw_result,
        }


collaboration_service = CollaborationService()
