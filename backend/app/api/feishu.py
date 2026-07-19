"""Feishu OAuth and Channel API routes."""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_rls import load_public_agent_channel_config
from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.security import get_current_user
from app.database import enter_rls_bypass, get_db, pin_rls_tenant_context
from app.api.channel_secrets import resolve_secret_value
from app.channel_message_contracts import prefix_message_with_sender_label
from app.models.channel_config import ChannelConfig
from app.models.identity import SSOScanSession
from app.models.user import User
from app.schemas.schemas import ChannelConfigCreate, ChannelConfigOut, TokenResponse, UserOut
from app.services.auth_provider import feishu_auth_provider
from app.services.feishu_app_registration import (
    FeishuRegistrationConflict,
    FeishuRegistrationError,
    FeishuRegistrationForbidden,
    FeishuRegistrationNotCancellable,
    FeishuRegistrationNotFound,
    FeishuRegistrationStateUnavailable,
    FeishuRegistrationState,
    feishu_app_registration_manager,
)
from app.services.feishu_contacts_cache import upsert_feishu_contact_cache
from app.services.feishu_identity_maintenance import build_feishu_p2p_conv_id, list_legacy_feishu_conv_ids
from app.services.feishu_platform import resolve_feishu_platform
from app.services.feishu_service import feishu_service
from app.services.approval_service import approval_service
from app.services.channel_agent_runtime import call_agent_llm, should_persist_channel_reply_as_assistant

router = APIRouter(tags=["feishu"])


_TOOL_STATUS_KEEP_LINES = 20
_FEISHU_CARD_MARKDOWN_LIMIT = 2800
_APPROVAL_ID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_APPROVE_TEXT_PATTERN = r"^\s*(放行|批准|同意|通过|approve|approved)(?:\s|[!！。,.，]|$)"
_REJECT_TEXT_PATTERN = r"^\s*(拒绝|驳回|不通过|reject|rejected|deny|denied)(?:\s|[!！。,.，]|$)"
_NEW_SESSION_TEXT_PATTERN = r"^\s*/(?:new|reset|restart|新会话|新建会话)(?:\s|$)"


def _is_feishu_new_session_command(text: str) -> bool:
    import re

    return bool(re.search(_NEW_SESSION_TEXT_PATTERN, text or "", re.IGNORECASE))


def _feishu_platform_extra(config) -> dict:
    return dict(getattr(config, "extra_config", None) or {})


def _parse_feishu_text_approval_action(text: str) -> str | None:
    import re

    if re.search(_APPROVE_TEXT_PATTERN, text or "", re.IGNORECASE):
        return "approve"
    if re.search(_REJECT_TEXT_PATTERN, text or "", re.IGNORECASE):
        return "reject"
    return None


async def _try_resolve_feishu_text_approval(
    *,
    db: AsyncSession,
    agent,
    user_text: str,
    resolved_user,
    config,
    receive_id: str,
    receive_id_type: str,
) -> dict | None:
    """Resolve pending approvals from trusted Feishu text commands before invoking the agent."""
    import json
    import re

    action = _parse_feishu_text_approval_action(user_text)
    if not action:
        return None

    def _reply(text: str):
        return feishu_service.send_message(
            config.app_id,
            config.app_secret,
            receive_id,
            "text",
            json.dumps({"text": text}, ensure_ascii=False),
            receive_id_type=receive_id_type,
            stage="approval_text_reply",
            extra_config=_feishu_platform_extra(config),
        )

    if not agent:
        await _reply("审批处理失败：未找到当前数字员工。")
        return {"code": 0, "msg": "approval agent missing"}
    if not resolved_user:
        await _reply("审批处理失败：无法确认你的 Hive 用户身份，请先绑定飞书账号或到 Web 端处理。")
        return {"code": 0, "msg": "approval user unresolved"}

    from app.models.audit import ApprovalRequest

    approval_uuid = None
    match = re.search(_APPROVAL_ID_PATTERN, user_text or "")
    if match:
        approval_uuid = uuid.UUID(match.group(0))

    query = select(ApprovalRequest).where(
        ApprovalRequest.agent_id == agent.id,
        ApprovalRequest.status == "pending",
    )
    if approval_uuid:
        query = query.where(ApprovalRequest.id == approval_uuid)
    query = query.order_by(ApprovalRequest.created_at.desc()).limit(1)

    result = await db.execute(query)
    pending = result.scalars().all()
    approval = pending[0] if pending else None
    if not approval:
        await _reply("没有找到这个数字员工当前待处理的审批。")
        return {"code": 0, "msg": "approval not found"}

    try:
        resolved = await approval_service.resolve_approval(db, approval.id, resolved_user, action)
        await db.commit()
    except ValueError as exc:
        await _reply(f"审批处理失败：{exc}")
        return {"code": 0, "msg": "approval resolve failed"}

    status_text = "已批准" if action == "approve" else "已拒绝"
    await _reply(f"{status_text}：{resolved.action_type}")
    return {"code": 0, "msg": "approval resolved"}


class _SerialPatchQueue:
    """Serialize patch requests for one Feishu message to prevent out-of-order overwrite."""

    def __init__(self):
        self._tail: asyncio.Task | None = None

    def enqueue(self, job_factory: Callable[[], Awaitable[None]]) -> None:
        prev = self._tail

        async def _runner():
            if prev:
                try:
                    await prev
                except Exception as exc:
                    logger.warning(f"[Feishu] Previous patch job failed before next job: {exc}")
            await job_factory()

        self._tail = asyncio.create_task(_runner())

    async def drain(self) -> None:
        if self._tail:
            await self._tail


