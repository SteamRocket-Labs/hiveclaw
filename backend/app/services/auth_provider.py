"""Provider-driven Feishu authentication and identity binding."""

from __future__ import annotations

import secrets
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import create_access_token, hash_password
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.participant import Participant
from app.models.tenant_setting import TenantSetting
from app.models.user import User

settings = get_settings()

FEISHU_TOKEN_URL_V2 = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"


def _get_runtime_settings():
    from app.config import get_settings as runtime_get_settings

    return runtime_get_settings()


def resolve_feishu_redirect_uri(settings_obj=None) -> str:
    """Return the OAuth callback URL used for both authorize and token exchange."""
    current_settings = settings_obj or _get_runtime_settings()
    explicit = (getattr(current_settings, "FEISHU_REDIRECT_URI", "") or "").strip()
    if explicit:
        return explicit

    public_base = (
        (getattr(current_settings, "PUBLIC_BASE_URL", "") or "")
        or (getattr(current_settings, "BASE_URL", "") or "")
    ).strip()
    if not public_base:
        return ""

    return f"{public_base.rstrip('/')}/api/auth/feishu/callback"


class FeishuAuthProvider:
    """Authenticate and bind Feishu users through external identities."""

    provider_type = "feishu"

    async def authenticate_with_code(
        self,
        db: AsyncSession,
        *,
        code: str,
        tenant_id: uuid.UUID | None = None,
    ) -> tuple[User, str]:
        provider = await self._ensure_provider(db, tenant_id)
        profile = await self._exchange_code_for_user(provider.config, code)
        user = await self._find_or_create_user(db, provider, profile, tenant_id)
        token = create_access_token(
            str(user.id),
            user.role,
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
        )
        await db.flush()
        return user, token

    async def bind_with_code(
        self,
        db: AsyncSession,
        *,
        user: User,
        code: str,
    ) -> User:
        provider = await self._ensure_provider(db, user.tenant_id)
        profile = await self._exchange_code_for_user(provider.config, code)
        await self._upsert_external_identity(db, provider, user, profile)
        self._write_through_user_fields(user, profile)
        await db.flush()
        return user

    async def get_oauth_config(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
    ) -> dict:
        """Resolve tenant-first Feishu OAuth config for authorize URLs."""
        return await self._load_provider_config(db, tenant_id)

    async def _ensure_provider(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
    ) -> IdentityProvider:
        config = await self._load_provider_config(db, tenant_id)
        result = await db.execute(
            select(IdentityProvider).where(
                IdentityProvider.provider_type == self.provider_type,
                IdentityProvider.tenant_id == tenant_id,
            ).limit(1)
        )
        provider = result.scalar_one_or_none()
        if provider:
            provider.name = "Feishu"
            provider.is_active = True
            provider.sso_login_enabled = True
            provider.config = config
            await db.flush()
            return provider

        provider = IdentityProvider(
            provider_type=self.provider_type,
            name="Feishu",
            is_active=True,
            sso_login_enabled=True,
            config=config,
            tenant_id=tenant_id,
        )
        db.add(provider)
        await db.flush()
        return provider

    async def _load_provider_config(self, db: AsyncSession, tenant_id: uuid.UUID | None) -> dict:
        current_settings = _get_runtime_settings()
        redirect_uri = resolve_feishu_redirect_uri(current_settings)

        if tenant_id:
            result = await db.execute(
                select(TenantSetting).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == "feishu_org_sync",
                )
            )
            setting = result.scalar_one_or_none()
            cfg = setting.value if setting and setting.value else {}
            if cfg.get("app_id") and cfg.get("app_secret"):
                if not redirect_uri:
                    raise RuntimeError("Feishu redirect URI not configured")
                return {
                    "app_id": cfg["app_id"],
                    "app_secret": cfg["app_secret"],
                    "redirect_uri": redirect_uri,
                    "source": "tenant_setting",
                }

        app_id = getattr(current_settings, "FEISHU_APP_ID", "") or ""
        app_secret = getattr(current_settings, "FEISHU_APP_SECRET", "") or ""
        if app_id and app_secret:
            if not redirect_uri:
                raise RuntimeError("Feishu redirect URI not configured")
            return {
                "app_id": app_id,
                "app_secret": app_secret,
                "redirect_uri": redirect_uri,
                "source": "global_env",
            }

        raise RuntimeError("Feishu OAuth not configured")

    async def _get_app_access_token(self, config: dict) -> str:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                FEISHU_APP_TOKEN_URL,
                json={"app_id": config["app_id"], "app_secret": config["app_secret"]},
            )
            resp.raise_for_status()
            data = resp.json()
        return data.get("app_access_token") or data.get("tenant_access_token") or ""

    async def _exchange_code_for_user(self, config: dict, code: str) -> dict:
        # OAuth v2: exchange code directly with client credentials (no app_access_token needed)
        redirect_uri = config.get("redirect_uri") or resolve_feishu_redirect_uri()
        if not redirect_uri:
            raise RuntimeError("Feishu redirect URI not configured")

        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                FEISHU_TOKEN_URL_V2,
                json={
                    "grant_type": "authorization_code",
                    "client_id": config["app_id"],
                    "client_secret": config["app_secret"],
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
            # v2 returns access_token at top level (not nested under data)
            access_token = token_data.get("access_token") or token_data.get("data", {}).get("access_token", "")
            if not access_token:
                raise RuntimeError(f"Feishu OAuth v2 code exchange failed: {token_data.get('error_description', token_data)}")

            info_resp = await client.get(
                FEISHU_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            info_resp.raise_for_status()
            info = info_resp.json().get("data", {})

        return {
            "user_id": info.get("user_id"),
            "open_id": info.get("open_id"),
            "union_id": info.get("union_id"),
            "name": info.get("name", ""),
            "email": info.get("email", ""),
            "avatar_url": info.get("avatar_url", ""),
            "mobile": info.get("mobile", ""),
            "raw_profile": info,
        }

    async def _find_or_create_user(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        profile: dict,
        tenant_id: uuid.UUID | None,
    ) -> User:
        user = await self._find_user_by_external_identity(db, provider, profile)
        if not user:
            user = await self._find_user_by_legacy_fields(db, tenant_id, profile)

        if not user:
            user = await self._create_user(db, tenant_id, profile)

        await self._upsert_external_identity(db, provider, user, profile)
        self._write_through_user_fields(user, profile)
        self._hydrate_user_profile(user, profile)
        return user

    async def _find_user_by_external_identity(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        profile: dict,
    ) -> User | None:
        query = select(User).join(ExternalIdentity, ExternalIdentity.user_id == User.id).where(
            ExternalIdentity.provider_id == provider.id
        )

        provider_user_id = profile.get("user_id")
        if provider_user_id:
            result = await db.execute(query.where(ExternalIdentity.provider_user_id == provider_user_id))
            user = result.scalar_one_or_none()
            if user:
                return user

        provider_open_id = profile.get("open_id")
        if provider_open_id:
            result = await db.execute(query.where(ExternalIdentity.provider_open_id == provider_open_id))
            user = result.scalar_one_or_none()
            if user:
                return user

        return None

    async def _find_user_by_legacy_fields(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        profile: dict,
    ) -> User | None:
        user_id = profile.get("user_id")
        open_id = profile.get("open_id")
        email = (profile.get("email") or "").strip()

        if user_id:
            query = select(User).where(User.feishu_user_id == user_id)
            if tenant_id is not None:
                query = query.where(User.tenant_id == tenant_id)
            result = await db.execute(query.limit(1))
            user = result.scalar_one_or_none()
            if user:
                return user

        if open_id:
            query = select(User).where(User.feishu_open_id == open_id)
            if tenant_id is not None:
                query = query.where(User.tenant_id == tenant_id)
            result = await db.execute(query.limit(1))
            user = result.scalar_one_or_none()
            if user:
                return user

        if email:
            query = select(User).where(User.email == email)
            if tenant_id is not None:
                query = query.where(User.tenant_id == tenant_id)
            result = await db.execute(query.limit(1))
            return result.scalar_one_or_none()

        return None

    async def _create_user(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        profile: dict,
    ) -> User:
        open_id = profile.get("open_id") or uuid.uuid4().hex
        email = (profile.get("email") or "").strip()
        username = email.split("@")[0] if email else f"feishu_{(profile.get('user_id') or open_id)[:16]}"
        display_name = (profile.get("name") or "").strip() or username

        result = await db.execute(select(User).where(User.username == username).limit(1))
        if result.scalar_one_or_none():
            username = f"{username}_{uuid.uuid4().hex[:6]}"

        if not email:
            email = f"{username}@feishu.local"

        user = User(
            username=username,
            email=email,
            password_hash=hash_password(secrets.token_hex(16)),
            display_name=display_name,
            avatar_url=profile.get("avatar_url"),
            tenant_id=tenant_id,
            role="member",
        )
        self._write_through_user_fields(user, profile)
        db.add(user)
        await db.flush()

        db.add(
            Participant(
                type="user",
                ref_id=user.id,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
            )
        )
        await db.flush()
        return user

    async def _upsert_external_identity(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: User,
        profile: dict,
    ) -> ExternalIdentity:
        identity = None
        provider_user_id = profile.get("user_id")
        provider_open_id = profile.get("open_id")

        if provider_user_id:
            result = await db.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider_id == provider.id,
                    ExternalIdentity.provider_user_id == provider_user_id,
                )
            )
            identity = result.scalar_one_or_none()

        if not identity and provider_open_id:
            result = await db.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider_id == provider.id,
                    ExternalIdentity.provider_open_id == provider_open_id,
                )
            )
            identity = result.scalar_one_or_none()

        provider_union_id = profile.get("union_id")
        if not identity and provider_union_id:
            result = await db.execute(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider_id == provider.id,
                    ExternalIdentity.provider_union_id == provider_union_id,
                )
            )
            identity = result.scalar_one_or_none()

        if not identity:
            identity = ExternalIdentity(provider_id=provider.id, user_id=user.id)
            db.add(identity)

        identity.user_id = user.id
        identity.provider_user_id = provider_user_id
        identity.provider_open_id = provider_open_id
        identity.provider_union_id = profile.get("union_id")
        identity.email = profile.get("email")
        identity.mobile = profile.get("mobile")
        identity.raw_profile = profile.get("raw_profile") or profile
        await db.flush()
        return identity

    def _hydrate_user_profile(self, user: User, profile: dict) -> None:
        if profile.get("avatar_url"):
            user.avatar_url = profile["avatar_url"]
        if profile.get("name"):
            user.display_name = profile["name"]
        if profile.get("email") and user.email.endswith("@feishu.local"):
            user.email = profile["email"]

    def _write_through_user_fields(self, user: User, profile: dict) -> None:
        if profile.get("open_id"):
            user.feishu_open_id = profile["open_id"]
        if profile.get("union_id"):
            user.feishu_union_id = profile["union_id"]
        if profile.get("user_id"):
            user.feishu_user_id = profile["user_id"]


feishu_auth_provider = FeishuAuthProvider()
