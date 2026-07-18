"""DingTalk Channel API routes.

Provides Config CRUD and message handling for DingTalk bots using Stream mode.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.security import get_current_user
from app.database import get_db
from app.api.channel_secrets import resolve_secret_field
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut

router = APIRouter(tags=["dingtalk"])


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_dingtalk_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure DingTalk bot for an agent. Fields: app_key, app_secret."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    existing = result.scalar_one_or_none()
    app_key = data.get("app_key", "").strip()
    app_secret = resolve_secret_field(data, "app_secret", existing.app_secret if existing else None)
    if not app_key or not app_secret:
        raise HTTPException(status_code=422, detail="app_key and app_secret are required")
    if existing:
        existing.app_id = app_key
        existing.app_secret = app_secret
        existing.is_configured = True
        await db.flush()
        # Restart Stream client
        from app.services.dingtalk_stream import dingtalk_stream_manager
        import asyncio

        asyncio.create_task(dingtalk_stream_manager.start_client(agent_id, app_key, app_secret))
        return ChannelConfigOut.model_validate(existing)

    config = ChannelConfig(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        channel_type="dingtalk",
        app_id=app_key,
        app_secret=app_secret,
        is_configured=True,
    )
    db.add(config)
    await db.flush()

    # Start Stream client
    from app.services.dingtalk_stream import dingtalk_stream_manager
    import asyncio

    asyncio.create_task(dingtalk_stream_manager.start_client(agent_id, app_key, app_secret))

    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/dingtalk-channel", response_model=ChannelConfigOut)
async def get_dingtalk_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    return ChannelConfigOut.model_validate(config).to_safe()


@router.delete("/agents/{agent_id}/dingtalk-channel", status_code=204)
async def delete_dingtalk_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await require_agent_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "dingtalk",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="DingTalk not configured")
    from app.services.external_principal_service import revoke_channel_config_external_principals

    await revoke_channel_config_external_principals(
        db,
        tenant_id=agent.tenant_id,
        config=config,
        actor_user_id=current_user.id,
        reason="DingTalk channel configuration deleted",
    )
    await db.delete(config)

    # Stop Stream client
    from app.services.dingtalk_stream import dingtalk_stream_manager
    import asyncio

    asyncio.create_task(dingtalk_stream_manager.stop_client(agent_id))


# ─── Message Processing (called by Stream callback) ────


async def process_dingtalk_message(
    agent_id: uuid.UUID,
    sender_staff_id: str,
    user_text: str,
    conversation_id: str,
    conversation_type: str,
    session_webhook: str,
):
    """Process an incoming DingTalk bot message and reply via session webhook."""
    import httpx
    from datetime import datetime, timezone
    from sqlalchemy import select as _select
    from app.database import tenant_scoped_session
    from app.services.tenant_resolver import resolve_tenant_for_agent
    from app.models.agent import Agent as AgentModel
    from app.models.audit import ChatMessage
    from app.models.channel_config import ChannelConfig
    from app.services.channel_agent_runtime import call_agent_llm, should_persist_channel_reply_as_assistant
    from app.services.channel_session import find_or_create_channel_session

    # Webhook bg path has no TenantMiddleware GUC. Resolve the tenant from the
    # path agent_id (narrow audited bypass single-row read) and pin the session.
    _dingtalk_tenant_id = await resolve_tenant_for_agent(agent_id)
    async with tenant_scoped_session(_dingtalk_tenant_id) as db:
        # Load agent
        agent_r = await db.execute(_select(AgentModel).where(AgentModel.id == agent_id))
        agent_obj = agent_r.scalar_one_or_none()
        if not agent_obj:
            logger.warning(f"[DingTalk] Agent {agent_id} not found")
            return
        from app.services.memory_service import compute_history_limit_for_agent

        _hist_limit = await compute_history_limit_for_agent(agent_id)

        # Determine conv_id for session isolation
        if conversation_type == "2":
            # Group chat
            conv_id = f"dingtalk_group_{conversation_id}_{sender_staff_id}"
        else:
            # P2P / single chat
            conv_id = f"dingtalk_p2p_{sender_staff_id}"

        delivery_target = {
            "channel": "dingtalk",
            "session_webhook": session_webhook,
            "conversation_id": conversation_id,
            "conversation_type": conversation_type,
            "sender_staff_id": sender_staff_id,
        }

        config = (
            await db.execute(
                _select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "dingtalk",
                )
            )
        ).scalar_one_or_none()
        from app.services.channel_ingress_context import current_channel_ingress_context
        from app.services.external_principal_service import resolve_or_create_external_principal

        ingress = current_channel_ingress_context()
        installation_ref = (
            ingress.installation_ref
            if ingress is not None and ingress.provider == "dingtalk" and ingress.installation_ref
            else str(getattr(config, "id", None) or f"dingtalk:{agent_id}")
        )
        principal_resolution = await resolve_or_create_external_principal(
            db,
            tenant_id=agent_obj.tenant_id,
            provider="dingtalk",
            installation_ref=installation_ref,
            channel_config_id=getattr(config, "id", None),
            subject_id=sender_staff_id,
            display_name=f"DingTalk {sender_staff_id[:8]}",
            profile={"conversation_id": conversation_id, "conversation_type": conversation_type},
        )
        runtime_actor = principal_resolution.actor
        platform_user_id = runtime_actor.id
        external_principal_id = principal_resolution.principal.id
        delivery_target["external_principal_id"] = str(external_principal_id)

        # Find or create session
        sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            tenant_id=agent_obj.tenant_id if agent_obj else None,
            user_id=platform_user_id,
            external_principal_id=external_principal_id,
            external_conv_id=conv_id,
            source_channel="dingtalk",
            first_message_title=user_text,
            delivery_target=delivery_target,
        )
        session_conv_id = str(sess.id)
        delivery_target["session_id"] = session_conv_id
        sess.delivery_target_json = delivery_target

        # Load history
        history_r = await db.execute(
            _select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(_hist_limit)
        )
        history = [{"role": m.role, "content": m.content} for m in reversed(history_r.scalars().all())]

        # Save user message
        db.add(
            ChatMessage(
                agent_id=agent_id,
                tenant_id=agent_obj.tenant_id,
                user_id=platform_user_id,
                external_principal_id=external_principal_id,
                role="user",
                content=user_text,
                conversation_id=session_conv_id,
            )
        )
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        # Call LLM
        from app.services.channel_delivery_service import channel_delivery_target as _cdt

        _cdt_token = _cdt.set(delivery_target)
        try:
            reply_text = await call_agent_llm(
                db,
                agent_id,
                user_text,
                history=history,
                user_id=platform_user_id,
                session_id=session_conv_id,
                session_source="dingtalk",
                session_channel="dingtalk",
                allow_bare_plan_confirmation=True,
                durable_run=True,
                durable_session=sess,
                durable_user=runtime_actor,
            )
        finally:
            _cdt.reset(_cdt_token)
        logger.info(f"[DingTalk] LLM reply: {reply_text[:100]}")

        # Reply via session webhook (markdown)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    session_webhook,
                    json={
                        "msgtype": "markdown",
                        "markdown": {
                            "title": agent_obj.name or "AI Reply",
                            "text": reply_text,
                        },
                    },
                )
        except Exception as e:
            logger.error(f"[DingTalk] Failed to reply via webhook: {e}")
            # Fallback: try plain text
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        session_webhook,
                        json={
                            "msgtype": "text",
                            "text": {"content": reply_text},
                        },
                    )
            except Exception as e2:
                logger.error(f"[DingTalk] Fallback text reply also failed: {e2}")

        if should_persist_channel_reply_as_assistant(reply_text):
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    tenant_id=agent_obj.tenant_id,
                    user_id=platform_user_id,
                    external_principal_id=external_principal_id,
                    role="assistant",
                    content=reply_text,
                    conversation_id=session_conv_id,
                )
            )
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        # Log activity
        from app.services.activity_logger import log_activity

        await log_activity(
            agent_id,
            "chat_reply",
            f"Replied to DingTalk message: {reply_text[:80]}",
            detail={"channel": "dingtalk", "user_text": user_text[:200], "reply": reply_text[:500]},
        )
