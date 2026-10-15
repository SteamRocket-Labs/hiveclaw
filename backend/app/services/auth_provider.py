"""Provider-driven Feishu authentication and identity binding."""

from __future__ import annotations

import secrets
import uuid

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.security import create_access_token, hash_password
from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.org import OrgMember
from app.models.organization_identity import OrgMemberUserLink
from app.models.participant import Participant
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.feishu_platform import resolve_feishu_platform
from app.services.identity_links import (
    _bindings_for_providers,
    apply_observed_subject_link,
    begin_deferred_identity_audit,
    commit_deferred_identity_audit,
    discard_deferred_identity_audit,
    member_subject_map,
    recheck_stored_link,
)

settings = get_settings()

CONTRADICTORY_SUBJECTS = "The Feishu subjects belong to different accounts. Nothing was linked."
EMAIL_ONLY_CANDIDATE = "This sign-in matches an email only. Nothing was linked. An administrator can repair the directory link."
EXTERNAL_IDENTITY_CONFLICT = "This Feishu subject is already linked to another account. Nothing was changed."
PROVIDER_DISABLED = "Feishu sign-in is disabled for this company."
_PROFILE_SUBJECTS = (
    ("user_id", "provider_user_id"),
    ("open_id", "provider_open_id"),
    ("union_id", "provider_union_id"),
)


