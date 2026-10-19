"""Feishu helpers — low-level utilities shared by all feishu domain modules."""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.feishu_platform import FeishuPlatform, resolve_feishu_platform

logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class FeishuToolAuth:
    app_id: str
    app_secret: str
    platform: FeishuPlatform

    @property
    def extra_config(self) -> dict:
        return {"platform_region": self.platform.region, "open_api_domain": self.platform.open_api_domain,
            "oauth_authorize_url": self.platform.oauth_authorize_url}


class FeishuAccessToken(str):
    """Keep the legacy string token interface with its resolved transport."""

    def __new__(cls, value: str, platform: FeishuPlatform):
        token = super().__new__(cls, value)
        token.platform = platform
        return token


def _feishu_token_platform(token: str) -> FeishuPlatform:
    # Plain tokens retain the historical CN helper contract. Configured tools
    # always obtain a platform-bound token from the authentication boundary.
    return token.platform if isinstance(token, FeishuAccessToken) else resolve_feishu_platform()


def _feishu_api_url(token: str, path: str) -> str:
    return _feishu_token_platform(token).open_api_url(path)


def _format_feishu_token_error(status: dict) -> str:
    """Return a human-readable auth failure without exposing credentials."""
    if not status.get("configured"):
        return "❌ Agent has no Feishu channel configured."

    message = status.get("message") or "unknown authentication error"
    code = status.get("code")
    code_suffix = f" (code {code})" if code is not None else ""
    return (
        "❌ Feishu channel is configured but authentication failed: "
        f"{message}{code_suffix}. Please update the app_secret in the agent's Feishu channel settings."
    )


async def _get_feishu_app_credentials(agent_id: uuid.UUID) -> tuple[str, str] | None:
    """Get (app_id, app_secret) from agent channel config or tenant-level bot config."""
    auth = await _get_feishu_tool_auth(agent_id)
    return (auth.app_id, auth.app_secret) if auth else None


async def _get_feishu_tool_auth(agent_id: uuid.UUID) -> FeishuToolAuth | None:
    """Resolve credentials and their platform from the same current installation."""
    tid = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(tid) as db:
        config = await load_feishu_channel_configuration(db, agent_id=agent_id)
        from app.services.feishu_read_receipts import observe_feishu_connection

        observe_feishu_connection(config)
        return FeishuToolAuth(config.app_id, config.app_secret, resolve_feishu_platform(config.extra_config)) if config else None


async def load_feishu_channel_configuration(db, *, agent_id):
    """Resolve the actual employee/company configuration used by Feishu tools."""
    from app.models.agent import Agent
    from app.models.channel_config import ChannelConfig
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.services.channel_delivery_service import channel_delivery_target

    target = channel_delivery_target.get() or {}
    if target.get("tenant_channel_config_id"):
        from app.services.channel_installation import load_tenant_channel_for_target

        return await load_tenant_channel_for_target(db, agent_id=agent_id, channel="feishu", target=target)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "feishu",
            ChannelConfig.is_configured,
        )
    )
    config = result.scalar_one_or_none()
    if config and config.app_id and config.app_secret:
        return config

    agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent or not agent.tenant_id:
        return None

    tenant_result = await db.execute(
        select(TenantChannelConfig).where(
            TenantChannelConfig.tenant_id == agent.tenant_id,
            TenantChannelConfig.channel_type == "feishu",
            TenantChannelConfig.is_active.is_(True),
        )
    )
    tenant_config = tenant_result.scalar_one_or_none()
    if tenant_config and tenant_config.app_id and tenant_config.app_secret:
        return tenant_config
    return None


async def _get_feishu_token(agent_id: uuid.UUID) -> tuple[str, str] | None:
    """Get (app_id, app_access_token) for the agent's configured Feishu channel."""
    status = await _get_feishu_token_status(agent_id)
    if status.get("ok") and status.get("app_id") and status.get("token"):
        return status["app_id"], status["token"]
    if status.get("configured"):
        logger.warning(
            "[Feishu] Token request failed for app_id=%s: %s",
            status.get("app_id"),
            status.get("message"),
        )
    return None


async def _get_feishu_token_status(agent_id: uuid.UUID) -> dict:
    """Return token probe status for diagnostics, never exposing app_secret."""
    auth = await _get_feishu_tool_auth(agent_id)
    if not auth:
        return {
            "configured": False,
            "ok": False,
            "message": "Agent has no Feishu channel configured.",
        }
    try:
        return await _request_feishu_tenant_access_token(auth.app_id, auth.app_secret, platform=auth.platform)
    except Exception as exc:
        return {
            "configured": True,
            "ok": False,
            "app_id": auth.app_id,
            "message": f"{type(exc).__name__}: {exc}",
        }


