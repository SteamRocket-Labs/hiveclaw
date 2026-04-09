"""Enterprise information synchronization service.

Uses Redis Pub/Sub to notify online Agent containers when enterprise info changes.
Agents pull latest data based on their roles and write to local enterprise_info/ directory.
"""

import json
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import publish_event
from app.models.agent import Agent
from app.models.audit import EnterpriseInfo

settings = get_settings()

# Redis channel for enterprise info updates
ENTERPRISE_INFO_CHANNEL = "enterprise_info_updated"


class EnterpriseSyncService:
    """Synchronize enterprise information to all online Agent containers."""

    async def update_enterprise_info(
        self, db: AsyncSession, tenant_id: uuid.UUID, info_type: str, content: dict,
        visible_roles: list[str], updated_by: uuid.UUID
    ) -> EnterpriseInfo:
        """Update enterprise info in database and notify all agents."""
        result = await db.execute(
            select(EnterpriseInfo).where(
                EnterpriseInfo.tenant_id == tenant_id,
                EnterpriseInfo.info_type == info_type,
            )
        )
        info = result.scalar_one_or_none()

        if info:
            info.content = content
            info.visible_roles = visible_roles
            info.version += 1
            info.updated_by = updated_by
        else:
            info = EnterpriseInfo(
                tenant_id=tenant_id,
                info_type=info_type,
                content=content,
                visible_roles=visible_roles,
                updated_by=updated_by,
            )
            db.add(info)

        await db.flush()

        # Publish update event
        await publish_event(ENTERPRISE_INFO_CHANNEL, {
            "tenant_id": str(tenant_id),
            "info_type": info_type,
            "version": info.version,
            "visible_roles": visible_roles,
        })

        logger.info(f"Published enterprise_info update: {info_type} v{info.version}")
        return info

    async def sync_to_agent(self, db: AsyncSession, agent_id: uuid.UUID, agent_role: str = "") -> None:
        """Pull enterprise info from DB and write to agent's enterprise_info/ directory.

        Filters by visible_roles — if empty, all roles can see it.
        """
        agent_result = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
        agent_tenant_id = agent_result.scalar_one_or_none()
        agent_dir = Path(settings.AGENT_DATA_DIR) / str(agent_id) / "enterprise_info"
        agent_dir.mkdir(parents=True, exist_ok=True)

        result = await db.execute(
            select(EnterpriseInfo).where(EnterpriseInfo.tenant_id == agent_tenant_id)
        )
        all_info = result.scalars().all()

        for info in all_info:
            # Filter by role visibility
            if info.visible_roles and agent_role and agent_role not in info.visible_roles:
                continue

            file_path = agent_dir / f"{info.info_type}.json"
            file_path.write_text(json.dumps({
                "type": info.info_type,
                "version": info.version,
                "content": info.content,
            }, ensure_ascii=False, indent=2))

        logger.info(f"Synced enterprise info to agent {agent_id}")

    async def sync_to_all_agents(self, db: AsyncSession, tenant_id: uuid.UUID | None = None) -> int:
        """Sync enterprise info to all running agents. Returns count."""
        stmt = select(Agent).where(Agent.status == "running")
        if tenant_id:
            stmt = stmt.where(Agent.tenant_id == tenant_id)
        result = await db.execute(stmt)
        agents = result.scalars().all()

        for agent in agents:
            await self.sync_to_agent(db, agent.id, agent.role_description)

        logger.info(f"Synced enterprise info to {len(agents)} agents")
        return len(agents)


enterprise_sync_service = EnterpriseSyncService()