class FeishuIdentityConflict(Exception):
    """A sign-in that must not take over an account or write a partial identity."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _get_runtime_settings():
    from app.config import get_settings as runtime_get_settings

    return runtime_get_settings()


_PROVIDER_RUNTIME_CONFIG = "feishu_provider_runtime_config"


def _remember_runtime_config(session: AsyncSession, provider_id: uuid.UUID, config: dict) -> None:
    """Hold the sign-in config off the provider row until directory locks are taken.

    Assigning ``IdentityProvider.config`` here would flush that row on the next
    query and deadlock a manual repair waiting on the same row.
    """

    info = getattr(session, "info", None)
    if info is None:
        return
    info.setdefault(_PROVIDER_RUNTIME_CONFIG, {})[provider_id] = config


def _runtime_config(session: AsyncSession, provider: IdentityProvider) -> dict:
    info = getattr(session, "info", None)
    stored = None
    if info is not None:
        stored = info.get(_PROVIDER_RUNTIME_CONFIG, {}).get(provider.id)
    if isinstance(stored, dict):
        return stored
    return provider.config or {}


def resolve_feishu_redirect_uri(settings_obj=None) -> str:
    """Return the OAuth callback URL used for both authorize and token exchange."""
    current_settings = settings_obj or _get_runtime_settings()
    explicit = (getattr(current_settings, "FEISHU_REDIRECT_URI", "") or "").strip()
    if explicit:
        return explicit

    public_base = (
        (getattr(current_settings, "PUBLIC_BASE_URL", "") or "") or (getattr(current_settings, "BASE_URL", "") or "")
    ).strip()
    if not public_base:
        return ""

    return f"{public_base.rstrip('/')}/api/auth/feishu/callback"


def _settings_platform_config(settings_obj) -> dict:
    return {
        "platform_region": getattr(settings_obj, "FEISHU_PLATFORM_REGION", "feishu_cn") or "feishu_cn",
        "open_api_domain": getattr(settings_obj, "FEISHU_OPEN_API_DOMAIN", "") or "",
        "oauth_authorize_url": getattr(settings_obj, "FEISHU_OAUTH_AUTHORIZE_URL", "") or "",
    }


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
        profile = await self._exchange_code_for_user(_runtime_config(db, provider), code)
        existing = await self._find_user_by_external_identity(db, provider, profile)
        if existing is not None and existing.is_active is not True:
            raise ValueError("User account is inactive")
        async with db.begin_nested():
            user = await self._find_or_create_user(db, provider, profile, tenant_id)
            if user.is_active is not True:
                raise ValueError("User account is inactive")
            await self._finish_sign_in(db, provider, user, profile)
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
        if user.is_active is not True:
            raise ValueError("User account is inactive")
        provider = await self._ensure_provider(db, user.tenant_id)
        profile = await self._exchange_code_for_user(_runtime_config(db, provider), code)
        async with db.begin_nested():
            await self._link_existing_directory_members(db, provider, user, profile)
            await self._finish_sign_in(db, provider, user, profile)
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
            select(IdentityProvider)
            .where(
                IdentityProvider.provider_type == self.provider_type,
                IdentityProvider.tenant_id == tenant_id,
            )
            .limit(1)
        )
        provider = result.scalar_one_or_none()
        if provider:
            if provider.is_active is not True or provider.sso_login_enabled is not True:
                raise FeishuIdentityConflict("provider_disabled", PROVIDER_DISABLED)
            _remember_runtime_config(db, provider.id, config)
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
        _remember_runtime_config(db, provider.id, config)
        return provider

    async def _load_provider_config(self, db: AsyncSession, tenant_id: uuid.UUID | None) -> dict:
        current_settings = _get_runtime_settings()
        redirect_uri = resolve_feishu_redirect_uri(current_settings)

        if tenant_id is not None:
            result = await db.execute(
                select(TenantSetting).where(
                    TenantSetting.tenant_id == tenant_id,
                    TenantSetting.key == "feishu_org_sync",
                )
            )
            setting = result.scalar_one_or_none()
            cfg = setting.value if setting and setting.value else {}
            if not (cfg.get("app_id") and cfg.get("app_secret")):
                raise RuntimeError("Feishu OAuth not configured")
            if not redirect_uri:
                raise RuntimeError("Feishu redirect URI not configured")
            return {
                "app_id": cfg["app_id"],
                "app_secret": cfg["app_secret"],
                "redirect_uri": redirect_uri,
                "platform_region": cfg.get("platform_region") or "feishu_cn",
                "open_api_domain": cfg.get("open_api_domain") or "",
                "oauth_authorize_url": cfg.get("oauth_authorize_url") or "",
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
                **_settings_platform_config(current_settings),
                "source": "global_env",
            }

        raise RuntimeError("Feishu OAuth not configured")

    async def _get_app_access_token(self, config: dict) -> str:
        platform = resolve_feishu_platform(config)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                platform.open_api_url("/auth/v3/app_access_token/internal"),
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

        platform = resolve_feishu_platform(config)
        async with httpx.AsyncClient(timeout=15) as client:
            token_resp = await client.post(
                platform.open_api_url("/authen/v2/oauth/token"),
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
                raise RuntimeError(
                    f"Feishu OAuth v2 code exchange failed: {token_data.get('error_description', token_data)}"
                )

            info_resp = await client.get(
                platform.open_api_url("/authen/v1/user_info"),
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
        external = await self._find_user_by_external_identity(db, provider, profile)
        if external is not None and external.is_active is not True:
            raise ValueError("User account is inactive")
        legacy = await self._find_user_by_legacy_fields(db, tenant_id, profile)
        if legacy is not None and legacy.is_active is not True:
            raise ValueError("User account is inactive")
        linked = await self._user_from_directory_link(db, provider, profile)
        accounts = [account for account in (external, legacy, linked) if account is not None]
        if len({account.id for account in accounts}) > 1:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        user = external or legacy or linked
        if tenant_id is None:
            await self._reject_company_subject_on_global_login(
                db, provider, profile, user.id if user is not None else None
            )
        if user is None:
            await self._refuse_email_collision(db, profile)
            user = await self._create_user(db, tenant_id, profile)
        await self._link_existing_directory_members(db, provider, user, profile)
        return user

    async def _find_user_by_external_identity(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        profile: dict,
    ) -> User | None:
        query = (
            select(User)
            .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
            .where(ExternalIdentity.provider_id == provider.id)
        )
        found: list[User] = []
        for key, column in (
            ("user_id", ExternalIdentity.provider_user_id),
            ("open_id", ExternalIdentity.provider_open_id),
            ("union_id", ExternalIdentity.provider_union_id),
        ):
            value = profile.get(key)
            if not value:
                continue
            rows = (await db.execute(query.where(column == value))).scalars().all()
            ids = {row.id: row for row in rows}
            if len(ids) > 1:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
            if ids:
                found.append(next(iter(ids.values())))
        if not found:
            return None
        if len({row.id for row in found}) > 1:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        user = found[0]
        if provider.tenant_id is None:
            if user.tenant_id is not None:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        elif user.tenant_id != provider.tenant_id:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        return user

    async def _find_user_by_legacy_fields(
        self,
        db: AsyncSession,
        tenant_id: uuid.UUID | None,
        profile: dict,
    ) -> User | None:
        found: list[User] = []
        for key, column in (
            ("user_id", User.feishu_user_id),
            ("open_id", User.feishu_open_id),
            ("union_id", User.feishu_union_id),
        ):
            value = profile.get(key)
            if not value:
                continue
            query = select(User).where(column == value)
            if tenant_id is None:
                query = query.where(User.tenant_id.is_(None))
            else:
                query = query.where(User.tenant_id == tenant_id)
            rows = (await db.execute(query.limit(2))).scalars().all()
            if len(rows) > 1:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
            if rows:
                found.append(rows[0])
        if not found:
            return None
        if len({row.id for row in found}) > 1:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        return found[0]

    async def _refuse_email_collision(self, db: AsyncSession, profile: dict) -> None:
        """Email can explain a collision. It cannot select the account."""

        email = (profile.get("email") or "").strip()
        if not email or email.endswith("@feishu.local"):
            return
        existing = (await db.execute(select(User.id).where(User.email == email).limit(1))).scalar_one_or_none()
        if existing is not None:
            raise FeishuIdentityConflict("email_only_candidate", EMAIL_ONLY_CANDIDATE)

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
        found: dict[uuid.UUID, ExternalIdentity] = {}
        for key, column in (
            ("user_id", ExternalIdentity.provider_user_id),
            ("open_id", ExternalIdentity.provider_open_id),
            ("union_id", ExternalIdentity.provider_union_id),
        ):
            value = profile.get(key)
            if not value:
                continue
            rows = (
                await db.execute(
                    select(ExternalIdentity).where(
                        ExternalIdentity.provider_id == provider.id,
                        column == value,
                    )
                )
            ).scalars().all()
            for row in rows:
                found[row.id] = row
        rows = list(found.values())
        owners = {row.user_id for row in rows}
        if len(owners) > 1:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        if owners and owners != {user.id}:
            raise FeishuIdentityConflict("external_identity_conflict", EXTERNAL_IDENTITY_CONFLICT)
        if not rows:
            identity = ExternalIdentity(provider_id=provider.id, user_id=user.id)
            db.add(identity)
            rows = [identity]
        columns = (
            ("provider_user_id", "user_id"),
            ("provider_open_id", "open_id"),
            ("provider_union_id", "union_id"),
        )
        column_owner: dict[str, ExternalIdentity] = {}
        for row in rows:
            for column, _key in columns:
                if getattr(row, column):
                    column_owner.setdefault(column, row)
        assignee: dict[str, ExternalIdentity] = {}
        for column, _key in columns:
            if column in column_owner:
                assignee[column] = column_owner[column]
            elif len(rows) == 1:
                assignee[column] = rows[0]
            else:
                assignee[column] = next((row for row in rows if not getattr(row, column)), rows[0])
        for row in rows:
            for column, key in columns:
                if assignee[column] is not row:
                    continue
                setattr(row, column, self._merged_subject(getattr(row, column), profile.get(key)))
            if profile.get("email"):
                row.email = profile.get("email")
            if profile.get("mobile"):
                row.mobile = profile.get("mobile")
            row.raw_profile = profile.get("raw_profile") or row.raw_profile or {"source": "sso"}
        await db.flush()
        return rows[0]

    async def _hydrate_user_profile(self, db: AsyncSession, user: User, profile: dict) -> None:
        if profile.get("avatar_url"):
            user.avatar_url = profile["avatar_url"]
        if profile.get("name"):
            user.display_name = str(profile["name"])[:100]
        email = (profile.get("email") or "").strip()
        if email and (user.email or "").endswith("@feishu.local") and email != user.email:
            taken = (
                await db.execute(select(User.id).where(User.email == email, User.id != user.id).limit(1))
            ).scalar_one_or_none()
            if taken is None:
                user.email = email

    def _write_through_user_fields(self, user: User, profile: dict) -> None:
        user.feishu_open_id = self._merged_subject(user.feishu_open_id, profile.get("open_id"))
        user.feishu_union_id = self._merged_subject(user.feishu_union_id, profile.get("union_id"))
        user.feishu_user_id = self._merged_subject(user.feishu_user_id, profile.get("user_id"))

    def _merged_subject(self, current: str | None, incoming: str | None) -> str | None:
        value = (incoming or "").strip()
        if not value:
            return current
        if current in (None, "", value):
            return value
        raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)

    async def _recheck_sign_in_enabled(self, db: AsyncSession, provider: IdentityProvider) -> IdentityProvider:
        """Read the current sign-in flag. A provider row lock is not taken here."""

        provider_id = provider.id
        db.expire(provider)
        current = (
            await db.execute(
                select(IdentityProvider).where(IdentityProvider.id == provider_id),
                execution_options={"populate_existing": True},
            )
        ).scalar_one_or_none()
        if current is None or current.is_active is not True or current.sso_login_enabled is not True:
            raise FeishuIdentityConflict("provider_disabled", PROVIDER_DISABLED)
        return current

    async def _apply_provider_runtime(self, db: AsyncSession, provider: IdentityProvider) -> None:
        """Store the exchanged config only after every member advisory lock."""

        current = await self._recheck_sign_in_enabled(db, provider)
        runtime = _runtime_config(db, current)
        if current.name != "Feishu" or current.config != runtime:
            current.name = "Feishu"
            current.config = runtime

    async def _finish_sign_in(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: User,
        profile: dict,
    ) -> None:
        """Write account subjects after the per-member advisory locks are done."""

        await self._apply_provider_runtime(db, provider)
        self._write_through_user_fields(user, profile)
        await self._hydrate_user_profile(db, user, profile)

    async def _reject_company_subject_on_global_login(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        profile: dict,
        allowed_user_id: uuid.UUID | None,
    ) -> None:
        """A global sign-in must not mint or adopt an account for a company subject.

        Only ``feishu_open_id`` is a globally unique legacy column. A tenant
        ``feishu_union_id`` or ``feishu_user_id`` has no provider id and is
        not proof. Same-provider ExternalIdentity rows still are.
        """

        for key, column in (
            ("open_id", User.feishu_open_id),
        ):
            value = profile.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            owner = (
                await db.execute(
                    select(User.id).where(column == value.strip(), User.tenant_id.is_not(None)).limit(1)
                )
            ).scalar_one_or_none()
            if owner is not None and owner != allowed_user_id:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        for key, column in (
            ("user_id", ExternalIdentity.provider_user_id),
            ("open_id", ExternalIdentity.provider_open_id),
            ("union_id", ExternalIdentity.provider_union_id),
        ):
            value = profile.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            owner = (
                await db.execute(
                    select(User.id)
                    .join(ExternalIdentity, ExternalIdentity.user_id == User.id)
                    .where(
                        ExternalIdentity.provider_id == provider.id,
                        column == value.strip(),
                        User.tenant_id.is_not(None),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if owner is not None and owner != allowed_user_id:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)

    async def _persist_authenticated_identity(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: User,
        profile: dict,
    ) -> None:
        await self._recheck_sign_in_enabled(db, provider)
        await self._upsert_external_identity(db, provider, user, profile)

    async def _user_from_directory_link(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        profile: dict,
    ) -> User | None:
        """Use one current manual or sync link. Email is not a subject."""

        if provider.tenant_id is None or provider.is_active is not True:
            return None
        wanted = {
            "provider_user_id": (profile.get("user_id") or "").strip(),
            "provider_open_id": (profile.get("open_id") or "").strip(),
            "provider_union_id": (profile.get("union_id") or "").strip(),
        }
        clauses = [
            and_(OrgMemberUserLink.subject_kind == kind, OrgMemberUserLink.match_subject == value)
            for kind, value in wanted.items()
            if value
        ]
        if not clauses:
            return None
        links = (
            await db.execute(
                select(OrgMemberUserLink).where(
                    OrgMemberUserLink.tenant_id == provider.tenant_id,
                    OrgMemberUserLink.provider_id == provider.id,
                    OrgMemberUserLink.status == "active",
                    or_(*clauses),
                )
            )
        ).scalars().all()
        if not links:
            return None
        bindings = await _bindings_for_providers(db, [provider.id], actor_id=None)
        accepted: dict[uuid.UUID, User] = {}
        inactive = False
        for link in links:
            member = await db.get(OrgMember, link.org_member_id)
            account = await db.get(User, link.user_id)
            valid, reason = recheck_stored_link(
                link, member=member, user=account, provider=provider, bindings=bindings
            )
            if valid and account is not None:
                accepted[account.id] = account
            elif reason == "inactive_user":
                inactive = True
        if len(accepted) > 1:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        if len(accepted) == 1:
            return next(iter(accepted.values()))
        if inactive:
            raise ValueError("User account is inactive")
        return None

    async def _link_existing_directory_members(
        self,
        db: AsyncSession,
        provider: IdentityProvider,
        user: User,
        profile: dict,
    ) -> None:
        """Attach SSO evidence to directory people that already exist. Never create one."""

        if provider.tenant_id is None:
            if user.tenant_id is not None:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
            await self._persist_authenticated_identity(db, provider, user, profile)
            return
        if user.tenant_id != provider.tenant_id:
            raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
        wanted = {
            "provider_user_id": (profile.get("user_id") or "").strip(),
            "provider_open_id": (profile.get("open_id") or "").strip(),
            "provider_union_id": (profile.get("union_id") or "").strip(),
        }
        clauses = []
        if wanted["provider_user_id"]:
            clauses.append(OrgMember.feishu_user_id == wanted["provider_user_id"])
            clauses.append(OrgMember.external_id == wanted["provider_user_id"])
        if wanted["provider_open_id"]:
            clauses.append(OrgMember.feishu_open_id == wanted["provider_open_id"])
            clauses.append(OrgMember.open_id == wanted["provider_open_id"])
        if wanted["provider_union_id"]:
            clauses.append(OrgMember.unionid == wanted["provider_union_id"])
        if not clauses:
            await self._persist_authenticated_identity(db, provider, user, profile)
            return
        members = (
            await db.execute(select(OrgMember).where(OrgMember.tenant_id == provider.tenant_id, or_(*clauses)))
        ).scalars().all()
        seen: set[uuid.UUID] = set()
        matched: list[tuple[OrgMember, dict[str, str]]] = []
        for member in members:
            if member.id in seen:
                continue
            seen.add(member.id)
            subjects = member_subject_map(member)
            if subjects.ambiguous:
                raise FeishuIdentityConflict("contradictory_subjects", CONTRADICTORY_SUBJECTS)
            matched.append((member, subjects.values))
        matched.sort(key=lambda item: item[0].id)
        if not matched:
            await self._persist_authenticated_identity(db, provider, user, profile)
            return
        persisted = False

        async def persist_after_lock() -> None:
            nonlocal persisted
            if persisted:
                return
            persisted = True
            await self._persist_authenticated_identity(db, provider, user, profile)

        begin_deferred_identity_audit(db)
        try:
            for member, subjects in matched:
                for kind, value in wanted.items():
                    if not value or subjects.get(kind) != value:
                        continue
                    outcome = await apply_observed_subject_link(
                        db,
                        tenant_id=provider.tenant_id,
                        actor_id=user.id,
                        member=member,
                        user=user,
                        provider=provider,
                        subject_kind=kind,
                        source="sso",
                        after_lock=persist_after_lock,
                    )
                    if outcome.code == "inactive_user":
                        raise ValueError("User account is inactive")
                    if outcome.reason == "active_link_conflict" or outcome.code in {
                        "cross_tenant",
                        "provider_unproven",
                        "provider_namespace",
                        "unproven_provider",
                    }:
                        raise FeishuIdentityConflict("external_identity_conflict", EXTERNAL_IDENTITY_CONFLICT)
                    if outcome.code == "conflict" and "stays unchanged" not in outcome.message:
                        raise FeishuIdentityConflict("external_identity_conflict", EXTERNAL_IDENTITY_CONFLICT)
            if not persisted:
                await self._persist_authenticated_identity(db, provider, user, profile)
            await commit_deferred_identity_audit(db)
        finally:
            discard_deferred_identity_audit(db)


feishu_auth_provider = FeishuAuthProvider()
