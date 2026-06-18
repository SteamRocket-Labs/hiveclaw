"""Agent collaboration service — Agent-to-Agent communication."""

import json
import uuid
from datetime import datetime, timezone

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
    3. Notify — Agent A sends information to Agent B (fire-and-forget)
    """

    async def delegate_task(
        self, db: AsyncSession, from_agent_id: uuid.UUID,
        to_agent_id: uuid.UUID, task_title: str, task_description: str,
        *,
        confirmed_plan_id: str | None = None,
        confirmed_plan_version: int | None = None,
        confirmed_plan_hash: str | None = None,
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
                "confirmed_plan_id": confirmed_plan_id,
                "confirmed_plan_version": confirmed_plan_version,
                "confirmed_plan_hash": confirmed_plan_hash,
            },
        )
        if raw_result.startswith(("❌", "⚠️")):
            raise ValueError(raw_result.lstrip("❌⚠️ ").strip())
        payload = json.loads(raw_result)

        db.add(AuditLog(
            agent_id=from_agent_id,
            tenant_id=from_agent.tenant_id,
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
        """Send an inter-agent message via Redis Streams event bus.

        Falls back to file-based inbox if Redis is unavailable.
        msg_type: 'notify' (fire-and-forget) or 'consult' (expects reply)
        """
        from_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        from_agent = from_result.scalar_one_or_none()

        # Resolve tenant_id for stream scoping
        to_result = await db.execute(select(Agent.tenant_id).where(Agent.id == to_agent_id))
        tenant_id = to_result.scalar_one_or_none()

        # Publish to Redis Streams event bus (durable, replayable)
        try:
            from app.core.event_bus import event_bus, collab_stream
            stream_key = collab_stream(str(tenant_id)) if tenant_id else "events:global:collab"
            await event_bus.publish(
                stream=stream_key,
                event_type=f"collab.{msg_type}",
                payload={
                    "from_agent_id": str(from_agent_id),
                    "from_agent_name": from_agent.name if from_agent else "Unknown",
                    "to_agent_id": str(to_agent_id),
                    "message": message[:2000],
                    "msg_type": msg_type,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                tenant_id=str(tenant_id) if tenant_id else None,
            )
            logger.info("Collab message published to event bus: %s -> %s", from_agent_id, to_agent_id)
        except Exception as e:
            # Fallback: write to file-based inbox if Redis is down
            logger.warning("Event bus publish failed, falling back to file inbox: %s", e)
            from pathlib import Path
            from app.config import get_settings
            settings = get_settings()

            inbox_dir = Path(settings.AGENT_DATA_DIR) / str(to_agent_id) / "workspace" / "inbox"
            inbox_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            msg_file = inbox_dir / f"{timestamp}_{str(from_agent_id)[:8]}.md"
            msg_file.write_text(
                f"# 来自 {from_agent.name if from_agent else 'Unknown'} 的消息\n"
                f"- 类型: {msg_type}\n"
                f"- 时间: {datetime.now(timezone.utc).isoformat()}\n\n"
                f"{message}\n"
            )

        db.add(AuditLog(
            agent_id=from_agent_id,
            tenant_id=tenant_id,
            action=f"collaboration:{msg_type}",
            details={"to_agent": str(to_agent_id), "message_preview": message[:100]},
        ))
        await db.flush()

        return {"status": "sent", "type": msg_type}


collaboration_service = CollaborationService()
