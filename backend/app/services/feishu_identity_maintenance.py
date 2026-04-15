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
from app.session_identifiers import build_feishu_session_lookup_ids
from app.services.channel_session import find_or_create_channel_session


def _apply_feishu_session_runtime_updates(
    session: ChatSession,
    *,
    user_id: uuid.UUID,
    title: str | None = None,
    created_at: datetime | None = None,
    participant_id: uuid.UUID | None = None,
    delivery_target: dict | None = None,
) -> None:
    session.user_id = user_id
    if title and (not session.title or session.title == "New Session"):
        session.title = title
    if created_at and (
        getattr(session, "created_at", None) is None or created_at < session.created_at
    ):
        session.created_at = created_at
    if participant_id and not getattr(session, "participant_id", None):
        session.participant_id = participant_id
    if delivery_target:
        session.delivery_target_json = delivery_target


async def _merge_legacy_feishu_session_into_canonical(
    db: AsyncSession,
    *,
    canonical_session: ChatSession,
    legacy_session: ChatSession,
    user_id: uuid.UUID,
) -> None:
    await db.execute(
        update(ChatMessage)
        .where(ChatMessage.conversation_id == str(legacy_session.id))
        .values(conversation_id=str(canonical_session.id), user_id=user_id)
    )
    if legacy_session.last_message_at and (
        canonical_session.last_message_at is None or legacy_session.last_message_at > canonical_session.last_message_at
    ):
        canonical_session.last_message_at = legacy_session.last_message_at
    _apply_feishu_session_runtime_updates(
        canonical_session,
        user_id=user_id,
        title=legacy_session.title,
        created_at=getattr(legacy_session, "created_at", None),
        participant_id=getattr(legacy_session, "participant_id", None),
        delivery_target=getattr(legacy_session, "delivery_target_json", None),
    )
    await db.delete(legacy_session)
    await db.flush()


async def _promote_legacy_feishu_alias_session(
    db: AsyncSession,
    *,
    legacy_session: ChatSession,
    external_conv_id: str,
    user_id: uuid.UUID,
    first_message_title: str,
    delivery_target: dict | None,
) -> ChatSession:
    legacy_session.external_conv_id = external_conv_id
    _apply_feishu_session_runtime_updates(
        legacy_session,
        user_id=user_id,
        title=first_message_title[:40],
        delivery_target=delivery_target,
    )
    await db.flush()
    return legacy_session


async def find_or_create_feishu_chat_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    provider_user_id: str | None,
    provider_open_id: str | None,
    first_message_title: str,
    delivery_target: dict | None = None,
    chat_type: str = "p2p",
    chat_id: str | None = None,
) -> ChatSession:
    """Resolve Feishu canonical/legacy ids before entering the generic session factory."""
    external_conv_id, legacy_external_conv_ids = build_feishu_session_lookup_ids(
        provider_user_id=provider_user_id,
        provider_open_id=provider_open_id,
        chat_type=chat_type,
        chat_id=chat_id,
    )

    result = await db.execute(
        select(ChatSession).where(
            ChatSession.agent_id == agent_id,
            ChatSession.external_conv_id == external_conv_id,
        )
    )
    session = result.scalar_one_or_none()

    if session is not None:
        for legacy_conv_id in legacy_external_conv_ids:
            legacy_result = await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.external_conv_id == legacy_conv_id,
                )
            )
            legacy_session = legacy_result.scalar_one_or_none()
            if not legacy_session or legacy_session.id == session.id:
                continue

            await _merge_legacy_feishu_session_into_canonical(
                db,
                canonical_session=session,
                legacy_session=legacy_session,
                user_id=user_id,
            )

        _apply_feishu_session_runtime_updates(
            session,
            user_id=user_id,
            delivery_target=delivery_target,
        )
        return session

    for legacy_conv_id in legacy_external_conv_ids:
        legacy_result = await db.execute(
            select(ChatSession).where(
                ChatSession.agent_id == agent_id,
                ChatSession.external_conv_id == legacy_conv_id,
            )
        )
        legacy_session = legacy_result.scalar_one_or_none()
        if legacy_session:
            return await _promote_legacy_feishu_alias_session(
                db,
                legacy_session=legacy_session,
                external_conv_id=external_conv_id,
                user_id=user_id,
                first_message_title=first_message_title,
                delivery_target=delivery_target,
            )

    return await find_or_create_channel_session(
        db=db,
        agent_id=agent_id,
        user_id=user_id,
        external_conv_id=external_conv_id,
        source_channel="feishu",
        first_message_title=first_message_title,
        delivery_target=delivery_target,
    )


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
