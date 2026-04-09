"""Feishu identity maintenance helpers for duplicate users and chat sessions."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.identity import ExternalIdentity
from app.models.participant import Participant
from app.models.user import User


def build_feishu_p2p_conv_id(provider_user_id: str | None = None, provider_open_id: str | None = None) -> str | None:
    """Build the canonical Feishu P2P conversation id.

    `user_id` is tenant-stable and always wins over app-scoped `open_id`.
    """
    stable_id = (provider_user_id or "").strip() or (provider_open_id or "").strip()
    if not stable_id:
        return None
    return f"feishu_p2p_{stable_id}"


def list_legacy_feishu_conv_ids(provider_open_id: str | None, canonical_conv_id: str | None = None) -> list[str]:
    """Return legacy Feishu conv ids that should collapse into the canonical session."""
    open_id = (provider_open_id or "").strip()
    if not open_id:
        return []
    legacy = f"feishu_p2p_{open_id}"
    if canonical_conv_id and legacy == canonical_conv_id:
        return []
    return [legacy]


def choose_canonical_feishu_user(candidates: Sequence[User]) -> User:
    """Pick the canonical user for a duplicate Feishu identity cluster."""

    def _score(user: User) -> tuple[int, int, float, str]:
        has_real_email = int(bool(user.email and not user.email.endswith("@feishu.local")))
        has_stable_user_id = int(bool(user.feishu_user_id))
        created_at = getattr(user, "created_at", None) or datetime.max
        # Higher score wins; earlier create time wins on tie.
        return (
            has_stable_user_id,
            has_real_email,
            -created_at.timestamp(),
            str(getattr(user, "id", "")),
        )

    return max(candidates, key=_score)


async def _load_user_participant(db: AsyncSession, user_id: uuid.UUID) -> Participant | None:
    result = await db.execute(
        select(Participant).where(
            Participant.type == "user",
            Participant.ref_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def _merge_participants(db: AsyncSession, primary_user: User, duplicate_user: User) -> None:
    primary_participant = await _load_user_participant(db, primary_user.id)
    duplicate_participant = await _load_user_participant(db, duplicate_user.id)

    if not duplicate_participant:
        return

    if primary_participant:
        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.participant_id == duplicate_participant.id)
            .values(participant_id=primary_participant.id)
        )
        await db.execute(
            update(ChatSession)
            .where(ChatSession.participant_id == duplicate_participant.id)
            .values(participant_id=primary_participant.id)
        )
        await db.delete(duplicate_participant)
        return

    duplicate_participant.ref_id = primary_user.id
    duplicate_participant.display_name = primary_user.display_name or duplicate_participant.display_name
    duplicate_participant.avatar_url = primary_user.avatar_url or duplicate_participant.avatar_url


def _copy_missing_user_fields(primary_user: User, duplicate_user: User) -> None:
    if duplicate_user.email and not duplicate_user.email.endswith("@feishu.local"):
        if not primary_user.email or primary_user.email.endswith("@feishu.local"):
            primary_user.email = duplicate_user.email
    if duplicate_user.display_name and not primary_user.display_name:
        primary_user.display_name = duplicate_user.display_name
    if duplicate_user.avatar_url and not primary_user.avatar_url:
        primary_user.avatar_url = duplicate_user.avatar_url
    if duplicate_user.feishu_open_id and not primary_user.feishu_open_id:
        primary_user.feishu_open_id = duplicate_user.feishu_open_id
    if duplicate_user.feishu_union_id and not primary_user.feishu_union_id:
        primary_user.feishu_union_id = duplicate_user.feishu_union_id
    if duplicate_user.feishu_user_id and not primary_user.feishu_user_id:
        primary_user.feishu_user_id = duplicate_user.feishu_user_id


async def _merge_user_record(db: AsyncSession, primary_user: User, duplicate_user: User) -> None:
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.user_id == duplicate_user.id)
        .values(user_id=primary_user.id)
    )
    await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == duplicate_user.id)
        .values(user_id=primary_user.id)
    )
    await db.execute(
        update(ExternalIdentity)
        .where(ExternalIdentity.user_id == duplicate_user.id)
        .values(user_id=primary_user.id)
    )
    await _merge_participants(db, primary_user, duplicate_user)
    _copy_missing_user_fields(primary_user, duplicate_user)
    await db.delete(duplicate_user)


async def merge_duplicate_feishu_users(db: AsyncSession) -> int:
    """Merge duplicate user rows that share the same stable Feishu user_id."""
    duplicate_groups = await db.execute(
        select(User.feishu_user_id)
        .where(User.feishu_user_id.is_not(None), User.feishu_user_id != "")
        .group_by(User.feishu_user_id)
        .having(func.count(User.id) > 1)
    )

    merged = 0
    for (feishu_user_id,) in duplicate_groups.all():
        users_result = await db.execute(
            select(User)
            .where(User.feishu_user_id == feishu_user_id)
            .order_by(User.created_at.asc())
        )
        users = list(users_result.scalars().all())
        if len(users) < 2:
            continue

        primary_user = choose_canonical_feishu_user(users)
        for duplicate_user in users:
            if duplicate_user.id == primary_user.id:
                continue
            await _merge_user_record(db, primary_user, duplicate_user)
            merged += 1

    await db.flush()
    return merged


async def normalize_feishu_chat_sessions(db: AsyncSession) -> int:
    """Canonicalize Feishu P2P sessions from open_id-based ids to user_id-based ids."""
    session_result = await db.execute(
        select(ChatSession, User)
        .join(User, User.id == ChatSession.user_id)
        .where(
            ChatSession.source_channel == "feishu",
            ChatSession.external_conv_id.like("feishu_p2p_%"),
            User.feishu_user_id.is_not(None),
            User.feishu_user_id != "",
        )
    )

    normalized = 0
    for session, user in session_result.all():
        canonical_conv_id = build_feishu_p2p_conv_id(user.feishu_user_id, user.feishu_open_id)
        if not canonical_conv_id or canonical_conv_id == session.external_conv_id:
            continue

        existing_result = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == session.agent_id,
                ChatSession.external_conv_id == canonical_conv_id,
            )
        )
        canonical_session = existing_result.scalar_one_or_none()

        if canonical_session and canonical_session.id != session.id:
            await db.execute(
                update(ChatMessage)
                .where(ChatMessage.conversation_id == str(session.id))
                .values(conversation_id=str(canonical_session.id), user_id=canonical_session.user_id)
            )
            if session.last_message_at and (
                canonical_session.last_message_at is None or session.last_message_at > canonical_session.last_message_at
            ):
                canonical_session.last_message_at = session.last_message_at
            if (not canonical_session.title or canonical_session.title == "New Session") and session.title:
                canonical_session.title = session.title
            await db.delete(session)
        else:
            session.external_conv_id = canonical_conv_id
            session.user_id = user.id

        normalized += 1

    await db.flush()
    return normalized


async def reconcile_feishu_identity_state(db: AsyncSession) -> dict[str, int]:
    """Run duplicate merge + session normalization in one pass."""
    merged_users = await merge_duplicate_feishu_users(db)
    normalized_sessions = await normalize_feishu_chat_sessions(db)
    return {
        "merged_users": merged_users,
        "normalized_sessions": normalized_sessions,
    }
