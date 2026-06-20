"""Shared web-session contract helpers."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession


def canonical_web_external_conv_id(user: Any) -> str:
    username = str(getattr(user, "username", "") or "").strip()
    if username:
        return f"web_{username}"
    user_id = getattr(user, "id", None)
    return f"web_{user_id}" if user_id else ""


def build_web_delivery_target(user: Any, *, session_id: str | None = None) -> dict[str, str]:
    username = str(getattr(user, "username", "") or "").strip()
    user_label = str(getattr(user, "display_name", "") or username).strip() or username
    target = {
        "channel": "web",
        "username": username,
        "user_label": user_label,
    }
    if session_id:
        target["session_id"] = session_id
    return target


async def apply_web_session_contract(
    db: AsyncSession,
    *,
    session: Any,
    agent_id: uuid.UUID,
    user: Any,
) -> dict[str, str]:
    """Attach canonical web delivery metadata to a ChatSession-like object."""

    session_id = str(getattr(session, "id", "") or "").strip() or None
    target = build_web_delivery_target(user, session_id=session_id)
    session.source_channel = "web"
    session.delivery_target_json = target
    session.session_kind = "human_chat"
    session.actor_type = "user"
    session.runtime_source = "web_chat"
    session.visibility_scope = "direct_user"
    session.listed_surface = "chat"

    desired_conv_id = canonical_web_external_conv_id(user)
    current_conv_id = str(getattr(session, "external_conv_id", "") or "").strip()
    can_rebind = not current_conv_id or current_conv_id.startswith("web_")
    if desired_conv_id and can_rebind:
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == agent_id,
                ChatSession.external_conv_id == desired_conv_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing is None or getattr(existing, "id", None) == getattr(session, "id", None):
            session.external_conv_id = desired_conv_id

    return target
