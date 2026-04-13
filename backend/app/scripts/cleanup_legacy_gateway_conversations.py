"""Gateway legacy conversation maintenance script.

This script collapses old `gw_agent_<source>_<target>` message threads into the
canonical agent-pair session surface managed by `session_service`.

Usage:
  Docker:  docker exec hive-backend-1 python3 -m app.scripts.cleanup_legacy_gateway_conversations
  Source:  cd backend && python3 -m app.scripts.cleanup_legacy_gateway_conversations
"""

from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    # Import models so SQLAlchemy resolves metadata before async work starts.
    from app.models import (  # noqa: F401
        activity_log, agent, audit, channel_config, chat_session,
        gateway_message, identity, invitation_code, llm, notification, org,
        participant, plaza, skill, system_settings, task,
        tenant, tenant_setting, tool, trigger, user,
    )
    from app.database import async_session
    from app.db_legacy_gateway_conversation_migration import promote_legacy_gateway_conversations

    async with async_session() as db:
        connection = await db.connection()
        normalized_pairs = await connection.run_sync(promote_legacy_gateway_conversations)
        await db.commit()

    logger.info("Normalized agent pairs: %s", normalized_pairs)
    logger.info("Gateway conversation maintenance complete")


if __name__ == "__main__":
    asyncio.run(main())