def _split_feishu_markdown_text(text: str, *, limit: int = _FEISHU_CARD_MARKDOWN_LIMIT) -> list[str]:
    """Split long Feishu markdown payloads into smaller chunks without dropping content."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []

    chunks: list[str] = []
    current = ""

    def _flush_current() -> None:
        nonlocal current
        if current:
            chunks.append(current)
            current = ""

    def _split_oversized_piece(piece: str) -> list[str]:
        if len(piece) <= limit:
            return [piece]

        segments: list[str] = []
        start = 0
        preferred_breaks = ("\n", " ", "，", "。", ",", ".", "；", ";", "：", ":")
        min_break_index = max(1, int(limit * 0.6))
        while start < len(piece):
            end = min(start + limit, len(piece))
            if end >= len(piece):
                segments.append(piece[start:end])
                break

            window = piece[start:end]
            break_at = -1
            for token in preferred_breaks:
                idx = window.rfind(token)
                if idx > break_at:
                    break_at = idx
            if break_at >= min_break_index:
                end = start + break_at + 1
            segments.append(piece[start:end])
            start = end
        return segments

    for piece in text.splitlines(keepends=True) or [text]:
        if len(piece) > limit:
            _flush_current()
            chunks.extend(_split_oversized_piece(piece))
            continue

        if not current:
            current = piece
            continue

        if len(current) + len(piece) <= limit:
            current += piece
            continue

        _flush_current()
        current = piece

    _flush_current()
    return chunks


def _feishu_markdown_elements(
    text: str,
    *,
    limit: int = _FEISHU_CARD_MARKDOWN_LIMIT,
    text_size: str | None = None,
) -> list[dict]:
    """Render markdown text as one or more Feishu card markdown elements."""
    normalized = text or "..."
    elements: list[dict] = []
    for chunk in _split_feishu_markdown_text(normalized, limit=limit):
        element = {"tag": "markdown", "content": chunk}
        if text_size:
            element["text_size"] = text_size
        elements.append(element)
    return elements or [{"tag": "markdown", "content": "..."}]


def _is_cardkit_stream_closed_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "streaming mode is closed" in msg or ("code=300309" in msg and "stream" in msg)


def _cache_feishu_sender(agent_id: uuid.UUID, *, open_id: str, user_id: str, name: str, email: str) -> None:
    """Persist a tiny sender cache for downstream name lookup tools."""
    if not open_id or not name:
        return

    try:
        upsert_feishu_contact_cache(
            agent_id,
            open_id=open_id,
            user_id=user_id,
            name=name,
            email=email,
        )
    except Exception as exc:
        logger.error(f"[Feishu] Cache write failed: {exc}")


async def _resolve_feishu_sender_profile(
    agent_id: uuid.UUID,
    *,
    config,
    sender_open_id: str,
    sender_user_id: str = "",
) -> dict:
    """Resolve a normalized Feishu sender profile for inbound message routing."""
    profile = {
        "user_id": sender_user_id or None,
        "open_id": sender_open_id or None,
        "union_id": None,
        "name": "",
        "email": "",
        "avatar_url": "",
        "mobile": "",
        "raw_profile": {},
    }

    if not sender_open_id:
        return profile

    try:
        user_info = await feishu_service.get_contact_user_by_open_id(
            config.app_id,
            config.app_secret,
            sender_open_id,
            stage="resolve_sender_profile",
            extra_config=_feishu_platform_extra(config),
        )
        if user_info:
            profile.update(
                {
                    "user_id": user_info.get("user_id") or profile["user_id"],
                    "open_id": user_info.get("open_id") or profile["open_id"],
                    "union_id": user_info.get("union_id"),
                    "name": user_info.get("name", ""),
                    "email": user_info.get("email", "") or user_info.get("enterprise_email", ""),
                    "avatar_url": user_info.get("avatar_url", ""),
                    "mobile": user_info.get("mobile", ""),
                    "raw_profile": user_info,
                }
            )
            logger.info(f"[Feishu] Resolved sender profile: {profile['name']} (user_id={profile['user_id'] or ''})")
            _cache_feishu_sender(
                agent_id,
                open_id=profile["open_id"] or "",
                user_id=profile["user_id"] or "",
                name=profile["name"] or "",
                email=profile["email"] or "",
            )
    except Exception as exc:
        logger.error(f"[Feishu] Failed to resolve sender profile: {exc}")

    return profile


# ─── OAuth ──────────────────────────────────────────────


async def _resolve_feishu_oauth_authorize_config(
    db: AsyncSession, tenant_id: uuid.UUID | None
) -> tuple[str, str, dict]:
    """Resolve the OAuth app and callback URL for authorize-url generation."""
    try:
        config = await feishu_auth_provider.get_oauth_config(db, tenant_id)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feishu SSO not configured on this deployment",
        ) from exc

    app_id = (config.get("app_id") or "").strip()
    redirect_uri = (config.get("redirect_uri") or "").strip()
    if not app_id or not redirect_uri:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Feishu SSO not configured on this deployment",
        )
    return app_id, redirect_uri, config


async def _load_public_sso_scan_session(db: AsyncSession, session_id: uuid.UUID) -> SSOScanSession | None:
    """Load a public SSO scan session by unguessable state and pin its tenant."""
    async with enter_rls_bypass(
        db,
        reason=f"public feishu sso session lookup for {session_id}",
    ) as bypass_db:
        session = await bypass_db.get(SSOScanSession, session_id)

    if session and session.tenant_id:
        await pin_rls_tenant_context(db, session.tenant_id)
    return session


@router.get("/auth/feishu/sso/available")
async def feishu_sso_available():
    """Check if Feishu SSO login is available (app configured)."""
    from app.config import get_settings

    _settings = get_settings()
    return {"available": bool(_settings.FEISHU_APP_ID)}


@router.post("/auth/feishu/sso/init")
async def feishu_sso_init(
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Create an SSO scan session and return the Feishu authorize URL."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlencode

    if tenant_id:
        await pin_rls_tenant_context(db, tenant_id)
    app_id, redirect_uri, oauth_config = await _resolve_feishu_oauth_authorize_config(db, tenant_id)

    session = SSOScanSession(
        id=uuid.uuid4(),
        status="pending",
        tenant_id=tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db.add(session)
    await db.commit()

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": str(session.id),
    }
    authorize_url = f"{resolve_feishu_platform(oauth_config).oauth_authorize_url}?{urlencode(params)}"

    return {"session_id": str(session.id), "authorize_url": authorize_url}


@router.get("/auth/feishu/sso/poll")
async def feishu_sso_poll(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Poll SSO session status. Returns token when completed."""
    session = await _load_public_sso_scan_session(db, session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    from datetime import datetime, timezone

    if session.expires_at < datetime.now(timezone.utc):
        return {"status": "expired"}

    if session.status == "completed" and session.access_token and session.user_id:
        user = await db.get(User, session.user_id)
        if not user:
            return {"status": "error", "detail": "User not found"}
        return {
            "status": "completed",
            "access_token": session.access_token,
            "user": UserOut.model_validate(user).model_dump(),
        }

    return {"status": session.status}


@router.post("/auth/feishu/bind/init")
async def feishu_bind_init(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an SSO session for binding Feishu to an existing account."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlencode

    app_id, redirect_uri, oauth_config = await _resolve_feishu_oauth_authorize_config(db, current_user.tenant_id)

    session = SSOScanSession(
        id=uuid.uuid4(),
        status="pending_bind",
        user_id=current_user.id,
        tenant_id=current_user.tenant_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db.add(session)
    await db.commit()

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": str(session.id),
    }
    authorize_url = f"{resolve_feishu_platform(oauth_config).oauth_authorize_url}?{urlencode(params)}"
    return {"session_id": str(session.id), "authorize_url": authorize_url}


@router.get("/auth/feishu/callback", response_class=HTMLResponse)
async def feishu_oauth_callback_get(code: str, state: str, db: AsyncSession = Depends(get_db)):
    """Browser/scan callback that completes an SSO scan session."""
    try:
        session_id = uuid.UUID(state)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid scan session state") from exc

    session = await _load_public_sso_scan_session(db, session_id)
    if not session:
        return HTMLResponse("<html><body>Invalid or expired SSO session.</body></html>", status_code=400)

    try:
        if session.status == "pending_bind" and session.user_id:
            # Bind mode: link feishu to existing user
            existing_user = await db.get(User, session.user_id)
            if not existing_user:
                return HTMLResponse("<html><body>User not found.</body></html>", status_code=400)
            user = await feishu_auth_provider.bind_with_code(db, user=existing_user, code=code)
            token = ""  # not needed for bind
        else:
            # Login mode: authenticate or create user
            user, token = await feishu_auth_provider.authenticate_with_code(db, code=code, tenant_id=session.tenant_id)
        session.status = "completed"
        session.provider_type = "feishu"
        session.user_id = user.id
        session.access_token = token or ""
        session.error_msg = None
        await db.commit()
    except Exception as exc:
        session.status = "expired"
        session.provider_type = "feishu"
        session.error_msg = str(exc)
        await db.commit()
        return HTMLResponse("<html><body>Feishu SSO failed.</body></html>", status_code=400)

    redirect_target = f"/sso/entry?sid={session.id}&complete=1"
    return HTMLResponse(
        f"<html><head><meta http-equiv='refresh' content='0; url={redirect_target}' /></head>"
        f"<body>Redirecting to <a href='{redirect_target}'>{redirect_target}</a>...</body></html>"
    )


@router.post("/auth/feishu/callback", response_model=TokenResponse)
async def feishu_oauth_callback(
    code: str,
    tenant_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle Feishu OAuth callback — exchange code for user session."""
    try:
        if tenant_id:
            await pin_rls_tenant_context(db, tenant_id)
        user, token = await feishu_auth_provider.authenticate_with_code(db, code=code, tenant_id=tenant_id)
        await db.commit()
    except Exception:
        logger.exception("Feishu auth failed")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Feishu authentication failed")

    return TokenResponse(access_token=token, user=UserOut.model_validate(user))


@router.post("/auth/feishu/bind")
async def bind_feishu_account(
    code: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bind Feishu account to existing user."""
    user = await feishu_auth_provider.bind_with_code(db, user=current_user, code=code)
    await db.commit()
    return UserOut.model_validate(user)


# ─── Channel Config (per-agent Feishu bot) ──────────────


class FeishuAppRegistrationStart(BaseModel):
    platform_region: Literal["feishu_cn", "lark_global"]


class FeishuAppRegistrationOut(BaseModel):
    session_id: str
    status: str
    platform_region: Literal["feishu_cn", "lark_global"]
    resolved_platform_region: Literal["feishu_cn", "lark_global"] | None = None
    verification_url: str | None = None
    qr_expires_at: str | None = None
    connection_status: str | None = None
    message: str | None = None
    error_code: str | None = None
    connected: bool
    cancellable: bool
    created_at: str
    updated_at: str


def _registration_http_error(exc: FeishuRegistrationError) -> HTTPException:
    if isinstance(exc, (FeishuRegistrationNotFound, FeishuRegistrationForbidden)):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, (FeishuRegistrationConflict, FeishuRegistrationNotCancellable)):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(exc, FeishuRegistrationStateUnavailable):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif exc.code == "invalid_platform_region":
        status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _registration_response(state: FeishuRegistrationState) -> FeishuAppRegistrationOut:
    return FeishuAppRegistrationOut(**state.to_public_dict())


def _disable_registration_response_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


async def _reconcile_registration_channel_status(
    db: AsyncSession,
    state: FeishuRegistrationState,
) -> FeishuRegistrationState:
    if state.status not in {"credentials_received", "connecting"}:
        return state
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == uuid.UUID(state.agent_id),
            ChannelConfig.channel_type == "feishu",
        )
    )
    config = result.scalar_one_or_none()
    if config is None:
        return state
    extra_config = dict(config.extra_config or {})
    if str(extra_config.get("registration_session_id") or "") != state.session_id:
        return state
    return await feishu_app_registration_manager.reconcile_channel_status(
        state,
        is_connected=bool(config.is_connected),
        is_configured=bool(config.is_configured),
        connection_status=str(extra_config.get("connection_status") or "") or None,
    )


