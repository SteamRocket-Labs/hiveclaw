"""Maintenance helpers for collapsing legacy session/message data onto the canonical trunk."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.services.session_service import find_or_create_agent_pair_session


def parse_legacy_gateway_conversation_id(conversation_id: str | None) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Parse a legacy `gw_agent_<source>_<target>` conversation id."""
    if not conversation_id or not conversation_id.startswith("gw_agent_"):
        return None

    raw_pair = conversation_id.removeprefix("gw_agent_")
    try:
        source_raw, target_raw = raw_pair.split("_", 1)
        return uuid.UUID(source_raw), uuid.UUID(target_raw)
    except (ValueError, AttributeError, TypeError):
        return None


async def normalize_legacy_gateway_conversations(db: AsyncSession) -> dict[str, int]:
    """Replay legacy gateway conversation ids through the canonical pair-session service."""
    result = await db.execute(
        select(ChatMessage.conversation_id).where(ChatMessage.conversation_id.like("gw_agent_%")).distinct()
    )
    legacy_conversation_ids = [value for value in result.scalars().all() if value]
    normalized_pairs = 0
    skipped_pairs = 0
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    for legacy_conversation_id in legacy_conversation_ids:
        parsed_pair = parse_legacy_gateway_conversation_id(legacy_conversation_id)
        if parsed_pair is None:
            skipped_pairs += 1
            continue

        canonical_pair = tuple(sorted(parsed_pair, key=str))
        if canonical_pair in seen_pairs:
            continue
        seen_pairs.add(canonical_pair)

        source_agent_id, target_agent_id = canonical_pair
        agent_result = await db.execute(select(Agent).where(Agent.id.in_(canonical_pair)))
        agents = {agent.id: agent for agent in agent_result.scalars().all()}
        source_agent = agents.get(source_agent_id)
        target_agent = agents.get(target_agent_id)
        if not source_agent or not target_agent:
            skipped_pairs += 1
            continue

        owner_user_id = source_agent.creator_id or target_agent.creator_id
        if owner_user_id is None:
            skipped_pairs += 1
            continue

        await find_or_create_agent_pair_session(
            db,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            owner_user_id=owner_user_id,
            source_name=source_agent.name,
            target_name=target_agent.name,
        )
        normalized_pairs += 1

    return {
        "legacy_conversation_count": len(legacy_conversation_ids),
        "normalized_pairs": normalized_pairs,
        "skipped_pairs": skipped_pairs,
    }
