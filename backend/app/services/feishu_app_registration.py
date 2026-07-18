"""Agent-scoped Feishu/Lark application registration through the official QR flow.

Redis is the short-lived registration-session authority. App credentials never
enter Redis or API responses; after the official SDK returns them they are
written directly to the encrypted ``ChannelConfig`` columns and the existing
WebSocket supervisor is started.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError
from redis.exceptions import RedisError

from app.core.events import get_redis


FEISHU_REGISTRATION_REGION_CN = "feishu_cn"
FEISHU_REGISTRATION_REGION_LARK = "lark_global"

FEISHU_ACCOUNTS_DOMAIN = "https://accounts.feishu.cn"
LARK_ACCOUNTS_DOMAIN = "https://accounts.larksuite.com"
_ALLOWED_QR_HOSTS = frozenset({"accounts.feishu.cn", "accounts.larksuite.com"})

REGISTRATION_SESSION_TTL_SECONDS = 30 * 60
REGISTRATION_STALE_AFTER_SECONDS = 90

_ACTIVE_STATUSES = frozenset(
    {
        "initializing",
        "scanning",
        "polling",
        "slow_down",
        "domain_switched",
        "credentials_received",
        "connecting",
    }
)
_CANCELLABLE_STATUSES = frozenset({"initializing", "scanning", "polling", "slow_down", "domain_switched"})
_TERMINAL_STATUSES = frozenset({"connected", "denied", "expired", "cancelled", "failed", "interrupted"})

# The SDK's default preset is retained and the minimum Hive IM permissions are
# explicit so the generated app can receive and answer messages immediately.
_REGISTRATION_ADDONS: dict[str, Any] = {
    "preset": True,
    "scopes": {
        "tenant": [
            "contact:contact.base:readonly",
            "contact:user.base:readonly",
            "contact:user.id:readonly",
            "im:chat",
            "im:message",
            "im:message.group_at_msg:readonly",
            "im:message.p2p_msg:readonly",
            "im:message:send_as_bot",
            "im:resource",
        ],
        "user": [],
    },
    "events": {"items": {"tenant": ["im.message.receive_v1"], "user": []}},
    "callbacks": {"items": ["card.action.trigger"]},
}

_COMPARE_AND_SWAP_SCRIPT = """
-- hive_feishu_registration_compare_and_swap
local active = redis.call('GET', KEYS[1])
if active ~= ARGV[1] then return 0 end
local current = redis.call('GET', KEYS[2])
if current ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
return 1
"""

_DELETE_ACTIVE_SCRIPT = """
-- hive_feishu_registration_delete_active
local active = redis.call('GET', KEYS[1])
if active ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class FeishuRegistrationError(RuntimeError):
    """Typed registration failure safe to map onto an API response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FeishuRegistrationStateUnavailable(FeishuRegistrationError):
    def __init__(self) -> None:
        super().__init__(
            "registration_state_unavailable",
            "Registration state is temporarily unavailable. Please try again.",
        )


class FeishuRegistrationConflict(FeishuRegistrationError):
    def __init__(self, message: str = "Another registration is already active for this Agent.") -> None:
        super().__init__("registration_already_active", message)


class FeishuRegistrationNotFound(FeishuRegistrationError):
    def __init__(self) -> None:
        super().__init__("registration_not_found", "Registration session not found or expired.")


class FeishuRegistrationForbidden(FeishuRegistrationError):
    def __init__(self) -> None:
        super().__init__("registration_forbidden", "This registration session belongs to another user or Agent.")


class FeishuRegistrationNotCancellable(FeishuRegistrationError):
    def __init__(self) -> None:
        super().__init__(
            "registration_not_cancellable",
            "The registration has already received credentials and can no longer be cancelled.",
        )


class InvalidVerificationUrl(FeishuRegistrationError):
    def __init__(self) -> None:
        super().__init__("invalid_verification_url", "The registration provider returned an invalid QR URL.")


class RegistrationAuthorizationLost(FeishuRegistrationError):
    def __init__(self) -> None:
        super().__init__(
            "registration_authorization_lost",
            "Your Agent management access changed before registration completed. Please start again.",
        )


@dataclass(frozen=True, slots=True)
class FeishuRegistrationContext:
    session_id: str
    tenant_id: str
    agent_id: str
    actor_user_id: str
    requested_platform_region: str
    agent_name: str


@dataclass(frozen=True, slots=True)
class FeishuRegistrationState:
    schema_version: int
    session_id: str
    tenant_id: str
    agent_id: str
    actor_user_id: str
    requested_platform_region: str
    resolved_platform_region: str | None
    status: str
    verification_url: str | None
    qr_expires_at: str | None
    connection_status: str | None
    message: str | None
    error_code: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_json(cls, raw: str) -> "FeishuRegistrationState":
        return cls(**json.loads(raw))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "platform_region": self.requested_platform_region,
            "resolved_platform_region": self.resolved_platform_region,
            "verification_url": self.verification_url,
            "qr_expires_at": self.qr_expires_at,
            "connection_status": self.connection_status,
            "message": self.message,
            "error_code": self.error_code,
            "connected": self.status == "connected",
            "cancellable": self.status in _CANCELLABLE_STATUSES,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


RedisGetter = Callable[[], Any | Awaitable[Any]]
RegistrationRunner = Callable[..., Awaitable[dict[str, Any]]]
CredentialPersister = Callable[[FeishuRegistrationContext, dict[str, Any], str], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _active_key(tenant_id: str, agent_id: str) -> str:
    return f"hive:feishu-app-registration:active:{tenant_id}:{agent_id}"


def _session_key(session_id: str) -> str:
    return f"hive:feishu-app-registration:session:{session_id}"


def _validate_verification_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidVerificationUrl() from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_QR_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise InvalidVerificationUrl()
    return url


def _registration_domains(platform_region: str) -> tuple[str, str]:
    if platform_region == FEISHU_REGISTRATION_REGION_CN:
        return FEISHU_ACCOUNTS_DOMAIN, LARK_ACCOUNTS_DOMAIN
    if platform_region == FEISHU_REGISTRATION_REGION_LARK:
        return LARK_ACCOUNTS_DOMAIN, LARK_ACCOUNTS_DOMAIN
    raise FeishuRegistrationError("invalid_platform_region", "Select Feishu (China) or Lark (Global).")


async def _official_registration_runner(**kwargs: Any) -> dict[str, Any]:
    from lark_oapi import aregister_app

    return await aregister_app(**kwargs)


def _resolved_region(credentials: dict[str, Any], requested_region: str) -> str:
    user_info = credentials.get("user_info")
    tenant_brand = str(user_info.get("tenant_brand") if isinstance(user_info, dict) else "").lower()
    if tenant_brand == "lark":
        return FEISHU_REGISTRATION_REGION_LARK
    if tenant_brand == "feishu":
        return FEISHU_REGISTRATION_REGION_CN
    return requested_region


async def _persist_registered_credentials(
    context: FeishuRegistrationContext,
    credentials: dict[str, Any],
    resolved_region: str,
) -> None:
    """Re-check authority, write encrypted credentials, then start WebSocket."""

    from fastapi import HTTPException
    from sqlalchemy import select

    from app.core.permissions import require_agent_manage_access
    from app.core.policy import write_audit_event
    from app.database import enter_rls_bypass, tenant_scoped_session
    from app.models.channel_config import ChannelConfig
    from app.models.user import User
    from app.services.feishu_ws import feishu_ws_manager

    app_id = str(credentials.get("client_id") or "").strip()
    app_secret = str(credentials.get("client_secret") or "").strip()
    if not app_id or not app_secret:
        raise FeishuRegistrationError(
            "registration_credentials_missing",
            "The registration provider did not return complete app credentials.",
        )

    tenant_uuid = uuid.UUID(context.tenant_id)
    agent_uuid = uuid.UUID(context.agent_id)
    actor_uuid = uuid.UUID(context.actor_user_id)
    registered_at = _iso_now()
    extra_config: dict[str, Any]

    async with tenant_scoped_session(
        tenant_uuid,
        require_tenant=True,
        source="feishu_app_registration.persist",
    ) as db:
        actor_result = await db.execute(select(User).where(User.id == actor_uuid))
        actor = actor_result.scalar_one_or_none()
        if actor is None:
            # Platform administrators can be tenant-less. The bypass remains an
            # exact-ID lookup and is itself audited by the database boundary.
            async with enter_rls_bypass(
                db,
                reason=f"Feishu registration actor revalidation for {context.session_id}",
                actor_id=context.actor_user_id,
            ) as bypass_db:
                actor_result = await bypass_db.execute(select(User).where(User.id == actor_uuid))
                actor = actor_result.scalar_one_or_none()
        if actor is None:
            raise RegistrationAuthorizationLost()

        try:
            agent = await require_agent_manage_access(db, actor, agent_uuid)
        except HTTPException as exc:
            raise RegistrationAuthorizationLost() from exc
        if str(agent.tenant_id) != context.tenant_id:
            raise RegistrationAuthorizationLost()

        config_result = await db.execute(
            select(ChannelConfig)
            .where(
                ChannelConfig.agent_id == agent_uuid,
                ChannelConfig.channel_type == "feishu",
            )
            .with_for_update()
        )
        config = config_result.scalar_one_or_none()
        existing_extra = dict(config.extra_config or {}) if config else {}
        existing_extra.pop("connection_error", None)
        existing_extra.pop("last_error", None)
        extra_config = {
            **existing_extra,
            "connection_mode": "websocket",
            "platform_region": resolved_region,
            "setup_method": "qr_registration",
            "registration_session_id": context.session_id,
            "connection_status": "connecting",
            "registered_at": registered_at,
        }

        if config is None:
            config = ChannelConfig(
                agent_id=agent_uuid,
                tenant_id=tenant_uuid,
                channel_type="feishu",
            )
            db.add(config)

        config.app_id = app_id
        config.app_secret = app_secret
        config.encrypt_key = None
        config.verification_token = None
        config.extra_config = extra_config
        config.is_configured = True
        config.is_connected = False
        await db.flush()

        await write_audit_event(
            db,
            event_type="channel.feishu_qr_registered",
            severity="info",
            actor_type="user",
            actor_id=actor_uuid,
            tenant_id=tenant_uuid,
            action="configure_feishu_channel_qr",
            resource_type="agent_channel",
            resource_id=agent_uuid,
            details={
                "channel_type": "feishu",
                "requested_platform_region": context.requested_platform_region,
                "resolved_platform_region": resolved_region,
                "registration_session_id": context.session_id,
                "app_id": app_id,
            },
        )

    # The tenant-scoped context commits before this side effect. A process
    # crash here remains recoverable because FeishuWSManager.start_all() scans
    # the committed ChannelConfig on startup.
    await feishu_ws_manager.start_client(
        agent_uuid,
        app_id,
        app_secret,
        extra_config=extra_config,
    )


class FeishuAppRegistrationManager:
    """Coordinates fenced, resumable Agent Detail QR registration sessions."""

    def __init__(
        self,
        *,
        redis_getter: RedisGetter = get_redis,
        registration_runner: RegistrationRunner = _official_registration_runner,
        credential_persister: CredentialPersister = _persist_registered_credentials,
    ) -> None:
        self._redis_getter = redis_getter
        self._registration_runner = registration_runner
        self._credential_persister = credential_persister
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def _redis(self):
        try:
            candidate = self._redis_getter()
            return await candidate if inspect.isawaitable(candidate) else candidate
        except RedisError as exc:
            raise FeishuRegistrationStateUnavailable() from exc

    async def _redis_call(self, method: str, *args: Any, **kwargs: Any):
        client = await self._redis()
        try:
            return await getattr(client, method)(*args, **kwargs)
        except RedisError as exc:
            raise FeishuRegistrationStateUnavailable() from exc

    async def _load_state(self, session_id: str) -> tuple[str, FeishuRegistrationState]:
        raw = await self._redis_call("get", _session_key(session_id))
        if not raw:
            raise FeishuRegistrationNotFound()
        try:
            return raw, FeishuRegistrationState.from_json(raw)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("[Feishu Registration] Corrupt session state session={}", session_id)
            raise FeishuRegistrationNotFound() from exc

    @staticmethod
    def _assert_context(
        state: FeishuRegistrationState,
        *,
        tenant_id: str | uuid.UUID,
        agent_id: str | uuid.UUID,
        actor_user_id: str | uuid.UUID,
    ) -> None:
        if (
            state.tenant_id != str(tenant_id)
            or state.agent_id != str(agent_id)
            or state.actor_user_id != str(actor_user_id)
        ):
            raise FeishuRegistrationForbidden()

    @staticmethod
    def _is_stale(state: FeishuRegistrationState) -> bool:
        # Before credentials arrive, the SDK poll loop emits a heartbeat every
        # few seconds. After persistence, PostgreSQL + the WS supervisor are
        # the recovery authority, so a slow connection must not be mislabeled
        # as a dead registration worker.
        if state.status not in _CANCELLABLE_STATUSES:
            return False
        try:
            updated_at = datetime.fromisoformat(state.updated_at)
        except ValueError:
            return True
        return _utc_now() - updated_at > timedelta(seconds=REGISTRATION_STALE_AFTER_SECONDS)

    async def _compare_and_swap(
        self,
        session_id: str,
        *,
        patch: dict[str, Any],
        allowed_statuses: frozenset[str] | set[str] | None = None,
    ) -> FeishuRegistrationState | None:
        for _attempt in range(8):
            try:
                raw, current = await self._load_state(session_id)
            except FeishuRegistrationNotFound:
                return None
            if allowed_statuses is not None and current.status not in allowed_statuses:
                return None
            updated = replace(current, **patch, updated_at=_iso_now())
            active_key = _active_key(current.tenant_id, current.agent_id)
            swapped = await self._redis_call(
                "eval",
                _COMPARE_AND_SWAP_SCRIPT,
                2,
                active_key,
                _session_key(session_id),
                session_id,
                raw,
                updated.to_json(),
                str(REGISTRATION_SESSION_TTL_SECONDS),
            )
            if int(swapped or 0) == 1:
                return updated
            await asyncio.sleep(0)
        return None

    async def _delete_active(self, state: FeishuRegistrationState) -> None:
        await self._redis_call(
            "eval",
            _DELETE_ACTIVE_SCRIPT,
            1,
            _active_key(state.tenant_id, state.agent_id),
            state.session_id,
        )

    async def _finish_failure(
        self,
        session_id: str,
        *,
        status: str,
        error_code: str,
        message: str,
    ) -> FeishuRegistrationState | None:
        state = await self._compare_and_swap(
            session_id,
            patch={"status": status, "error_code": error_code, "message": message},
            allowed_statuses=_ACTIVE_STATUSES,
        )
        if state is not None:
            await self._delete_active(state)
        return state

    async def _record_background_failure(
        self,
        session_id: str,
        *,
        status: str,
        error_code: str,
        message: str,
    ) -> None:
        try:
            await self._finish_failure(
                session_id,
                status=status,
                error_code=error_code,
                message=message,
            )
        except FeishuRegistrationStateUnavailable:
            # Redis is the only ephemeral session authority. Credentials are
            # not persisted without a successful fence, and the browser gets
            # a typed 503 when it next reads the registration endpoint.
            logger.warning(
                "[Feishu Registration] Could not record background failure because Redis is unavailable session={}",
                session_id,
            )

    async def start_registration(
        self,
        *,
        tenant_id: str | uuid.UUID,
        agent_id: str | uuid.UUID,
        actor_user_id: str | uuid.UUID,
        platform_region: str,
        agent_name: str,
    ) -> FeishuRegistrationState:
        _registration_domains(platform_region)
        client = await self._redis()
        try:
            await client.ping()
        except RedisError as exc:
            raise FeishuRegistrationStateUnavailable() from exc

        tenant = str(tenant_id)
        agent = str(agent_id)
        actor = str(actor_user_id)
        active_key = _active_key(tenant, agent)

        for _attempt in range(2):
            session_id = str(uuid.uuid4())
            now = _iso_now()
            state = FeishuRegistrationState(
                schema_version=1,
                session_id=session_id,
                tenant_id=tenant,
                agent_id=agent,
                actor_user_id=actor,
                requested_platform_region=platform_region,
                resolved_platform_region=None,
                status="initializing",
                verification_url=None,
                qr_expires_at=None,
                connection_status=None,
                message="Preparing the official QR registration flow.",
                error_code=None,
                created_at=now,
                updated_at=now,
            )
            await self._redis_call(
                "set",
                _session_key(session_id),
                state.to_json(),
                ex=REGISTRATION_SESSION_TTL_SECONDS,
            )
            acquired = await self._redis_call(
                "set",
                active_key,
                session_id,
                ex=REGISTRATION_SESSION_TTL_SECONDS,
                nx=True,
            )
            if acquired:
                context = FeishuRegistrationContext(
                    session_id=session_id,
                    tenant_id=tenant,
                    agent_id=agent,
                    actor_user_id=actor,
                    requested_platform_region=platform_region,
                    agent_name=str(agent_name or "").strip(),
                )
                task = asyncio.create_task(
                    self._run_registration(context),
                    name=f"feishu-app-registration:{session_id[:8]}",
                )
                self._tasks[session_id] = task
                return state

            await self._redis_call("delete", _session_key(session_id))
            existing_session_id = await self._redis_call("get", active_key)
            if not existing_session_id:
                continue
            try:
                _raw, existing = await self._load_state(str(existing_session_id))
            except FeishuRegistrationNotFound:
                await self._redis_call("eval", _DELETE_ACTIVE_SCRIPT, 1, active_key, str(existing_session_id))
                continue
            if existing.status in _TERMINAL_STATUSES or self._is_stale(existing):
                if self._is_stale(existing):
                    await self._compare_and_swap(
                        existing.session_id,
                        patch={
                            "status": "interrupted",
                            "error_code": "registration_interrupted",
                            "message": "The registration worker stopped. Start a new QR registration.",
                        },
                        allowed_statuses=_ACTIVE_STATUSES,
                    )
                await self._delete_active(existing)
                continue
            if existing.actor_user_id == actor:
                return existing
            raise FeishuRegistrationConflict()

        raise FeishuRegistrationConflict()

    async def get_session(self, session_id: str | uuid.UUID) -> FeishuRegistrationState:
        _raw, state = await self._load_state(str(session_id))
        if self._is_stale(state):
            interrupted = await self._finish_failure(
                state.session_id,
                status="interrupted",
                error_code="registration_interrupted",
                message="The registration worker stopped. Start a new QR registration.",
            )
            return interrupted or (await self._load_state(state.session_id))[1]
        return state

    async def get_active_registration(
        self,
        *,
        tenant_id: str | uuid.UUID,
        agent_id: str | uuid.UUID,
        actor_user_id: str | uuid.UUID,
    ) -> FeishuRegistrationState:
        active_key = _active_key(str(tenant_id), str(agent_id))
        session_id = await self._redis_call("get", active_key)
        if not session_id:
            raise FeishuRegistrationNotFound()
        state = await self.get_session(str(session_id))
        self._assert_context(
            state,
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        return state

    async def get_registration_for_actor(
        self,
        session_id: str | uuid.UUID,
        *,
        tenant_id: str | uuid.UUID,
        agent_id: str | uuid.UUID,
        actor_user_id: str | uuid.UUID,
    ) -> FeishuRegistrationState:
        state = await self.get_session(session_id)
        self._assert_context(
            state,
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        return state

    async def cancel_registration(
        self,
        session_id: str | uuid.UUID,
        *,
        tenant_id: str | uuid.UUID,
        agent_id: str | uuid.UUID,
        actor_user_id: str | uuid.UUID,
    ) -> FeishuRegistrationState:
        session = str(session_id)
        current = await self.get_registration_for_actor(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            actor_user_id=actor_user_id,
        )
        if current.status not in _CANCELLABLE_STATUSES:
            raise FeishuRegistrationNotCancellable()
        cancelled = await self._compare_and_swap(
            session,
            patch={
                "status": "cancelled",
                "error_code": "registration_cancelled",
                "message": "Registration cancelled.",
            },
            allowed_statuses=_CANCELLABLE_STATUSES,
        )
        if cancelled is None:
            latest = await self.get_session(session)
            if latest.status not in _CANCELLABLE_STATUSES:
                raise FeishuRegistrationNotCancellable()
            raise FeishuRegistrationConflict("Registration state changed. Refresh and try again.")
        await self._delete_active(cancelled)
        task = self._tasks.get(session)
        if task is not None and not task.done():
            task.cancel()
        return cancelled

    async def reconcile_channel_status(
        self,
        state: FeishuRegistrationState,
        *,
        is_connected: bool,
        is_configured: bool,
        connection_status: str | None,
    ) -> FeishuRegistrationState:
        if state.status not in {"credentials_received", "connecting"}:
            return state
        if is_connected:
            connected = await self._compare_and_swap(
                state.session_id,
                patch={
                    "status": "connected",
                    "connection_status": "connected",
                    "error_code": None,
                    "message": "The app is connected through WebSocket.",
                },
                allowed_statuses={"credentials_received", "connecting"},
            )
            if connected is not None:
                await self._delete_active(connected)
                return connected
        if not is_configured or connection_status == "invalid_credentials":
            failed = await self._finish_failure(
                state.session_id,
                status="failed",
                error_code="websocket_credentials_rejected",
                message="The app was created, but Feishu/Lark rejected its credentials. Scan again.",
            )
            if failed is not None:
                return failed
        if connection_status and connection_status != state.connection_status:
            updated = await self._compare_and_swap(
                state.session_id,
                patch={"connection_status": connection_status},
                allowed_statuses={"credentials_received", "connecting"},
            )
            if updated is not None:
                return updated
        return state

    async def _run_registration(self, context: FeishuRegistrationContext) -> None:
        callback_tasks: list[asyncio.Task[Any]] = []

        def _schedule_update(coro: Awaitable[Any]) -> None:
            task = asyncio.create_task(coro)
            callback_tasks.append(task)

        def _on_qr_code(info: dict[str, Any]) -> None:
            verification_url = _validate_verification_url(info.get("url"))
            expire_in = max(1, min(int(info.get("expire_in") or 600), REGISTRATION_SESSION_TTL_SECONDS))
            expires_at = (_utc_now() + timedelta(seconds=expire_in)).isoformat()
            _schedule_update(
                self._compare_and_swap(
                    context.session_id,
                    patch={
                        "status": "scanning",
                        "verification_url": verification_url,
                        "qr_expires_at": expires_at,
                        "message": "Scan the QR code and confirm the app registration.",
                    },
                    allowed_statuses={"initializing", "scanning"},
                )
            )

        def _on_status_change(info: dict[str, Any]) -> None:
            provider_status = str(info.get("status") or "")
            mapping = {
                "polling": ("polling", "Waiting for scan confirmation."),
                "slow_down": ("slow_down", "Confirmation is taking longer than expected."),
                "domain_switched": ("domain_switched", "Continuing registration on Lark Global."),
            }
            mapped = mapping.get(provider_status)
            if mapped is None:
                return
            _schedule_update(
                self._compare_and_swap(
                    context.session_id,
                    patch={"status": mapped[0], "message": mapped[1]},
                    allowed_statuses=_CANCELLABLE_STATUSES,
                )
            )

        domain, lark_domain = _registration_domains(context.requested_platform_region)
        normalized_agent_name = " ".join(context.agent_name.split())
        app_name = normalized_agent_name[:20] or "Hive Agent"
        credentials: dict[str, Any] | None = None
        try:
            credentials = await self._registration_runner(
                on_qr_code=_on_qr_code,
                on_status_change=_on_status_change,
                source="hive-agent-detail",
                domain=domain,
                lark_domain=lark_domain,
                app_preset={"name": app_name, "desc": "Hive digital employee IM channel"},
                addons=_REGISTRATION_ADDONS,
            )
            if callback_tasks:
                await asyncio.gather(*tuple(callback_tasks))
            if not credentials.get("client_id") or not credentials.get("client_secret"):
                raise FeishuRegistrationError(
                    "registration_credentials_missing",
                    "The registration provider did not return complete app credentials.",
                )
            region = _resolved_region(credentials, context.requested_platform_region)
            fenced = await self._compare_and_swap(
                context.session_id,
                patch={
                    "status": "credentials_received",
                    "resolved_platform_region": region,
                    "verification_url": None,
                    "qr_expires_at": None,
                    "message": "Credentials received. Saving the encrypted channel configuration.",
                },
                allowed_statuses=_CANCELLABLE_STATUSES,
            )
            if fenced is None:
                return
            await self._credential_persister(context, credentials, region)
            await self._compare_and_swap(
                context.session_id,
                patch={
                    "status": "connecting",
                    "resolved_platform_region": region,
                    "connection_status": "connecting",
                    "message": "App created. Establishing the WebSocket connection.",
                },
                allowed_statuses={"credentials_received"},
            )
        except asyncio.CancelledError:
            await self._record_background_failure(
                context.session_id,
                status="interrupted",
                error_code="registration_interrupted",
                message="The registration worker stopped. Start a new QR registration.",
            )
        except InvalidVerificationUrl as exc:
            await self._record_background_failure(
                context.session_id,
                status="failed",
                error_code=exc.code,
                message=exc.message,
            )
        except FeishuRegistrationError as exc:
            await self._record_background_failure(
                context.session_id,
                status="failed",
                error_code=exc.code,
                message=exc.message,
            )
        except AppAccessDeniedError:
            await self._record_background_failure(
                context.session_id,
                status="denied",
                error_code="registration_denied",
                message="Registration was denied in Feishu/Lark.",
            )
        except AppExpiredError:
            await self._record_background_failure(
                context.session_id,
                status="expired",
                error_code="registration_expired",
                message="The QR code expired. Start a new registration.",
            )
        except Exception as exc:  # SDK errors are mapped without exposing provider payloads or secrets.
            error_name = type(exc).__name__
            # Never attach the exception or SQL parameters here: once the SDK
            # has returned, they may include the plaintext client_secret.
            logger.warning(
                "[Feishu Registration] Flow failed session={} error_type={}",
                context.session_id,
                error_name,
            )
            await self._record_background_failure(
                context.session_id,
                status="failed",
                error_code="registration_failed",
                message="Feishu/Lark registration failed. Please try again.",
            )
        finally:
            self._tasks.pop(context.session_id, None)
            if credentials is not None:
                credentials.pop("client_secret", None)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


feishu_app_registration_manager = FeishuAppRegistrationManager()
