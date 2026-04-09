"""Unified inbound Feishu user resolution."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.user import User


class ChannelUserService:
    """Resolve platform users from Feishu sender identifiers."""

    async def resolve_feishu_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        provider_user_id: str | None = None,
        provider_open_id: str | None = None,
        email: str | None = None,
    ) -> User | None:
        """Resolve by stable user_id, then open_id, then exact tenant email."""
        if provider_user_id:
            user = await self._find_by_provider_user_id(db, tenant_id, provider_user_id)
            if user:
                return user

        if provider_open_id:
            user = await self._find_by_open_id(db, tenant_id, provider_open_id)
            if user:
                return user

        if email:
            return await self._find_by_email(db, tenant_id, email)

        return None

    async def _get_tenant_provider(self, db: AsyncSession, tenant_id: uuid.UUID) -> IdentityProvider | None:
        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id == tenant_id,
            )
        )
        provider = result.scalar_one_or_none()
        if provider:
            return provider

        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _find_by_provider_user_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        provider_user_id: str,
    ) -> User | None:
        provider = await self._get_tenant_provider(db, tenant_id)
        if provider:
            result = await db.execute(
                select(User)
                .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
                .where(
                    ExternalIdentity.provider_id == provider.id,
                    ExternalIdentity.provider_user_id == provider_user_id,
                )
            )
            user = result.scalar_one_or_none()
            if user:
                return user

        result = await db.execute(
            select(User).where(User.feishu_user_id == provider_user_id, User.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def _find_by_open_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        provider_open_id: str,
    ) -> User | None:
        provider = await self._get_tenant_provider(db, tenant_id)
        if provider:
            result = await db.execute(
                select(User)
                .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
                .where(
                    ExternalIdentity.provider_id == provider.id,
                    ExternalIdentity.provider_open_id == provider_open_id,
                )
            )
            user = result.scalar_one_or_none()
            if user:
                return user

        result = await db.execute(
            select(User).where(User.feishu_open_id == provider_open_id, User.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def _find_by_email(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID,
        email: str,
    ) -> User | None:
        result = await db.execute(
            select(User).where(User.email == email, User.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()


channel_user_service = ChannelUserService()