async def _get_feishu_office_token(agent_id, tool_name):
    """Probe once and retain configured authentication failures as failures."""
    from app.services.feishu_platform import feishu_tool_error

    status = await _get_feishu_token_status(agent_id)
    if status.get("ok") and status.get("app_id") and status.get("token"):
        return status["app_id"], status["token"]
    return feishu_tool_error(tool_name, message=_format_feishu_token_error(status),
        http_status=status.get("http_status"), code=status.get("code"),
        error_class="not_configured" if not status.get("configured") else None,
        platform=resolve_feishu_platform(status["platform"]) if status.get("platform") else None)


async def _request_feishu_tenant_access_token(app_id: str, app_secret: str, *, platform: FeishuPlatform | None = None) -> dict:
    """Call Feishu token API and normalize success/failure details."""
    import httpx
    from app.services.feishu_platform import _redact_diagnostic

    platform = platform or resolve_feishu_platform()
    namespace = {"platform_region": platform.region, "open_api_domain": platform.open_api_domain}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            platform.open_api_url("/auth/v3/tenant_access_token/internal"),
            json={"app_id": app_id, "app_secret": app_secret},
        )

    try:
        data = resp.json()
    except Exception as exc:
        return {
            "configured": True, "platform": namespace,
            "ok": False,
            "app_id": app_id,
            "http_status": resp.status_code,
            "message": f"Invalid Feishu token response: {type(exc).__name__}",
        }

    if not isinstance(data, dict):
        return {"configured": True, "platform": namespace, "ok": False, "app_id": app_id, "http_status": resp.status_code,
            "message": "Invalid Feishu token response: expected an object"}
    token = data.get("tenant_access_token", "")
    if 200 <= resp.status_code < 300 and data.get("code") in (None, 0) and isinstance(token, str) and token:
        return {
            "configured": True, "platform": namespace,
            "ok": True,
            "app_id": app_id,
            "token": FeishuAccessToken(token, platform),
            "http_status": resp.status_code,
            "code": data.get("code"),
            "message": data.get("msg") or "ok",
        }

    return {
        "configured": True, "platform": namespace,
        "ok": False,
        "app_id": app_id,
        "http_status": resp.status_code,
        "code": data.get("code"),
        "message": _redact_diagnostic(data.get("msg") or "tenant_access_token missing", (app_secret,)),
    }


async def _get_agent_calendar_id(token: str) -> tuple[str | None, str | None]:
    """Get (calendar_id, error_msg) for the agent app's primary calendar.

    Returns (calendar_id, None) on success, or (None, human_readable_error) on failure.
    """
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _feishu_api_url(token, "/calendar/v4/calendars/primary"),
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    code = data.get("code", -1)
    if code == 0:
        cals = data.get("data", {}).get("calendars", [])
        if cals:
            cal_id = cals[0].get("calendar", {}).get("calendar_id")
            return cal_id, None
        return None, "日历列表为空，请确认应用有 calendar:calendar 权限并已发布新版本"
    if code == 99991672:
        return None, (
            "❌ 飞书日历权限未开通（错误码 99991672）\n\n"
            "请在飞书开放平台为应用 cli_a9257c5136781ceb 开通以下权限并发布新版本：\n"
            "• calendar:calendar:readonly（应用身份权限）\n"
            "• calendar:calendar.event:create（应用身份权限）\n"
            "• calendar:calendar.event:read（用户身份权限）\n"
            "• calendar:calendar.event:update（用户身份权限）\n"
            "• calendar:calendar.event:delete（用户身份权限）\n\n"
            "开通步骤：飞书开放平台 → 权限管理 → 批量导入权限 → 添加以上权限 → 创建版本 → 确认发布"
        )
    return None, f"获取日历 ID 失败：{data.get('msg')} (code {code})"


async def _feishu_resolve_open_id(token: str, email: str) -> str | None:
    """Resolve a user's open_id from their email."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _feishu_api_url(token, "/contact/v3/users/batch_get_id"),
            json={"emails": [email]},
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id_type": "open_id"},
        )
    data = resp.json()
    if data.get("code") != 0:
        return None
    for u in data.get("data", {}).get("user_list", []):
        oid = u.get("user_id")
        if oid:
            return oid
    return None


def _iso_to_ts(iso_str: str) -> float:
    """Convert ISO 8601 (or near-ISO) string to Unix timestamp.

    LLMs produce varied formats — accept broadly rather than crash.
    """
    from datetime import datetime as _dt, timezone as _tz

    stripped = iso_str.strip()

    # Fast path: already a unix timestamp string
    if stripped.isdigit() and len(stripped) >= 10:
        return float(stripped)

    # Standard ISO 8601 via fromisoformat (handles +08:00, Z, etc.)
    try:
        return _dt.fromisoformat(stripped.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        logger.debug("[_iso_to_ts] fromisoformat failed for %r, trying dateutil", iso_str)

    # Fallback: dateutil handles almost everything LLMs produce
    try:
        from dateutil import parser as _dp

        d = _dp.parse(stripped)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz.utc)
        return d.timestamp()
    except Exception as exc:
        logger.warning("[_iso_to_ts] dateutil also failed for %r: %s", iso_str, exc)

    raise ValueError(f"Cannot parse datetime: {iso_str!r}")