@router.post(
    "/agents/{agent_id}/channel/registration/start",
    response_model=FeishuAppRegistrationOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_feishu_app_registration(
    agent_id: uuid.UUID,
    data: FeishuAppRegistrationStart,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start the official Agent Detail QR flow for Feishu or Lark."""
    _disable_registration_response_cache(response)
    agent = await require_agent_manage_access(db, current_user, agent_id)
    try:
        registration = await feishu_app_registration_manager.start_registration(
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            actor_user_id=current_user.id,
            platform_region=data.platform_region,
            agent_name=agent.name,
        )
    except FeishuRegistrationError as exc:
        raise _registration_http_error(exc) from exc
    return _registration_response(registration)


@router.get(
    "/agents/{agent_id}/channel/registration/active",
    response_model=FeishuAppRegistrationOut,
)
async def get_active_feishu_app_registration(
    agent_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resume the current user's active registration after a page reload."""
    _disable_registration_response_cache(response)
    agent = await require_agent_manage_access(db, current_user, agent_id)
    try:
        registration = await feishu_app_registration_manager.get_active_registration(
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            actor_user_id=current_user.id,
        )
        registration = await _reconcile_registration_channel_status(db, registration)
    except FeishuRegistrationError as exc:
        raise _registration_http_error(exc) from exc
    return _registration_response(registration)


@router.get(
    "/agents/{agent_id}/channel/registration/{session_id}",
    response_model=FeishuAppRegistrationOut,
)
async def get_feishu_app_registration(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll one actor-bound registration and reconcile real WebSocket truth."""
    _disable_registration_response_cache(response)
    agent = await require_agent_manage_access(db, current_user, agent_id)
    try:
        registration = await feishu_app_registration_manager.get_registration_for_actor(
            session_id,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            actor_user_id=current_user.id,
        )
        registration = await _reconcile_registration_channel_status(db, registration)
    except FeishuRegistrationError as exc:
        raise _registration_http_error(exc) from exc
    return _registration_response(registration)


@router.post(
    "/agents/{agent_id}/channel/registration/{session_id}/cancel",
    response_model=FeishuAppRegistrationOut,
)
async def cancel_feishu_app_registration(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a pre-credential QR flow using a Redis compare-and-swap fence."""
    _disable_registration_response_cache(response)
    agent = await require_agent_manage_access(db, current_user, agent_id)
    try:
        registration = await feishu_app_registration_manager.cancel_registration(
            session_id,
            tenant_id=agent.tenant_id,
            agent_id=agent_id,
            actor_user_id=current_user.id,
        )
    except FeishuRegistrationError as exc:
        raise _registration_http_error(exc) from exc
    return _registration_response(registration)


@router.post("/agents/{agent_id}/channel", response_model=ChannelConfigOut, status_code=status.HTTP_201_CREATED)
async def configure_channel(
    agent_id: uuid.UUID,
    data: ChannelConfigCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Feishu bot credentials for a digital employee (wizard step 5)."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    # Check existing
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "feishu",
        )
    )
    existing = result.scalar_one_or_none()
    existing_extra = existing.extra_config if existing else {}
    incoming_extra = {**(existing_extra or {}), **(data.extra_config or {})}
    incoming_extra["platform_region"] = resolve_feishu_platform(incoming_extra).region
    incoming_extra["setup_method"] = "manual"
    incoming_extra["identity_status"] = "unverified_manual"
    incoming_extra.pop("registration_session_id", None)
    connection_mode = incoming_extra.get("connection_mode", existing_extra.get("connection_mode", "webhook"))
    app_id = resolve_secret_value(data.app_id, existing.app_id if existing else None, preserve_missing=True)
    app_secret = resolve_secret_value(data.app_secret, existing.app_secret if existing else None, preserve_missing=True)
    encrypt_key = resolve_secret_value(
        data.encrypt_key, existing.encrypt_key if existing else None, preserve_missing=True
    )
    verification_token = resolve_secret_value(
        data.verification_token,
        existing.verification_token if existing else None,
        preserve_missing=True,
    )
    if not app_id:
        raise HTTPException(status_code=422, detail="app_id is required")
    if not app_secret:
        raise HTTPException(status_code=422, detail="app_secret is required")
    if connection_mode == "webhook" and not encrypt_key and not verification_token:
        raise HTTPException(status_code=422, detail="Webhook mode requires encrypt_key or verification_token")
    if existing:
        from app.services.external_principal_service import revoke_channel_config_external_principals

        await revoke_channel_config_external_principals(
            db,
            tenant_id=agent.tenant_id,
            config=existing,
            actor_user_id=current_user.id,
            reason="Feishu/Lark channel credentials changed through manual configuration",
        )
        existing.self_identity_user_id = None
        existing.self_identity_verified_at = None
        existing.app_id = app_id
        existing.app_secret = app_secret
        existing.encrypt_key = encrypt_key
        existing.verification_token = verification_token
        existing.extra_config = incoming_extra or existing.extra_config or {}
        existing.is_configured = True
        await db.flush()

        # Start/Stop WS client in background
        from app.services.feishu_ws import feishu_ws_manager
        import asyncio

        mode = existing.extra_config.get("connection_mode", "webhook")
        if mode == "websocket":
            asyncio.create_task(
                feishu_ws_manager.start_client(
                    agent_id,
                    existing.app_id,
                    existing.app_secret,
                    extra_config=existing.extra_config,
                )
            )
        else:
            asyncio.create_task(feishu_ws_manager.stop_client(agent_id))

        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        channel_type=data.channel_type,
        app_id=app_id,
        app_secret=app_secret,
        encrypt_key=encrypt_key,
        verification_token=verification_token,
        extra_config=incoming_extra,
        is_configured=True,
    )
    db.add(config)
    await db.flush()

    # Start WS client in background
    from app.services.feishu_ws import feishu_ws_manager
    import asyncio

    mode = config.extra_config.get("connection_mode", "webhook")
    if mode == "websocket":
        asyncio.create_task(
            feishu_ws_manager.start_client(
                agent_id,
                config.app_id,
                config.app_secret,
                extra_config=config.extra_config,
            )
        )

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/channel", response_model=ChannelConfigOut)
async def get_channel_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get Feishu channel configuration for an agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "feishu",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    return ChannelConfigOut.model_validate(config).to_safe()


@router.get("/agents/{agent_id}/channel/webhook-url")
async def get_webhook_url(agent_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Get the webhook URL for this agent's Feishu bot."""
    import os
    from app.models.system_settings import SystemSetting

    # Priority: system_settings > env var > request.base_url
    public_base = ""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "platform"))
    setting = result.scalar_one_or_none()
    if setting and setting.value.get("public_base_url"):
        public_base = setting.value["public_base_url"].rstrip("/")
    if not public_base:
        public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base:
        public_base = str(request.base_url).rstrip("/")
    return {"webhook_url": f"{public_base}/api/channel/feishu/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/channel", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove Feishu bot configuration for an agent."""
    agent = await require_agent_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "feishu",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Channel not configured")
    from app.services.external_principal_service import revoke_channel_config_external_principals
    from app.services.feishu_ws import feishu_ws_manager

    await revoke_channel_config_external_principals(
        db,
        tenant_id=agent.tenant_id,
        config=config,
        actor_user_id=current_user.id,
        reason="Feishu/Lark channel configuration deleted",
    )
    await db.delete(config)
    await db.flush()
    await feishu_ws_manager.stop_client(agent_id)


# ─── Feishu Event Webhook ───────────────────────────────


def _verify_feishu_signature(encrypt_key: str, timestamp: str, nonce: str, body_str: str, signature: str) -> bool:
    """Verify Feishu webhook X-Lark-Signature using HMAC-SHA256."""
    import hashlib
    import hmac

    content = timestamp + nonce + encrypt_key + body_str
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, signature)


def _decrypt_feishu_payload(encrypt_key: str, encrypted_text: str) -> dict:
    """Decrypt a Feishu webhook payload configured with Encrypt Key."""
    import base64
    import hashlib
    import json

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    encrypted = base64.b64decode(encrypted_text)
    iv = encrypted[:16]
    ciphertext = encrypted[16:]
    aes_key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    cipher = AES.new(aes_key, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(ciphertext), AES.block_size)
    return json.loads(plaintext.decode("utf-8"))


@router.post("/channel/feishu/{agent_id}/webhook")
async def feishu_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Feishu event callback for a specific agent's bot."""
    body_bytes = await request.body()
    body_str = body_bytes.decode("utf-8")
    import json as _json_wb

    body = _json_wb.loads(body_str)

    config = await load_public_agent_channel_config(db, agent_id=agent_id, channel_type="feishu")
    if config is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    if config and config.encrypt_key:
        sig = request.headers.get("X-Lark-Signature", "")
        ts = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        if not sig or not _verify_feishu_signature(config.encrypt_key, ts, nonce, body_str, sig):
            logger.warning(f"[Feishu] Invalid signature for agent {agent_id}")
            raise HTTPException(status_code=403, detail="Invalid signature")
        if "encrypt" in body:
            try:
                body = _decrypt_feishu_payload(config.encrypt_key, body["encrypt"])
            except Exception as exc:
                logger.warning(f"[Feishu] Failed to decrypt webhook for agent {agent_id}: {exc}")
                raise HTTPException(status_code=400, detail="Invalid encrypted payload") from exc
    elif config and config.verification_token:
        if body.get("token") != config.verification_token:
            logger.warning(f"[Feishu] Verification token mismatch for agent {agent_id}")
            raise HTTPException(status_code=403, detail="Invalid verification token")

    # Handle verification challenge after signature / token verification
    if "challenge" in body:
        return {"challenge": body["challenge"]}

    from app.services.channel_ingress_inbox import accept_authenticated_channel_event, channel_installation_ref

    await accept_authenticated_channel_event(
        db,
        tenant_id=config.tenant_id,
        agent_id=agent_id,
        provider="feishu",
        installation_ref=channel_installation_ref(config, fallback=f"feishu:{agent_id}"),
        provider_event_id=str(body.get("header", {}).get("event_id") or ""),
        handler_key="feishu.event",
        body=body,
        metadata={"transport": "webhook"},
    )
    return {"code": 0, "msg": "accepted"}


# ─── Feishu Interactive Card Callback ────────────────────


@router.post("/channel/feishu/card-callback")
async def feishu_card_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Feishu interactive card button clicks (approve/reject approval requests).

    Feishu sends a POST when a user clicks an action button on an interactive card.
    We verify the action, resolve the approval, and return an updated card.

    Authentication: Feishu card callbacks are authenticated by the combination of:
      1. Unguessable approval_id (UUID) — only holders of the original card know it
      2. feishu_open_id verified against platform user database (must match an existing user)
      3. Only pending approvals can be resolved (resolve_approval checks status)
    For additional security, verify X-Lark-Signature when encrypt_key is configured.
    """
    import json as _json

    body = await request.json()
    logger.info(f"[Feishu] Card callback received: {_json.dumps(body, ensure_ascii=False)[:500]}")

    # Extract action value from card callback
    action = body.get("action", {})
    action_value_str = action.get("value", "{}")
    try:
        action_value = _json.loads(action_value_str) if isinstance(action_value_str, str) else action_value_str
    except _json.JSONDecodeError:
        action_value = {}

    approval_id_str = action_value.get("approval_id")
    action_type = action_value.get("action")  # "approve" or "reject"

    if not approval_id_str or not action_type:
        return {"toast": {"type": "error", "content": "Invalid action"}}

    # Resolve the approval
    try:
        from app.services.approval_service import approval_service
        from app.models.agent import Agent as AgentModel
        from app.models.audit import ApprovalRequest

        approval_uuid = uuid.UUID(approval_id_str)
        async with enter_rls_bypass(
            db,
            reason=f"public feishu card approval lookup for {approval_uuid}",
        ) as bypass_db:
            approval_result = await bypass_db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == approval_uuid)
            )
            approval_record = approval_result.scalar_one_or_none()
            if not approval_record:
                return {"toast": {"type": "error", "content": "Approval not found"}}

            agent_result = await bypass_db.execute(select(AgentModel).where(AgentModel.id == approval_record.agent_id))
            agent = agent_result.scalar_one_or_none()
            tenant_id = getattr(approval_record, "tenant_id", None) or (agent.tenant_id if agent else None)

        if tenant_id is None:
            return {"toast": {"type": "error", "content": "Approval tenant not found"}}
        await pin_rls_tenant_context(db, tenant_id)
        if getattr(approval_record, "tenant_id", None) is None:
            approval_record.tenant_id = tenant_id

        # Resolve the clicker only through this Agent's provider-proven binding.
        open_id = body.get("open_id", "")
        operator = body.get("operator", {})
        feishu_open_id = operator.get("open_id", open_id)
        from app.models.external_principal import ExternalPrincipal
        from app.services.external_principal_service import load_external_runtime_actor

        config = await db.scalar(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == approval_record.agent_id,
                ChannelConfig.tenant_id == tenant_id,
                ChannelConfig.channel_type == "feishu",
            )
        )
        principal = None
        if config is not None and feishu_open_id:
            principal = await db.scalar(
                select(ExternalPrincipal).where(
                    ExternalPrincipal.tenant_id == tenant_id,
                    ExternalPrincipal.provider == "feishu",
                    ExternalPrincipal.installation_ref == str(config.id),
                    ExternalPrincipal.subject_id == feishu_open_id,
                    ExternalPrincipal.status == "active",
                )
            )
        runtime_actor = None
        if principal is not None and principal.linked_user_id is not None:
            runtime_actor = await load_external_runtime_actor(
                db,
                tenant_id=tenant_id,
                principal_id=principal.id,
                expected_user_id=principal.linked_user_id,
            )
        if runtime_actor is None or not runtime_actor.authority_bound or runtime_actor.id is None:
            return {
                "toast": {
                    "type": "error",
                    "content": "Identity is not bound. Scan again from the Agent channel page or use the web platform.",
                },
            }
        user = await db.get(User, runtime_actor.id)
        if user is None:
            return {"toast": {"type": "error", "content": "Bound user is no longer active."}}

        approval = await approval_service.resolve_approval(db, approval_uuid, user, action_type)

        # Audit event
        try:
            from app.core.policy import write_audit_event

            await write_audit_event(
                db,
                event_type="approval.resolved_via_feishu",
                severity="warn",
                actor_type="user",
                actor_id=user.id,
                tenant_id=user.tenant_id or uuid.UUID(int=0),
                action=f"feishu_card_{action_type}",
                resource_type="approval",
                resource_id=approval.id,
                details={"action_type": approval.action_type, "feishu_open_id": feishu_open_id},
            )
        except Exception:
            logger.warning("Audit write failed for approval.resolved_via_feishu", exc_info=True)

        await db.commit()

        status_emoji = "✅" if action_type == "approve" else "❌"
        status_text = "已批准" if action_type == "approve" else "已拒绝"

        # Return updated card (replaces the original card)
        return {
            "toast": {"type": "success", "content": f"{status_text}"},
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "template": "green" if action_type == "approve" else "red",
                    "title": {"tag": "plain_text", "content": f"{status_emoji} 审批已处理"},
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**操作**: {approval.action_type}\n**结果**: {status_text}\n**处理人**: {user.display_name}",
                        },
                    },
                ],
            },
        }

    except ValueError as e:
        return {"toast": {"type": "error", "content": str(e)}}
    except Exception as e:
        logger.error(f"[Feishu] Card callback failed: {e}", exc_info=True)
        return {"toast": {"type": "error", "content": "Processing failed, please try again"}}


async def process_feishu_event(agent_id: uuid.UUID, body: dict, db: AsyncSession, *, tenant_channel_config=None):
    """Core logic to process feishu events from both webhook and WS client.

    Args:
        tenant_channel_config: If provided, use tenant-level credentials instead of
            per-agent ChannelConfig (Phase 6 enterprise webhook path).
    """
    logger.info(
        f"[Feishu] Event processing for {agent_id}: event_type={body.get('header', {}).get('event_type', 'N/A')}"
    )

    # Get channel config — use tenant-level config if provided (Phase 6), else per-agent
    if tenant_channel_config:
        config = tenant_channel_config
    else:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "feishu",
            )
        )
        config = result.scalar_one_or_none()
    if not config:
        return {"code": 1, "msg": "Channel not found"}

    # Handle events
    event = body.get("event", {})
    event_type = body.get("header", {}).get("event_type", "")

    if event_type == "card.action.trigger":

        class _CardActionRequest:
            def __init__(self, payload: dict):
                self._payload = payload

            async def json(self):
                return self._payload

        return await feishu_card_callback(_CardActionRequest(event), db)

    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        sender = event.get("sender", {}).get("sender_id", {})
        sender_open_id = sender.get("open_id", "")
        sender_user_id_from_event = sender.get("user_id", "")  # tenant-stable ID, available directly in event body
        msg_type = message.get("message_type", "text")
        chat_type = message.get("chat_type", "p2p")  # p2p or group
        chat_id = message.get("chat_id", "")

        logger.info(f"[Feishu] Received {msg_type} message, chat_type={chat_type}, from={sender_open_id}")

        # ── Normalize post (rich text) → extract text + schedule image downloads ──
        if msg_type == "post":
            import json as _json_post

            _post_body = _json_post.loads(message.get("content", "{}"))
            # Feishu post content: {"title": "...", "content": [[{"tag":"text","text":"..."},...],...]}
            # The content may be nested under a locale key like "zh_cn"
            _paragraphs = _post_body.get("content", [])
            if not _paragraphs:
                # Try locale keys (zh_cn, en_us, etc.)
                for _locale_key, _locale_val in _post_body.items():
                    if isinstance(_locale_val, dict) and "content" in _locale_val:
                        _paragraphs = _locale_val["content"]
                        break
            _text_parts = []
            _post_image_keys = []
            for _para in _paragraphs:
                _line_parts = []
                for _elem in _para:
                    _tag = _elem.get("tag")
                    if _tag == "text":
                        _line_parts.append(_elem.get("text", ""))
                    elif _tag == "a":
                        _href = _elem.get("href", "")
                        _link_text = _elem.get("text", "")
                        _line_parts.append(f"{_link_text} ({_href})" if _href else _link_text)
                    elif _tag == "img":
                        _ik = _elem.get("image_key", "")
                        if _ik:
                            _post_image_keys.append(_ik)
                if _line_parts:
                    _text_parts.append("".join(_line_parts))
            _extracted_text = "\n".join(_text_parts).strip()
            # Download images and embed as base64 for vision-capable models
            _image_markers = []
            if _post_image_keys:
                import base64 as _b64

                _msg_id = message.get("message_id", "")
                from pathlib import Path as _PostPath
                from app.config import get_settings as _post_gs

                _post_settings = _post_gs()
                _upload_dir = _PostPath(_post_settings.AGENT_DATA_DIR) / str(agent_id) / "workspace" / "uploads"
                _upload_dir.mkdir(parents=True, exist_ok=True)
                for _ik in _post_image_keys:
                    try:
                        _img_bytes = await feishu_service.download_message_resource(
                            config.app_id,
                            config.app_secret,
                            _msg_id,
                            _ik,
                            "image",
                            extra_config=_feishu_platform_extra(config),
                        )
                        # Save to workspace
                        _save_path = _upload_dir / f"image_{_ik[-8:]}.jpg"
                        _save_path.write_bytes(_img_bytes)
                        logger.info(f"[Feishu] Saved post image to {_save_path} ({len(_img_bytes)} bytes)")
                        # Embed as base64 marker for vision models
                        _b64_data = _b64.b64encode(_img_bytes).decode("ascii")
                        _image_markers.append(f"[image_data:data:image/jpeg;base64,{_b64_data}]")
                    except Exception as _dl_err:
                        logger.error(f"[Feishu] Failed to download post image {_ik}: {_dl_err}")
            # Build final text with embedded images
            if not _extracted_text and _image_markers:
                _extracted_text = "[用户发送了图片，请看图片内容]"
            _final_content = _extracted_text
            if _image_markers:
                _final_content += "\n" + "\n".join(_image_markers)
            # Rewrite as text message so existing handler processes it
            message["content"] = _json_post.dumps({"text": _final_content})
            msg_type = "text"
            logger.info(f"[Feishu] Normalized post → text='{_extracted_text[:100]}', images={len(_image_markers)}")

        if msg_type in ("file", "image"):
            import asyncio as _asyncio

            _asyncio.create_task(
                _handle_feishu_file(
                    db,
                    agent_id,
                    config,
                    message,
                    sender_open_id,
                    sender_user_id_from_event,
                    chat_type,
                    chat_id,
                )
            )
            return {"code": 0, "msg": "ok"}

        if msg_type == "text":
            import json
            import re

            content = json.loads(message.get("content", "{}"))
            user_text = content.get("text", "")

            # Strip @mention tags (e.g. @_user_1) from group messages
            user_text = re.sub(r"@_user_\d+", "", user_text).strip()

            if not user_text:
                return {"code": 0, "msg": "empty message after stripping mentions"}

            # Load recent conversation history via session (session UUID may already exist)
            from app.models.audit import ChatMessage
            from app.models.agent import Agent as AgentModel
            from app.models.chat_session import ChatSession
            from app.services.channel_session import find_or_create_channel_session, start_new_channel_session

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            if agent_obj is None:
                logger.error(f"[Feishu] Agent not found while resolving sender identity: agent_id={agent_id}")
                return {"code": 1, "msg": "Agent not found"}
            agent_tenant_id = agent_obj.tenant_id
            sender_profile = await _resolve_feishu_sender_profile(
                agent_id,
                config=config,
                sender_open_id=sender_open_id,
                sender_user_id=sender_user_id_from_event,
            )
            sender_name = sender_profile.get("name", "")
            sender_user_id_feishu = sender_profile.get("user_id", "") or sender_user_id_from_event
            sender_subject_id = (
                sender_profile.get("open_id")
                or sender_open_id
                or sender_user_id_feishu
                or sender_profile.get("email")
                or ""
            )

            if chat_type == "group" and chat_id:
                conv_id = f"feishu_group_{chat_id}_{sender_subject_id}"
                legacy_conv_ids: list[str] = []
            else:
                conv_id = (
                    build_feishu_p2p_conv_id(sender_user_id_feishu, sender_open_id) or f"feishu_p2p_{sender_open_id}"
                )
                legacy_conv_ids = list_legacy_feishu_conv_ids(sender_open_id, conv_id)
            delivery_target = {
                "channel": "feishu",
                "receive_id": chat_id if chat_type == "group" and chat_id else sender_open_id,
                "receive_id_type": "chat_id" if chat_type == "group" and chat_id else "open_id",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "open_id": sender_open_id,
                "user_id": sender_user_id_feishu,
                "user_label": sender_name,
            }

            # Resolve identity before history/LLM work so Feishu text approvals are handled in-channel.
            if not (sender_profile.get("open_id") or sender_profile.get("user_id") or sender_profile.get("email")):
                logger.warning(f"[Feishu] Rejecting message without a verified sender identifier: agent_id={agent_id}")
                try:
                    await feishu_service.send_message(
                        config.app_id,
                        config.app_secret,
                        delivery_target["receive_id"],
                        "text",
                        json.dumps(
                            {"text": "⚠️ 无法验证当前飞书身份，请重新进入会话后再试，或联系管理员检查飞书应用权限。"},
                            ensure_ascii=False,
                        ),
                        receive_id_type=delivery_target["receive_id_type"],
                        stage="sender_identity_unavailable",
                        extra_config=_feishu_platform_extra(config),
                    )
                except Exception as send_error:
                    logger.error(
                        f"[Feishu] Failed to deliver sender identity error: {send_error}",
                        exc_info=True,
                    )
                return {"code": 0, "msg": "sender identity unavailable"}

            from app.services.channel_ingress_inbox import channel_installation_ref
            from app.services.external_principal_service import resolve_or_create_external_principal

            principal_resolution = await resolve_or_create_external_principal(
                db,
                tenant_id=agent_tenant_id,
                provider="feishu",
                installation_ref=channel_installation_ref(config, fallback=f"feishu:{agent_id}"),
                channel_config_id=getattr(config, "id", None),
                subject_id=sender_subject_id,
                display_name=sender_name or f"Feishu {sender_open_id[:8]}",
                profile=sender_profile,
            )
            runtime_actor = principal_resolution.actor
            platform_user_id = runtime_actor.id
            external_principal_id = principal_resolution.principal.id
            delivery_target["external_principal_id"] = str(external_principal_id)
            delivery_target["user_label"] = runtime_actor.display_name or sender_name or sender_open_id[:8]

            if _is_feishu_new_session_command(user_text):
                _new_sess = await start_new_channel_session(
                    db=db,
                    agent_id=agent_id,
                    tenant_id=agent_tenant_id,
                    user_id=platform_user_id,
                    external_principal_id=external_principal_id,
                    external_conv_id=conv_id,
                    source_channel="feishu",
                    title="New Session",
                    legacy_external_conv_ids=legacy_conv_ids,
                    delivery_target=delivery_target,
                )
                session_conv_id = str(_new_sess.id)
                delivery_target["session_id"] = session_conv_id
                _new_sess.delivery_target_json = delivery_target
                await db.commit()
                await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    delivery_target["receive_id"],
                    "text",
                    json.dumps({"text": "已开启新会话。接下来的消息会从新的上下文开始。"}, ensure_ascii=False),
                    receive_id_type=delivery_target["receive_id_type"],
                    stage="new_session_ack",
                    extra_config=_feishu_platform_extra(config),
                )
                return {"code": 0, "msg": "new session created"}

            approval_shortcut = await _try_resolve_feishu_text_approval(
                db=db,
                agent=agent_obj,
                user_text=user_text,
                resolved_user=runtime_actor if runtime_actor.authority_bound else None,
                config=config,
                receive_id=delivery_target["receive_id"],
                receive_id_type=delivery_target["receive_id_type"],
            )
            if approval_shortcut is not None:
                return approval_shortcut

            from app.services.memory_service import compute_history_limit_for_agent

            _hist_limit = await compute_history_limit_for_agent(agent_id)

            # Pre-resolve session so history lookup uses the UUID  (session created later if new)
            pre_session_conv_ids = [conv_id, *legacy_conv_ids]
            _pre_sess_r = await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.external_conv_id.in_(pre_session_conv_ids),
                )
            )
            _pre_sessions = list(_pre_sess_r.scalars().all())
            _pre_sess = next((sess for sess in _pre_sessions if sess.external_conv_id == conv_id), None)
            if _pre_sess is None and _pre_sessions:
                _pre_sess = _pre_sessions[0]
            _history_conv_id = str(_pre_sess.id) if _pre_sess else conv_id
            history_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == _history_conv_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(_hist_limit)
            )
            history_msgs = history_result.scalars().all()
            history = []
            for m in reversed(history_msgs):
                entry = {"role": m.role, "content": m.content}
                if getattr(m, "thinking", None):
                    entry["reasoning_content"] = m.thinking
                if getattr(m, "thinking_signature", None):
                    entry["reasoning_signature"] = m.thinking_signature
                history.append(entry)

            try:
                from app.core.execution_context import set_delegated_user_identity

                if platform_user_id is not None:
                    _sender_label = sender_name or runtime_actor.display_name or sender_open_id[:8]
                    set_delegated_user_identity(
                        user_id=platform_user_id,
                        user_name=_sender_label,
                        channel="feishu",
                    )
            except Exception as _ei_err:
                logger.debug(f"[Feishu] Failed to set execution identity: {_ei_err}")

            # ── Find-or-create a ChatSession via external_conv_id (DB-based, no cache needed) ──
            from datetime import datetime as _dt, timezone as _tz

            _sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                tenant_id=agent_tenant_id,
                user_id=platform_user_id,
                external_principal_id=external_principal_id,
                external_conv_id=conv_id,
                source_channel="feishu",
                first_message_title=user_text,
                legacy_external_conv_ids=legacy_conv_ids,
                delivery_target=delivery_target,
            )
            session_conv_id = str(_sess.id)
            delivery_target["session_id"] = session_conv_id
            _sess.delivery_target_json = delivery_target

            # Save user message
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    tenant_id=agent_tenant_id,
                    user_id=platform_user_id,
                    external_principal_id=external_principal_id,
                    role="user",
                    content=user_text,
                    conversation_id=session_conv_id,
                )
            )
            _sess.last_message_at = _dt.now(_tz.utc)
            await db.commit()

            # Prepend sender identity so the agent knows who is talking
            llm_user_text = prefix_message_with_sender_label(
                user_text,
                sender_name=sender_name,
                sender_id=sender_user_id_feishu,
            )

            # ── Inject recent uploaded file context ──────────────────────────
            # Check the uploads directory for recently modified files (within 30 min).
            # This is more reliable than scanning DB history, because the file save
            # to disk always succeeds even if the DB transaction fails.
            try:
                import time as _time
                import pathlib as _pl
                from app.config import get_settings as _gs

                _upload_dir = _pl.Path(_gs().AGENT_DATA_DIR) / str(agent_id) / "workspace" / "uploads"
                _recent_file_path = None
                if _upload_dir.exists() and "uploads/" not in user_text and "workspace/" not in user_text:
                    _now = _time.time()
                    _candidates = sorted(
                        _upload_dir.iterdir(),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    for _fp in _candidates:
                        if _fp.is_file() and (_now - _fp.stat().st_mtime) < 1800:  # 30 min
                            _recent_file_path = f"uploads/{_fp.name}"
                            break
                if _recent_file_path:
                    # _recent_file_path is relative to uploads dir; agent workspace root is
                    # AGENT_DATA_DIR/{agent_id}/, so the correct relative path is workspace/uploads/
                    _ws_rel_path = f"workspace/{_recent_file_path}"
                    llm_user_text = (
                        llm_user_text + f"\n\n[系统提示：用户刚上传了文件，路径为工作区 `{_ws_rel_path}`。"
                        f"如果用户的指令涉及这篇文章、这个文件、这份文档等，"
                        f'请立即调用 read_document(path="{_ws_rel_path}") 读取内容，不要先用 list_files 验证，直接读取即可。]'
                    )
                    logger.info(f"[Feishu] Injected recent file hint: {_ws_rel_path}")
            except Exception as _fe:
                logger.error(f"[Feishu] File injection error: {_fe}")

            # Set sender open_id contextvar so calendar tool can auto-invite the requester
            from app.services.agent_tools import channel_feishu_sender_open_id as _cfso

            _cfso_token = _cfso.set(sender_open_id)

            # Set channel_file_sender contextvar so the agent can send files back via Feishu
            from app.services.agent_tools import channel_file_sender as _cfs
            from app.services.channel_delivery_service import channel_delivery_target as _cdt

            _reply_to_id = chat_id if chat_type == "group" else sender_open_id
            _rid_type = "chat_id" if chat_type == "group" else "open_id"

            async def _feishu_file_sender(file_path, msg: str = ""):
                try:
                    await feishu_service.upload_and_send_file(
                        config.app_id,
                        config.app_secret,
                        _reply_to_id,
                        file_path,
                        receive_id_type=_rid_type,
                        accompany_msg=msg,
                        extra_config=_feishu_platform_extra(config),
                    )
                except Exception as _upload_err:
                    # Fallback: send a download link when upload permission is not granted
                    from pathlib import Path as _P
                    from app.config import get_settings as _gs_fallback

                    _fs = _gs_fallback()
                    _base_url = getattr(_fs, "BASE_URL", "").rstrip("/") or ""
                    _fp = _P(file_path)
                    _ws_root = _P(_fs.AGENT_DATA_DIR)
                    try:
                        _rel = str(_fp.relative_to(_ws_root / str(agent_id)))
                    except ValueError:
                        _rel = _fp.name
                    _fallback_parts = []
                    if msg:
                        _fallback_parts.append(msg)
                    if _base_url:
                        _dl_url = f"{_base_url}/api/agents/{agent_id}/files/download?path={_rel}"
                        _fallback_parts.append(f"📎 {_fp.name}\n🔗 {_dl_url}")
                    _fallback_parts.append(
                        f"⚠️ 文件直接发送失败（{_upload_err}）\n"
                        "如需 Agent 直接发飞书文件，请在飞书开放平台为应用开启 "
                        "`im:resource`（即 `im:resource:upload`）权限并发布版本。"
                    )
                    await feishu_service.send_message(
                        config.app_id,
                        config.app_secret,
                        _reply_to_id,
                        "text",
                        json.dumps({"text": "\n\n".join(_fallback_parts)}),
                        receive_id_type=_rid_type,
                        extra_config=_feishu_platform_extra(config),
                    )

            _cfs_token = _cfs.set(_feishu_file_sender)
            _cdt_token = _cdt.set(delivery_target)

            # Set up streaming response via CardKit (primary) or IM patch (fallback)
            import json as _json_card

            cardkit_card_id: str | None = None
            cardkit_sequence: int = 0
            cardkit_stream_disabled = False
            msg_id_for_patch: str | None = None

            _reply_target = chat_id if chat_type == "group" and chat_id else sender_open_id
            _rid_type = "chat_id" if chat_type == "group" and chat_id else "open_id"

            init_card = {
                "schema": "2.0",
                "config": {
                    "streaming_mode": True,
                    "locales": ["zh_cn", "en_us"],
                    "summary": {"content": "思考中..."},
                },
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "",
                            "text_align": "left",
                            "text_size": "normal_v2",
                            "element_id": "streaming_content",
                        },
                        {
                            "tag": "markdown",
                            "content": " ",
                            "icon": {
                                "tag": "custom_icon",
                                "img_key": "img_v3_02vb_496bec09-4b43-4773-ad6b-0cdd103cd2bg",
                                "size": "16px 16px",
                            },
                            "element_id": "loading_icon",
                        },
                    ]
                },
            }

            try:
                cardkit_card_id = await feishu_service.create_card_entity(
                    config.app_id,
                    config.app_secret,
                    init_card,
                    extra_config=_feishu_platform_extra(config),
                )
                cardkit_sequence = 1
                await feishu_service.send_card_by_card_id(
                    config.app_id,
                    config.app_secret,
                    _reply_target,
                    cardkit_card_id,
                    receive_id_type=_rid_type,
                    extra_config=_feishu_platform_extra(config),
                )
                logger.info(f"[Feishu] CardKit card created and sent: card_id={cardkit_card_id}")
            except Exception as exc:
                logger.warning(f"[Feishu] CardKit flow failed, falling back to IM patch: {exc}")
                cardkit_card_id = None
                init_card_fallback = {
                    "config": {"update_multi": True},
                    "header": {"template": "blue", "title": {"content": "思考中...", "tag": "plain_text"}},
                    "elements": [{"tag": "markdown", "content": "..."}],
                }
                try:
                    init_resp = await feishu_service.send_message(
                        config.app_id,
                        config.app_secret,
                        _reply_target,
                        "interactive",
                        _json_card.dumps(init_card_fallback),
                        receive_id_type=_rid_type,
                        stage="stream_init_card",
                        extra_config=_feishu_platform_extra(config),
                    )
                    msg_id_for_patch = init_resp.get("data", {}).get("message_id")
                except Exception as fallback_exc:
                    logger.error(f"[Feishu] Fallback init card also failed: {fallback_exc}")

            _stream_buffer = []
            _thinking_buffer = []
            _last_flush_time = time.time()
            _FLUSH_INTERVAL_CARDKIT = 0.5
            _FLUSH_INTERVAL_PATCH = 1.0
            _agent_name = agent_obj.name if agent_obj else "AI 回复"
            _tool_status_running: dict[str, str] = {}
            _tool_status_done: list[str] = []
            _patch_queue = _SerialPatchQueue()
            _heartbeat_task: asyncio.Task | None = None
            _llm_done = False
            _last_flushed_hash = 0
            _last_flushed_text = ""
            _flush_lock = asyncio.Lock()

            def _build_card(
                answer_text: str,
                thinking_text: str = "",
                streaming: bool = False,
            ) -> dict:
                elements = []
                done_visible = _tool_status_done[-_TOOL_STATUS_KEEP_LINES:]
                running_visible = list(_tool_status_running.values())
                all_visible = done_visible + running_visible
                if all_visible:
                    elements.extend(_feishu_markdown_elements("\n".join(all_visible)))
                    elements.append({"tag": "hr"})
                if thinking_text:
                    think_preview = thinking_text[:200].replace("\n", " ")
                    elements.extend(
                        _feishu_markdown_elements(
                            f"<font color='grey'>💭 **思考过程**\n"
                            f"{think_preview}{'...' if len(thinking_text) > 200 else ''}</font>"
                        )
                    )
                    elements.append({"tag": "hr"})
                body = answer_text + ("▌" if streaming and answer_text else ("..." if streaming else ""))
                elements.extend(_feishu_markdown_elements(body or "..."))
                return {
                    "config": {"update_multi": True},
                    "header": {
                        "template": "blue",
                        "title": {"content": _agent_name, "tag": "plain_text"},
                    },
                    "elements": elements,
                }

            def _build_final_cardkit_card(answer_text: str, thinking_text: str = "") -> dict:
                elements = []
                if thinking_text:
                    elements.append(
                        {
                            "tag": "collapsible_panel",
                            "expanded": False,
                            "header": {
                                "title": {
                                    "tag": "markdown",
                                    "content": f"💭 Thinking... ({len(thinking_text)} chars)",
                                },
                                "vertical_align": "center",
                                "icon": {
                                    "tag": "standard_icon",
                                    "token": "down-small-ccm_outlined",
                                    "size": "16px 16px",
                                },
                                "icon_position": "follow_text",
                                "icon_expanded_angle": -180,
                            },
                            "border": {"color": "grey", "corner_radius": "5px"},
                            "elements": _feishu_markdown_elements(thinking_text, text_size="notation"),
                        }
                    )
                done_visible = _tool_status_done[-_TOOL_STATUS_KEEP_LINES:]
                running_visible = list(_tool_status_running.values())
                all_visible = done_visible + running_visible
                if all_visible:
                    elements.extend(_feishu_markdown_elements("\n".join(all_visible)))
                    elements.append({"tag": "hr"})
                elements.extend(_feishu_markdown_elements(answer_text or "..."))
                return {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True, "update_multi": True},
                    "body": {"elements": elements},
                }

            async def _queue_patch_card(card: dict, stage: str) -> None:
                if not msg_id_for_patch:
                    return
                payload = _json_card.dumps(card)

                async def _job():
                    try:
                        await feishu_service.patch_message(
                            config.app_id,
                            config.app_secret,
                            msg_id_for_patch,
                            payload,
                            stage=stage,
                            extra_config=_feishu_platform_extra(config),
                        )
                    except Exception as exc:
                        logger.warning(f"[Feishu] Patch failed (stage={stage}, message_id={msg_id_for_patch}): {exc}")

                _patch_queue.enqueue(_job)

            async def _flush_stream(reason: str, force: bool = False):
                nonlocal _last_flush_time, _last_flushed_hash, cardkit_sequence, _last_flushed_text
                nonlocal cardkit_stream_disabled
                if not cardkit_card_id and not msg_id_for_patch:
                    return
                async with _flush_lock:
                    now = time.time()
                    flush_interval = _FLUSH_INTERVAL_CARDKIT if cardkit_card_id else _FLUSH_INTERVAL_PATCH
                    if not force and now - _last_flush_time < flush_interval:
                        return
                    accumulated = "".join(_stream_buffer)
                    if cardkit_card_id and not cardkit_stream_disabled:
                        done_visible = _tool_status_done[-_TOOL_STATUS_KEEP_LINES:]
                        running_visible = list(_tool_status_running.values())
                        all_tool_lines = done_visible + running_visible
                        if all_tool_lines:
                            tool_section = "\n".join(all_tool_lines)
                            cardkit_text = f"{tool_section}\n---\n{accumulated}" if accumulated else tool_section
                        else:
                            cardkit_text = accumulated
                        if cardkit_text != _last_flushed_text:
                            cardkit_sequence += 1
                            try:
                                await asyncio.wait_for(
                                    feishu_service.stream_card_content(
                                        config.app_id,
                                        config.app_secret,
                                        cardkit_card_id,
                                        "streaming_content",
                                        cardkit_text,
                                        cardkit_sequence,
                                        extra_config=_feishu_platform_extra(config),
                                    ),
                                    timeout=5.0,
                                )
                                _last_flushed_text = cardkit_text
                            except asyncio.TimeoutError:
                                logger.warning(f"[Feishu] CardKit stream timed out, seq={cardkit_sequence}")
                            except Exception as exc:
                                if _is_cardkit_stream_closed_error(exc):
                                    cardkit_stream_disabled = True
                                    logger.warning(
                                        f"[Feishu] CardKit streaming disabled after server closed stream: {exc}"
                                    )
                                    return
                                logger.warning(f"[Feishu] CardKit stream failed: {exc}")
                    elif msg_id_for_patch:
                        card = _build_card(accumulated, "".join(_thinking_buffer), streaming=True)
                        current_hash = hash(
                            accumulated
                            + "".join(_thinking_buffer)
                            + str(_tool_status_done)
                            + str(list(_tool_status_running.values()))
                        )
                        if reason == "heartbeat" and current_hash == _last_flushed_hash:
                            return
                        _last_flushed_hash = current_hash
                        await _queue_patch_card(card, stage=f"stream_{reason}")
                    _last_flush_time = now

            async def _ws_on_chunk(text: str):
                if not cardkit_card_id and not msg_id_for_patch:
                    return
                _stream_buffer.append(text)
                await _flush_stream("chunk")

            async def _ws_on_thinking(text: str):
                if not cardkit_card_id and not msg_id_for_patch:
                    return
                _thinking_buffer.append(text)
                await _flush_stream("thinking")

            async def _ws_on_tool_call(evt: dict):
                tool_name = evt.get("name") or "unknown_tool"
                call_id = evt.get("call_id") or tool_name
                status = (evt.get("status") or "").lower()
                if status == "running":
                    _tool_status_running[call_id] = f"⏳ Tool running: `{tool_name}`"
                elif status == "done":
                    _tool_status_running.pop(call_id, None)
                    _tool_status_done.append(f"✅ Tool done: `{tool_name}`")
                else:
                    _tool_status_running.pop(call_id, None)
                    _tool_status_done.append(f"ℹ️ Tool update: `{tool_name}` ({status or 'unknown'})")
                await _flush_stream("tool")

            async def _heartbeat():
                while not _llm_done:
                    await asyncio.sleep(_FLUSH_INTERVAL_CARDKIT if cardkit_card_id else _FLUSH_INTERVAL_PATCH)
                    await _flush_stream("heartbeat")

            if cardkit_card_id or msg_id_for_patch:
                _heartbeat_task = asyncio.create_task(_heartbeat())

            # Call LLM with history and streaming callback
            try:
                reply_text = await call_agent_llm(
                    db,
                    agent_id,
                    llm_user_text,
                    history=history,
                    user_id=platform_user_id,
                    on_chunk=_ws_on_chunk,
                    on_tool_call=_ws_on_tool_call,
                    on_thinking=_ws_on_thinking,
                    session_id=session_conv_id,
                    session_source="feishu",
                    session_channel="feishu",
                    allow_bare_plan_confirmation=True,
                    durable_run=True,
                    durable_session=_sess,
                    durable_user=runtime_actor,
                )
            except Exception as _llm_err:
                logger.error(f"[Feishu] LLM invocation failed for agent {agent_id}: {_llm_err}")
                reply_text = f"⚠️ 抱歉，处理消息时出错，请稍后重试。({type(_llm_err).__name__})"
            finally:
                _llm_done = True
                if _heartbeat_task:
                    _heartbeat_task.cancel()
                    try:
                        await _heartbeat_task
                    except (Exception, asyncio.CancelledError):
                        logger.debug("[Feishu] Heartbeat task cancelled during cleanup")
                _cfs.reset(_cfs_token)
                _cfso.reset(_cfso_token)
                _cdt.reset(_cdt_token)
            logger.info(f"[Feishu] LLM reply: {reply_text[:100]}")

            # Send final card update or fallback text
            if cardkit_card_id:
                try:
                    cardkit_sequence += 1
                    await asyncio.wait_for(
                        feishu_service.set_card_streaming_mode(
                            config.app_id,
                            config.app_secret,
                            cardkit_card_id,
                            0,
                            cardkit_sequence,
                            extra_config=_feishu_platform_extra(config),
                        ),
                        timeout=10.0,
                    )
                    cardkit_sequence += 1
                    final_card = _build_final_cardkit_card(reply_text, "".join(_thinking_buffer))
                    await asyncio.wait_for(
                        feishu_service.update_cardkit_card(
                            config.app_id,
                            config.app_secret,
                            cardkit_card_id,
                            final_card,
                            cardkit_sequence,
                            extra_config=_feishu_platform_extra(config),
                        ),
                        timeout=10.0,
                    )
                except Exception as exc:
                    logger.error(f"[Feishu] CardKit final update failed: {exc}")
                    try:
                        await feishu_service.send_message(
                            config.app_id,
                            config.app_secret,
                            _reply_target,
                            "text",
                            json.dumps({"text": reply_text}),
                            receive_id_type=_rid_type,
                            stage="stream_final_fallback_text",
                            extra_config=_feishu_platform_extra(config),
                        )
                    except Exception as fallback_exc:
                        logger.error(f"[Feishu] CardKit fallback text also failed: {fallback_exc}")
            elif msg_id_for_patch:
                try:
                    await _patch_queue.drain()
                except Exception as exc:
                    logger.warning(f"[Feishu] Drain patch queue failed before final patch: {exc}")
                final_card = _build_card(reply_text, "".join(_thinking_buffer), streaming=False)
                try:
                    await feishu_service.patch_message(
                        config.app_id,
                        config.app_secret,
                        msg_id_for_patch,
                        _json_card.dumps(final_card),
                        stage="stream_final",
                        extra_config=_feishu_platform_extra(config),
                    )
                except Exception as exc:
                    logger.error(f"[Feishu] Final card patch failed: {exc}")
                    try:
                        await feishu_service.send_message(
                            config.app_id,
                            config.app_secret,
                            _reply_target,
                            "text",
                            json.dumps({"text": reply_text}),
                            receive_id_type=_rid_type,
                            stage="stream_final_fallback_text",
                            extra_config=_feishu_platform_extra(config),
                        )
                    except Exception as fallback_exc:
                        logger.error(f"[Feishu] Fallback text also failed: {fallback_exc}")
            else:
                try:
                    await feishu_service.send_message(
                        config.app_id,
                        config.app_secret,
                        _reply_target,
                        "text",
                        json.dumps({"text": reply_text}),
                        receive_id_type=_rid_type,
                        stage="stream_no_card_fallback_text",
                        extra_config=_feishu_platform_extra(config),
                    )
                except Exception as exc:
                    logger.error(f"[Feishu] Failed to send fallback message: {exc}")

            # Log activity
            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "chat_reply",
                f"回复了飞书消息: {reply_text[:80]}",
                detail={"channel": "feishu", "user_text": user_text[:200], "reply": reply_text[:500]},
            )

            if should_persist_channel_reply_as_assistant(reply_text):
                db.add(
                    ChatMessage(
                        agent_id=agent_id,
                        tenant_id=agent_tenant_id,
                        user_id=platform_user_id,
                        external_principal_id=external_principal_id,
                        role="assistant",
                        content=reply_text,
                        conversation_id=session_conv_id,
                    )
                )
            _sess.last_message_at = _dt.now(_tz.utc)
            await db.commit()

    return {"code": 0, "msg": "ok"}


IMPORT_RE = None  # lazy sentinel
_FILE_ACK_MESSAGES = [
    "收到你的文件，请问有什么需要帮忙的？",
    "文件收到了！你想让我怎么处理它？",
    "好的，我已经收到这份文件，请告诉我你的需求~",
    "已收到文件，随时准备好为你处理！",
    "收到！请问希望我对这份文件做什么？",
]


async def _handle_feishu_file(
    db,
    agent_id,
    config,
    message,
    sender_open_id,
    sender_user_id_from_event,
    chat_type,
    chat_id,
):
    """Handle incoming file or image messages from Feishu (runs as a background task)."""
    import asyncio
    import random
    import json
    from pathlib import Path
    from app.config import get_settings
    from app.models.audit import ChatMessage
    from app.models.agent import Agent as AgentModel
    from app.services.channel_session import find_or_create_channel_session
    from app.database import tenant_scoped_session as _tenant_scoped_session
    from app.services.tenant_resolver import resolve_tenant_for_agent
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import select as _select

    # Webhook bg path has no TenantMiddleware GUC. Resolve the tenant once from
    # the path agent_id (narrow audited bypass single-row read) and pin every
    # session below to it so Agent/User/ChatMessage writes stay tenant-scoped.
    _feishu_tenant_id = await resolve_tenant_for_agent(agent_id)

    msg_type = message.get("message_type", "file")
    message_id = message.get("message_id", "")
    content = json.loads(message.get("content", "{}"))

    # Extract file key and name
    if msg_type == "image":
        file_key = content.get("image_key", "")
        filename = f"image_{file_key[-8:]}.jpg" if file_key else "image.jpg"
        res_type = "image"
    else:
        file_key = content.get("file_key", "")
        filename = content.get("file_name") or f"file_{file_key[-8:]}.bin"
        res_type = "file"

    if not file_key:
        logger.warning(f"[Feishu] No file_key in {msg_type} message")
        return

    # Resolve workspace upload dir
    settings = get_settings()
    upload_dir = Path(settings.AGENT_DATA_DIR) / str(agent_id) / "workspace" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    save_path = upload_dir / filename

    # Download the file
    try:
        file_bytes = await feishu_service.download_message_resource(
            config.app_id,
            config.app_secret,
            message_id,
            file_key,
            res_type,
            extra_config=_feishu_platform_extra(config),
        )
        save_path.write_bytes(file_bytes)
        logger.info(f"[Feishu] Saved {msg_type} to {save_path} ({len(file_bytes)} bytes)")
    except Exception as e:
        logger.error(f"[Feishu] Failed to download {msg_type}: {e}")
        err_tip = "抱歉，文件下载失败。可能原因：机器人缺少 `im:resource` 权限（文件读取）。\n请在飞书开放平台 → 权限管理 → 批量导入权限 JSON → 重新发布机器人版本后重试。"
        try:
            import json as _j

            if chat_type == "group" and chat_id:
                await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    chat_id,
                    "text",
                    _j.dumps({"text": err_tip}),
                    receive_id_type="chat_id",
                    extra_config=_feishu_platform_extra(config),
                )
            else:
                await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    sender_open_id,
                    "text",
                    _j.dumps({"text": err_tip}),
                    extra_config=_feishu_platform_extra(config),
                )
        except Exception as e2:
            logger.error(f"[Feishu] Also failed to send error tip: {e2}")
        return

    # Resolve platform user and session using a fresh db session
    async with _tenant_scoped_session(_feishu_tenant_id) as db:
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        sender_profile = await _resolve_feishu_sender_profile(
            agent_id,
            config=config,
            sender_open_id=sender_open_id,
            sender_user_id=sender_user_id_from_event,
        )
        sender_user_id_feishu = sender_profile.get("user_id", "") or sender_user_id_from_event
        sender_subject_id = (
            sender_profile.get("open_id")
            or sender_open_id
            or sender_user_id_feishu
            or sender_profile.get("email")
            or ""
        )
        if not sender_subject_id:
            logger.warning(f"[Feishu] Rejecting file without a verified sender identifier: agent_id={agent_id}")
            return

        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.external_principal_service import resolve_or_create_external_principal

        principal_resolution = await resolve_or_create_external_principal(
            db,
            tenant_id=agent_obj.tenant_id if agent_obj else _feishu_tenant_id,
            provider="feishu",
            installation_ref=channel_installation_ref(config, fallback=f"feishu:{agent_id}"),
            channel_config_id=getattr(config, "id", None),
            subject_id=sender_subject_id,
            display_name=sender_profile.get("name") or f"Feishu {sender_open_id[:8]}",
            profile=sender_profile,
        )
        runtime_actor = principal_resolution.actor
        platform_user_id = runtime_actor.id
        external_principal_id = principal_resolution.principal.id

        # Set execution identity — delegated user action via Feishu
        try:
            from app.core.execution_context import set_delegated_user_identity

            if platform_user_id is not None:
                _sender_label = runtime_actor.display_name or sender_open_id[:8]
                set_delegated_user_identity(
                    user_id=platform_user_id,
                    user_name=_sender_label,
                    channel="feishu",
                )
        except Exception as _ei_err:
            logger.debug(f"[Feishu] Failed to set execution identity: {_ei_err}")

        # Conv ID — prefer user_id for session continuity
        if chat_type == "group" and chat_id:
            conv_id = f"feishu_group_{chat_id}_{sender_subject_id}"
            legacy_conv_ids = []
        else:
            conv_id = build_feishu_p2p_conv_id(sender_user_id_feishu, sender_open_id) or f"feishu_p2p_{sender_open_id}"
            legacy_conv_ids = list_legacy_feishu_conv_ids(sender_open_id, conv_id)
        delivery_target = {
            "channel": "feishu",
            "receive_id": chat_id if chat_type == "group" and chat_id else sender_open_id,
            "receive_id_type": "chat_id" if chat_type == "group" and chat_id else "open_id",
            "chat_id": chat_id,
            "chat_type": chat_type,
            "open_id": sender_open_id,
            "user_id": sender_user_id_feishu,
            "user_label": runtime_actor.display_name or sender_open_id[:8],
            "external_principal_id": str(external_principal_id),
        }

        # Find-or-create session
        _sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            tenant_id=_feishu_tenant_id,
            user_id=platform_user_id,
            external_principal_id=external_principal_id,
            external_conv_id=conv_id,
            source_channel="feishu",
            first_message_title=f"[文件] {filename}",
            legacy_external_conv_ids=legacy_conv_ids,
            delivery_target=delivery_target,
        )
        session_conv_id = str(_sess.id)
        delivery_target["session_id"] = session_conv_id
        _sess.delivery_target_json = delivery_target

        # Store user message — include base64 marker for images so LLM can see them
        if msg_type == "image":
            import base64 as _b64_img

            _b64_data = _b64_img.b64encode(file_bytes).decode("ascii")
            _image_marker = f"[image_data:data:image/jpeg;base64,{_b64_data}]"
            user_msg_content = f"[用户发送了图片]\n{_image_marker}"
        else:
            user_msg_content = f"[file:{filename}]"
        db.add(
            ChatMessage(
                agent_id=agent_id,
                tenant_id=_feishu_tenant_id,
                user_id=platform_user_id,
                external_principal_id=external_principal_id,
                role="user",
                content=user_msg_content if msg_type != "image" else f"[file:{filename}]",
                conversation_id=session_conv_id,
            )
        )
        _sess.last_message_at = _dt.now(_tz.utc)

        # Load conversation history for LLM context
        from app.services.memory_service import compute_history_limit_for_agent as _chlfa

        _hist_limit2 = await _chlfa(agent_id)
        _hist_r = await db.execute(
            _select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(_hist_limit2)
        )
        _history = [{"role": m.role, "content": m.content} for m in reversed(_hist_r.scalars().all())]

        await db.commit()

    # For images: call LLM so vision models can actually see the image
    if msg_type == "image":
        import time as _time_img
        import json as _json_card_img

        # Send initial loading card
        _reply_to = chat_id if chat_type == "group" else sender_open_id
        _rid_type = "chat_id" if chat_type == "group" else "open_id"
        _agent_name = agent_obj.name if agent_obj else "AI"
        _init_card = {
            "config": {"update_multi": True},
            "header": {"template": "blue", "title": {"content": "识别图片中...", "tag": "plain_text"}},
            "elements": [{"tag": "markdown", "content": "..."}],
        }
        _patch_msg_id = None
        try:
            _init_resp = await feishu_service.send_message(
                config.app_id,
                config.app_secret,
                _reply_to,
                "interactive",
                _json_card_img.dumps(_init_card),
                receive_id_type=_rid_type,
                extra_config=_feishu_platform_extra(config),
            )
            _patch_msg_id = _init_resp.get("data", {}).get("message_id")
        except Exception as _e_init:
            logger.error(f"[Feishu] Failed to send init card for image: {_e_init}")

        _img_stream_buf = []
        _img_last_flush = _time_img.time()

        async def _img_on_chunk(text):
            nonlocal _img_last_flush
            _img_stream_buf.append(text)
            now = _time_img.time()
            if _patch_msg_id and now - _img_last_flush >= 1.0:
                _card = {
                    "config": {"update_multi": True},
                    "header": {"template": "blue", "title": {"content": _agent_name, "tag": "plain_text"}},
                    "elements": [{"tag": "markdown", "content": "".join(_img_stream_buf) + "▌"}],
                }
                import asyncio as _aio_img

                _aio_img.create_task(
                    feishu_service.patch_message(
                        config.app_id,
                        config.app_secret,
                        _patch_msg_id,
                        _json_card_img.dumps(_card),
                        extra_config=_feishu_platform_extra(config),
                    )
                )
                _img_last_flush = now

        # Call LLM with image marker — vision models will parse it
        from app.services.channel_delivery_service import channel_delivery_target as _cdt_img

        _cdt_img_token = _cdt_img.set(delivery_target)
        async with _tenant_scoped_session(_feishu_tenant_id) as _db_img:
            try:
                reply_text = await call_agent_llm(
                    _db_img,
                    agent_id,
                    user_msg_content,
                    history=_history,
                    user_id=platform_user_id,
                    on_chunk=_img_on_chunk,
                    session_id=session_conv_id,
                    session_source="feishu",
                    session_channel="feishu",
                    allow_bare_plan_confirmation=True,
                    durable_run=True,
                    durable_session=_sess,
                    durable_user=runtime_actor,
                )
            finally:
                _cdt_img.reset(_cdt_img_token)

        logger.info(f"[Feishu] Image LLM reply: {reply_text[:100]}")

        # Send final card or fallback text
        if _patch_msg_id:
            _final_card = {
                "config": {"update_multi": True},
                "header": {"template": "blue", "title": {"content": _agent_name, "tag": "plain_text"}},
                "elements": [{"tag": "markdown", "content": reply_text or "..."}],
            }
            await feishu_service.patch_message(
                config.app_id,
                config.app_secret,
                _patch_msg_id,
                _json_card_img.dumps(_final_card),
                extra_config=_feishu_platform_extra(config),
            )
        else:
            try:
                await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    _reply_to,
                    "text",
                    json.dumps({"text": reply_text}),
                    receive_id_type=_rid_type,
                    extra_config=_feishu_platform_extra(config),
                )
            except Exception as _e_fb:
                logger.error(f"[Feishu] Failed to send image reply: {_e_fb}")

        if should_persist_channel_reply_as_assistant(reply_text):
            async with _tenant_scoped_session(_feishu_tenant_id) as _db_save:
                _db_save.add(
                    ChatMessage(
                        agent_id=agent_id,
                        tenant_id=_feishu_tenant_id,
                        user_id=platform_user_id,
                        external_principal_id=external_principal_id,
                        role="assistant",
                        content=reply_text,
                        conversation_id=session_conv_id,
                    )
                )
                await _db_save.commit()

        # Log activity
        from app.services.activity_logger import log_activity

        await log_activity(
            agent_id,
            "chat_reply",
            f"回复了飞书图片消息: {reply_text[:80]}",
            detail={"channel": "feishu", "type": "image"},
        )
        return

    # For non-image files: send simple ack as before
    await asyncio.sleep(random.uniform(1.0, 2.0))

    ack = random.choice(_FILE_ACK_MESSAGES)
    try:
        if chat_type == "group" and chat_id:
            await feishu_service.send_message(
                config.app_id,
                config.app_secret,
                chat_id,
                "text",
                json.dumps({"text": ack}),
                receive_id_type="chat_id",
                extra_config=_feishu_platform_extra(config),
            )
        else:
            await feishu_service.send_message(
                config.app_id,
                config.app_secret,
                sender_open_id,
                "text",
                json.dumps({"text": ack}),
                extra_config=_feishu_platform_extra(config),
            )
    except Exception as e:
        logger.error(f"[Feishu] Failed to send ack: {e}")

    # Provider receipt only: never place platform-authored ACK text in the
    # model-visible assistant history.


async def _download_post_images(agent_id, config, message_id, image_keys):
    """Download images embedded in a Feishu post message to the agent's workspace."""
    from pathlib import Path
    from app.config import get_settings

    settings = get_settings()
    upload_dir = Path(settings.AGENT_DATA_DIR) / str(agent_id) / "workspace" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    for ik in image_keys:
        try:
            file_bytes = await feishu_service.download_message_resource(
                config.app_id,
                config.app_secret,
                message_id,
                ik,
                "image",
                extra_config=_feishu_platform_extra(config),
            )
            save_path = upload_dir / f"image_{ik[-8:]}.jpg"
            save_path.write_bytes(file_bytes)
            logger.info(f"[Feishu] Saved post image to {save_path} ({len(file_bytes)} bytes)")
        except Exception as e:
            logger.error(f"[Feishu] Failed to download post image {ik}: {e}")
