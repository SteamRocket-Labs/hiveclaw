"""Unified inbound Feishu user resolution."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.user import User
from app.services.auth_provider import feishu_auth_provider


class ChannelUserService:
    """Resolve platform users from Feishu sender identifiers."""

    async def resolve_feishu_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID | None,
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

    async def resolve_or_create_feishu_user(
        self,
        db: AsyncSession,
        *,
        tenant_id: uuid.UUID | None,
        profile: dict,
    ) -> User:
        """Resolve by external identity first, then create/bind via provider-driven auth."""
        user = await self.resolve_feishu_user(
            db,
            tenant_id=tenant_id,
            provider_user_id=profile.get("user_id"),
            provider_open_id=profile.get("open_id"),
            email=profile.get("email"),
        )
        provider = await feishu_auth_provider._ensure_provider(db, tenant_id)

        if user is None:
            return await feishu_auth_provider._find_or_create_user(db, provider, profile, tenant_id)

        await feishu_auth_provider._upsert_external_identity(db, provider, user, profile)
        feishu_auth_provider._write_through_user_fields(user, profile)
        feishu_auth_provider._hydrate_user_profile(user, profile)
        await db.flush()
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

    async def _get_tenant_provider(self, db: AsyncSession, tenant_id: uuid.UUID | None) -> IdentityProvider | None:
        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id == tenant_id,
            ).limit(1)
        )
        provider = result.scalar_one_or_none()
        if provider:
            return provider

        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_type == "feishu",
                IdentityProvider.tenant_id.is_(None),
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_by_provider_user_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
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
            select(User).where(
                User.feishu_user_id == provider_user_id,
                User.tenant_id == tenant_id,
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_by_open_id(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
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
            select(User).where(
                User.feishu_open_id == provider_open_id,
                User.tenant_id == tenant_id,
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def _find_by_email(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        email: str,
    ) -> User | None:
        query = select(User).where(User.email == email)
        if tenant_id is not None:
            query = query.where(User.tenant_id == tenant_id)
        result = await db.execute(query.limit(1))
        return result.scalar_one_or_none()


channel_user_service = ChannelUserService()
