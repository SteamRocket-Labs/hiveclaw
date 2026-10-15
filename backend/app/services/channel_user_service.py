"""Unified inbound Feishu user resolution."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_session import ChatSession
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import AgentRelationship, OrgMember
from app.models.user import User


class _SubjectRejected(Exception):
    """A supplied subject contradicts another account, or the provider cannot prove it."""


class ChannelUserService:
    """Resolve platform users from Feishu sender identifiers."""

    async def resolve_feishu_delivery_target_by_name(
        self,
        db: AsyncSession,
        *,
        agent_id: uuid.UUID,
        tenant_id: uuid.UUID | None,
        member_name: str,
    ) -> tuple[str, str] | None:
        """Resolve the best outbound Feishu identity for a named recipient.

        Preference order:
        1. Existing Feishu chat session with this agent (best signal for app-scoped open_id).
        2. Agent relationship / org member record.
        3. Tenant user record.
        """
        normalized_name = (member_name or "").strip()
        if not normalized_name:
            return None

        result = await db.execute(
            select(ChatSession.external_conv_id, User.display_name)
            .join(User, User.id == ChatSession.user_id)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.source_channel == "feishu",
                User.display_name == normalized_name,
                ChatSession.external_conv_id.is_not(None),
            )
            .order_by(ChatSession.last_message_at.desc().nullslast(), ChatSession.created_at.desc())
            .limit(5)
        )
        for row in result.all():
            external_conv_id = getattr(row, "external_conv_id", None)
            if not external_conv_id or not str(external_conv_id).startswith("feishu_p2p_"):
                continue
            identifier = str(external_conv_id)[len("feishu_p2p_") :]
            if not identifier:
                continue
            return identifier, ("open_id" if identifier.startswith("ou_") else "user_id")

        result = await db.execute(
            select(OrgMember)
            .join(AgentRelationship, AgentRelationship.member_id == OrgMember.id)
            .where(
                AgentRelationship.agent_id == agent_id,
                OrgMember.name == normalized_name,
            )
            .limit(1)
        )
        member = result.scalar_one_or_none()
        if member:
            provider_user_id = (member.external_id or member.feishu_user_id or "").strip()
            provider_open_id = (member.open_id or member.feishu_open_id or "").strip()
            if provider_user_id:
                return provider_user_id, "user_id"
            if provider_open_id:
                return provider_open_id, "open_id"

        org_query = select(OrgMember).where(OrgMember.name == normalized_name)
        if tenant_id is not None:
            org_query = org_query.where(OrgMember.tenant_id == tenant_id)
        result = await db.execute(org_query.limit(1))
        member = result.scalar_one_or_none()
        if member:
            provider_user_id = (member.external_id or member.feishu_user_id or "").strip()
            provider_open_id = (member.open_id or member.feishu_open_id or "").strip()
            if provider_user_id:
                return provider_user_id, "user_id"
            if provider_open_id:
                return provider_open_id, "open_id"

        user_query = select(User).where(User.display_name == normalized_name)
        if tenant_id is not None:
            user_query = user_query.where(User.tenant_id == tenant_id)
        result = await db.execute(user_query.limit(1))
        user = result.scalar_one_or_none()
        if user:
            if user.feishu_user_id:
                return user.feishu_user_id, "user_id"
            if user.feishu_open_id:
                return user.feishu_open_id, "open_id"

        return None

    async def resolve_feishu_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID | None,
        provider_user_id: str | None = None,
        provider_open_id: str | None = None,
        email: str | None = None,
        provider_union_id: str | None = None,
    ) -> User | None:
        """Resolve every supplied Feishu subject to the same active account.

        ``email`` stays in the signature for existing callers and is not a match.
        A subject bound to a different account blocks the others.
        """

        del email
        found: list[User | None] = []
        try:
            if provider_user_id:
                found.append(await self._find_by_provider_user_id(db, tenant_id, provider_user_id))
            if provider_open_id:
                found.append(await self._find_by_open_id(db, tenant_id, provider_open_id))
            if provider_union_id:
                found.append(await self._find_by_union_id(db, tenant_id, provider_union_id))
        except _SubjectRejected:
            return None
        accounts = [user for user in found if user is not None]
        if not accounts:
            return None
        if len({user.id for user in accounts}) != 1:
            return None
        user = accounts[0]
        if getattr(user, "is_active", True) is not True:
            return None
        account_tenant = getattr(user, "tenant_id", tenant_id)
        if tenant_id is None:
            if account_tenant is not None:
                return None
        elif account_tenant != tenant_id:
            return None
        return user

    async def get_feishu_delivery_target(
        self,
        db: AsyncSession,
        *,
        user: User,
    ) -> tuple[str, str] | None:
        """Return the best Feishu receive target for outbound delivery."""
        provider = await self._get_tenant_provider(db, user.tenant_id)
        if provider:
            result = await db.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider_id == provider.id,
                    ExternalIdentity.user_id == user.id,
                )
            )
            identity = result.scalar_one_or_none()
            if identity:
                if identity.provider_user_id:
                    return identity.provider_user_id, "user_id"
                if identity.provider_open_id:
                    return identity.provider_open_id, "open_id"

        if user.feishu_user_id:
            return user.feishu_user_id, "user_id"
        if user.feishu_open_id:
            return user.feishu_open_id, "open_id"
        return None

    async def _get_identity_provider(self, db: AsyncSession, tenant_id: uuid.UUID | None) -> IdentityProvider | None:
        """The provider that may prove an inbound subject. A tenant never borrows the global row."""

        query = select(IdentityProvider).where(IdentityProvider.provider_type == "feishu")
        if tenant_id is None:
            query = query.where(IdentityProvider.tenant_id.is_(None))
        else:
            query = query.where(IdentityProvider.tenant_id == tenant_id)
        rows = (await db.execute(query)).scalars().all()
        active = [row for row in rows if row.is_active is True]
        if active:
            return active[0]
        if rows:
            raise _SubjectRejected()
        return None

    def _tenant_user_clause(self, tenant_id: uuid.UUID | None):
        if tenant_id is None:
            return User.tenant_id.is_(None)
        return User.tenant_id == tenant_id

    async def _legacy_user(self, db: AsyncSession, tenant_id: uuid.UUID | None, column, value: str) -> User | None:
        rows = (
            await db.execute(select(User).where(column == value, self._tenant_user_clause(tenant_id)).limit(2))
        ).scalars().all()
        if len(rows) > 1:
            raise _SubjectRejected()
        return rows[0] if rows else None

    async def _external_user(self, db: AsyncSession, provider: IdentityProvider, column, value: str) -> User | None:
        rows = (
            await db.execute(
                select(User)
                .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
                .where(ExternalIdentity.provider_id == provider.id, column == value)
            )
        ).scalars().all()
        ids = {row.id: row for row in rows}
        if len(ids) > 1:
            raise _SubjectRejected()
        return next(iter(ids.values())) if ids else None

    async def _agree(self, external: User | None, legacy: User | None) -> User | None:
        if external is not None and legacy is not None and external.id != legacy.id:
            raise _SubjectRejected()
        return external or legacy

    async def _get_tenant_provider(self, db: AsyncSession, tenant_id: uuid.UUID | None) -> IdentityProvider | None:
        result = await db.execute(
            select(IdentityProvider)
            .where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id == tenant_id,
            )
            .limit(1)
        )
        provider = result.scalar_one_or_none()
        if provider:
            return provider

        result = await db.execute(
            select(IdentityProvider)
            .where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_by_provider_user_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        provider_user_id: str,
    ) -> User | None:
        provider = await self._get_identity_provider(db, tenant_id)
        external = None
        if provider is not None:
            external = await self._external_user(db, provider, ExternalIdentity.provider_user_id, provider_user_id)
        legacy = await self._legacy_user(db, tenant_id, User.feishu_user_id, provider_user_id)
        return await self._agree(external, legacy)

    async def _find_by_open_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        provider_open_id: str,
    ) -> User | None:
        provider = await self._get_identity_provider(db, tenant_id)
        external = None
        if provider is not None:
            external = await self._external_user(db, provider, ExternalIdentity.provider_open_id, provider_open_id)
        legacy = await self._legacy_user(db, tenant_id, User.feishu_open_id, provider_open_id)
        return await self._agree(external, legacy)

    async def _find_by_union_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        provider_union_id: str,
    ) -> User | None:
        provider = await self._get_identity_provider(db, tenant_id)
        external = None
        if provider is not None:
            external = await self._external_user(db, provider, ExternalIdentity.provider_union_id, provider_union_id)
        legacy = await self._legacy_user(db, tenant_id, User.feishu_union_id, provider_union_id)
        return await self._agree(external, legacy)


channel_user_service = ChannelUserService()
