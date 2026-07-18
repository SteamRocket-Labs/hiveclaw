"""Microsoft Teams Bot Channel API routes."""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channel_rls import load_public_agent_channel_config
from app.config import get_settings
from app.api.channel_secrets import resolve_secret_field
from app.core.permissions import check_agent_access, require_agent_manage_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent as AgentModel
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.user import User
from app.schemas.schemas import ChannelConfigOut
from app.services.channel_agent_runtime import call_agent_llm, should_persist_channel_reply_as_assistant
from app.services.channel_session import find_or_create_channel_session
from app.services.agent_tools import channel_file_sender as _cfs_s
from pathlib import Path as _Path

settings = get_settings()

router = APIRouter(tags=["microsoft_teams"])

TEAMS_MSG_LIMIT = 28000  # Teams message char limit (approx 28KB)

# In-memory cache for OAuth tokens
_teams_tokens: dict[str, dict] = {}  # agent_id -> {access_token, expires_at}


async def _get_teams_access_token(config: ChannelConfig) -> str | None:
    """Get or refresh Microsoft Teams access token.

    Supports:
    - Client credentials (app_id + app_secret) - default
    - Managed Identity (when use_managed_identity is True in extra_config)
    """
    agent_id = str(config.agent_id)
    cached = _teams_tokens.get(agent_id)
    if cached and cached["expires_at"] > time.time() + 60:  # Refresh 60s before expiry
        logger.debug(f"Teams: Using cached access token for agent {agent_id}")
        return cached["access_token"]

    # Check if managed identity should be used
    use_managed_identity = config.extra_config.get("use_managed_identity", False)

    if use_managed_identity:
        # Use Azure Managed Identity
        try:
            from azure.identity.aio import DefaultAzureCredential
            from azure.core.credentials import AccessToken

            credential = DefaultAzureCredential()
            # For Bot Framework, we need the token for the Bot Framework API
            # Managed identity needs to be granted permissions to the Bot Framework API
            scope = "https://api.botframework.com/.default"
            token: AccessToken = await credential.get_token(scope)

            _teams_tokens[agent_id] = {
                "access_token": token.token,
                "expires_at": token.expires_on,
            }
            logger.info(
                f"Teams: Successfully obtained access token via managed identity for agent {agent_id}, expires at {token.expires_on}"
            )
            await credential.close()
            return token.token
        except ImportError:
            logger.error("Teams: azure-identity package not installed. Install it with: pip install azure-identity")
            return None
        except Exception as e:
            logger.error(
                f"Teams: Failed to get access token via managed identity for agent {agent_id}: {e}", exc_info=True
            )
            return None

    # Use client credentials (app_id + app_secret)
    app_id = config.app_id
    app_secret = config.app_secret
    if not app_id or not app_secret:
        logger.error(f"Teams: Missing app_id or app_secret for agent {agent_id}")
        return None

    # Get tenant_id from config (per-agent), environment variable, or default to "common" (multi-tenant)
    tenant_id = config.extra_config.get("tenant_id") or os.environ.get("TEAMS_TENANT_ID") or "common"
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "client_credentials",
        "scope": "https://api.botframework.com/.default",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(token_url, data=data)
            if resp.status_code != 200:
                error_body = resp.text
                try:
                    error_json = resp.json()
                    error_description = error_json.get("error_description", "No description")
                    error_code = error_json.get("error", "unknown")
                    logger.error(
                        f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, error={error_code}, description={error_description}"
                    )
                except Exception:
                    logger.error(
                        f"Teams: OAuth token request failed for agent {agent_id}: status={resp.status_code}, response={error_body}"
                    )
                logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
                return None
            token_data = resp.json()
            access_token = token_data["access_token"]
            expires_in = token_data["expires_in"]

            _teams_tokens[agent_id] = {
                "access_token": access_token,
                "expires_at": time.time() + expires_in,
            }
            logger.info(f"Teams: Successfully obtained access token for agent {agent_id}, expires in {expires_in}s")
            return access_token
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, "response") and e.response else "No response body"
        try:
            if hasattr(e, "response") and e.response:
                error_json = e.response.json()
                error_description = error_json.get("error_description", "No description")
                error_code = error_json.get("error", "unknown")
                logger.error(
                    f"Teams: OAuth token HTTP error for agent {agent_id}: status={e.response.status_code}, error={error_code}, description={error_description}"
                )
        except Exception:
            logger.error(
                f"Teams: OAuth token HTTP error for agent {agent_id}: status={e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}, response={error_body}"
            )
        logger.error(f"Teams: Token URL={token_url}, tenant_id={tenant_id}, client_id={app_id[:20]}...")
        return None
    except Exception as e:
        logger.error(f"Teams: Failed to get access token for agent {agent_id}: {e}", exc_info=True)
        return None


async def _send_teams_message(config: ChannelConfig, conversation_id: str, activity: dict) -> None:
    """Send an activity (message) to Microsoft Teams."""
    access_token = await _get_teams_access_token(config)
    if not access_token:
        logger.error(f"Teams: No access token for agent {config.agent_id}, cannot send message")
        raise ValueError("No access token available")

    service_url = config.extra_config.get("service_url")
    if not service_url:
        logger.error(f"Teams: No service_url in config for agent {config.agent_id}, cannot send message")
        raise ValueError(f"No service_url in config for agent {config.agent_id}")

    # Ensure activity has required fields
    if "type" not in activity:
        activity["type"] = "message"
    if "timestamp" not in activity:
        activity["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"

    # Teams API expects 'replyToId' for replies, not 'conversation.id'
    # If it's a reply, ensure the 'id' field is set to the message being replied to
    if activity.get("replyToId") and "id" not in activity:
        activity["id"] = str(uuid.uuid4())  # Generate a new ID for the reply activity

    # Teams has a 28KB limit for message activities. Chunk if needed.
    text_content = activity.get("text", "")
    if len(text_content.encode("utf-8")) > TEAMS_MSG_LIMIT:
        chunks = [text_content[i : i + TEAMS_MSG_LIMIT] for i in range(0, len(text_content), TEAMS_MSG_LIMIT)]
        for i, chunk in enumerate(chunks):
            chunk_activity = {**activity, "text": chunk}
            if i > 0:  # Only the first chunk is a direct reply, subsequent are new messages
                chunk_activity.pop("replyToId", None)
            await _send_teams_message_single_chunk(access_token, service_url, conversation_id, chunk_activity)
    else:
        await _send_teams_message_single_chunk(access_token, service_url, conversation_id, activity)


async def _send_teams_message_single_chunk(
    access_token: str, service_url: str, conversation_id: str, activity: dict
) -> None:
    """Send a single chunked message to Microsoft Teams."""
    # Ensure service_url doesn't have trailing slash to avoid double slashes
    service_url_clean = service_url.rstrip("/")
    post_url = f"{service_url_clean}/v3/conversations/{conversation_id}/activities"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(post_url, headers=headers, json=activity)
            if resp.status_code != 200:
                error_body = resp.text
                try:
                    error_json = resp.json()
                    error_description = error_json.get("error", {}).get(
                        "message", error_json.get("message", "No description")
                    )
                    error_code = error_json.get("error", {}).get("code", "unknown")
                    logger.error(
                        f"Teams: Failed to send message: status={resp.status_code}, error={error_code}, description={error_description}"
                    )
                except Exception:
                    logger.error(f"Teams: Failed to send message: status={resp.status_code}, response={error_body}")
                logger.error(
                    f"Teams: POST URL={post_url}, conversation_id={conversation_id}, service_url={service_url}"
                )
            resp.raise_for_status()
            logger.info(f"Teams: Sent message to conversation {conversation_id}")
    except httpx.HTTPStatusError as e:
        error_body = e.response.text if hasattr(e, "response") and e.response else "No response body"
        try:
            if hasattr(e, "response") and e.response:
                error_json = e.response.json()
                error_description = error_json.get("error", {}).get(
                    "message", error_json.get("message", "No description")
                )
                error_code = error_json.get("error", {}).get("code", "unknown")
                logger.error(
                    f"Teams: HTTP error sending message: status={e.response.status_code}, error={error_code}, description={error_description}"
                )
        except Exception:
            logger.error(
                f"Teams: HTTP error sending message: status={e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}, response={error_body}"
            )
        logger.error(f"Teams: POST URL={post_url}, conversation_id={conversation_id}, service_url={service_url}")
        raise


# ─── Config CRUD ────────────────────────────────────────


@router.post("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut, status_code=201)
async def configure_teams_channel(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure Microsoft Teams bot for an agent. Fields: app_id, app_secret."""
    agent = await require_agent_manage_access(db, current_user, agent_id)

    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "microsoft_teams",
        )
    )
    existing = result.scalar_one_or_none()
    app_id = data.get("app_id", "").strip()
    app_secret = resolve_secret_field(data, "app_secret", existing.app_secret if existing else None)
    tenant_id = data.get("tenant_id", "").strip()  # Optional: for single-tenant apps
    use_managed_identity = data.get("use_managed_identity", False)  # Optional: use Azure Managed Identity

    # Validate: either managed identity OR app_id + app_secret required
    if not use_managed_identity and (not app_id or not app_secret):
        raise HTTPException(
            status_code=422, detail="Either use_managed_identity must be enabled, or app_id and app_secret are required"
        )
    if existing:
        existing.app_id = app_id if not use_managed_identity else existing.app_id
        existing.app_secret = app_secret if not use_managed_identity else existing.app_secret
        existing.is_configured = True
        # Store tenant_id and use_managed_identity in extra_config
        if not existing.extra_config:
            existing.extra_config = {}
        if tenant_id:
            existing.extra_config["tenant_id"] = tenant_id
        elif "tenant_id" in existing.extra_config and not tenant_id:
            # Remove tenant_id if not provided (use default)
            existing.extra_config.pop("tenant_id", None)
        existing.extra_config["use_managed_identity"] = use_managed_identity
        await db.flush()
        return ChannelConfigOut.model_validate(existing)

    extra_config = {}
    if tenant_id:
        extra_config["tenant_id"] = tenant_id
    if use_managed_identity:
        extra_config["use_managed_identity"] = True

    config = ChannelConfig(
        agent_id=agent_id,
        tenant_id=agent.tenant_id,
        channel_type="microsoft_teams",
        app_id=app_id if not use_managed_identity else None,
        app_secret=app_secret if not use_managed_identity else None,
        is_configured=True,
        extra_config=extra_config,
    )
    db.add(config)
    await db.flush()
    return ChannelConfigOut.model_validate(config)


@router.get("/agents/{agent_id}/teams-channel", response_model=ChannelConfigOut)
async def get_teams_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get Microsoft Teams channel configuration for an agent."""
    await check_agent_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "microsoft_teams",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    return ChannelConfigOut.model_validate(config).to_safe()


@router.get("/agents/{agent_id}/teams-channel/webhook-url")
async def get_teams_webhook_url(
    agent_id: uuid.UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the Microsoft Teams webhook URL for an agent."""
    await check_agent_access(db, current_user, agent_id)
    import os
    from app.models.system_settings import SystemSetting

    public_base = ""
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == "platform"))
    setting = result.scalar_one_or_none()
    if setting and setting.value.get("public_base_url"):
        public_base = setting.value["public_base_url"].rstrip("/")
    if not public_base:
        public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not public_base:
        # Use request.base_url
        base_url = str(request.base_url).rstrip("/")
        from urllib.parse import urlparse

        parsed = urlparse(base_url)

        # Only adjust port for localhost/127.0.0.1 (local development)
        # In production with reverse proxy, trust the request.base_url (port 80/443)
        if parsed.hostname in ("localhost", "127.0.0.1"):
            # Local development: if port is 80, 3008 (frontend), or missing, use backend port 8000
            if parsed.port in (80, 3008, None):
                public_base = f"{parsed.scheme}://{parsed.hostname}:8000"
            else:
                public_base = base_url
        else:
            # Production: trust the request.base_url (from reverse proxy headers)
            # It will have the correct public port (80/443)
            public_base = base_url
    return {"webhook_url": f"{public_base}/api/channel/teams/{agent_id}/webhook"}


@router.delete("/agents/{agent_id}/teams-channel", status_code=204)
async def delete_teams_channel(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete Microsoft Teams channel configuration for an agent."""
    agent = await require_agent_manage_access(db, current_user, agent_id)
    result = await db.execute(
        select(ChannelConfig).where(
            ChannelConfig.agent_id == agent_id,
            ChannelConfig.channel_type == "microsoft_teams",
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Microsoft Teams not configured")
    from app.services.external_principal_service import revoke_channel_config_external_principals

    await revoke_channel_config_external_principals(
        db,
        tenant_id=agent.tenant_id,
        config=config,
        actor_user_id=current_user.id,
        reason="Microsoft Teams channel configuration deleted",
    )
    await db.delete(config)
    await db.commit()


# ─── Event Webhook ──────────────────────────────────────


@router.post("/channel/teams/{agent_id}/webhook")
async def teams_event_webhook(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Microsoft Teams Bot Framework callbacks."""
    try:
        body_bytes = await request.body()
        replay_event_id = getattr(getattr(request, "state", None), "channel_ingress_event_id", None)
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError as e:
            logger.error(f"Teams: Failed to parse JSON body: {e}, body={body_bytes[:200]}")
            return Response(status_code=400, content="Invalid JSON")

        # Microsoft Teams Bot Framework sends the activity directly in the body (not wrapped in "activity" key)
        # Check if body itself is the activity (has "type" field) or if it's wrapped
        if isinstance(body, dict) and "type" in body:
            activity = body
        elif isinstance(body, dict) and "activity" in body:
            activity = body["activity"]
        else:
            logger.warning(
                f"Teams: Unexpected body structure for agent {agent_id}: {list(body.keys()) if isinstance(body, dict) else type(body)}"
            )
            activity = body if isinstance(body, dict) else {}

        logger.info(
            f"Teams: Webhook received for agent {agent_id}, activity type={activity.get('type')}, from={activity.get('from', {}).get('id', 'unknown')}, text={activity.get('text', '')[:50] if activity.get('text') else 'no text'}"
        )

        # Teams Bot Framework uses a simple token for authentication, not HMAC for incoming webhooks
        # For now, we rely on the unguessable URL token.
        # In a full production setup, you'd validate the JWT token in the Authorization header.

        # Get channel config and pin tenant RLS for this public webhook.
        config = await load_public_agent_channel_config(
            db,
            agent_id=agent_id,
            channel_type="microsoft_teams",
        )
        if not config:
            logger.warning(f"Teams: Webhook received for unconfigured agent {agent_id}")
            return Response(status_code=404)
        agent_tenant_id = config.tenant_id
        if not agent_tenant_id:
            _agent_obj = await db.get(AgentModel, agent_id)
            agent_tenant_id = _agent_obj.tenant_id if _agent_obj else None

        # Extract serviceUrl from the activity for sending replies
        service_url = activity.get("serviceUrl")
        if service_url:
            if config.extra_config.get("service_url") != service_url:
                config.extra_config["service_url"] = service_url
                config.is_connected = True
                await db.flush()
                await db.commit()
                logger.info(f"Teams: Updated service_url for agent {agent_id} to {service_url}")

        activity_id = activity.get("id")

        # Only process message activities
        if activity.get("type") != "message":
            return {"ok": True}

        # Ignore bot's own messages
        # Check if the message is from the bot itself (either by app_id or by comparing with recipient)
        bot_id = config.app_id
        if not bot_id:
            # If no app_id, use the recipient ID from the activity (the bot is the recipient)
            bot_id = activity.get("recipient", {}).get("id")
        if bot_id and activity.get("from", {}).get("id") == bot_id:
            return {"ok": True}

        user_text = activity.get("text", "").strip()
        if not user_text:
            return {"ok": True}

        # Extract conversation and sender info
        conversation_id = activity.get("conversation", {}).get("id")
        sender_id = activity.get("from", {}).get("id")
        sender_name = activity.get("from", {}).get("name", f"Teams User {sender_id[:8]}")
        reply_to_id = activity.get("id")  # The ID of the incoming message to reply to

        if not conversation_id or not sender_id:
            logger.warning(f"Teams: Missing conversation_id or sender_id in activity for agent {agent_id}")
            return {"ok": True}

        if replay_event_id is None:
            from app.services.channel_ingress_inbox import accept_authenticated_channel_event, channel_installation_ref

            await accept_authenticated_channel_event(
                db,
                tenant_id=agent_tenant_id,
                agent_id=agent_id,
                provider="teams",
                installation_ref=channel_installation_ref(config, fallback=f"teams:{agent_id}"),
                provider_event_id=str(activity_id or ""),
                handler_key="teams.activity",
                body=body,
                metadata={"transport": "bot_framework_webhook"},
            )
            return {"ok": True}

        logger.info(f"Teams: Message from={sender_id}, conversation={conversation_id}: {user_text[:80]}")

        from app.services.channel_ingress_inbox import channel_installation_ref
        from app.services.external_principal_service import resolve_or_create_external_principal

        principal_resolution = await resolve_or_create_external_principal(
            db,
            tenant_id=agent_tenant_id,
            provider="teams",
            installation_ref=channel_installation_ref(config, fallback=f"teams:{agent_id}"),
            channel_config_id=getattr(config, "id", None),
            subject_id=sender_id,
            display_name=sender_name,
            profile={"conversation_id": conversation_id},
        )
        runtime_actor = principal_resolution.actor
        platform_user_id = runtime_actor.id
        external_principal_id = principal_resolution.principal.id
        bot_channel_account = activity.get("recipient", {}) or {}
        if not bot_channel_account.get("id") and config.app_id:
            bot_channel_account = {"id": config.app_id}
        user_account = activity.get("from", {}) or {"id": sender_id, "name": sender_name}
        delivery_target = {
            "channel": "microsoft_teams",
            "conversation_id": conversation_id,
            "reply_to_id": reply_to_id,
            "sender_id": sender_id,
            "recipient_id": user_account.get("id") or sender_id,
            "recipient_name": user_account.get("name") or sender_name,
            "bot_id": bot_channel_account.get("id") or config.app_id,
            "user_label": runtime_actor.display_name or sender_name,
            "external_principal_id": str(external_principal_id),
        }

        # Find-or-create session for this Teams conversation
        sess = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            tenant_id=agent_tenant_id,
            user_id=platform_user_id,
            external_principal_id=external_principal_id,
            external_conv_id=f"teams_{conversation_id}_{sender_id}",
            source_channel="microsoft_teams",
            first_message_title=user_text,
            delivery_target=delivery_target,
        )
        session_conv_id = str(sess.id)
        delivery_target["session_id"] = session_conv_id
        sess.delivery_target_json = delivery_target

        # Load history
        from app.services.memory_service import compute_history_limit_for_agent

        _hist_limit = await compute_history_limit_for_agent(agent_id)

        history_r = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == session_conv_id)
            .order_by(ChatMessage.created_at.desc())
            .limit(_hist_limit)
        )
        history = [{"role": m.role, "content": m.content} for m in reversed(history_r.scalars().all())]

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
        sess.last_message_at = datetime.now(timezone.utc)
        await db.commit()

        # Set channel_file_sender contextvar for agent → user file delivery
        async def _teams_file_sender(file_path, msg: str = ""):
            _fp = _Path(file_path)
            use_mi = config.extra_config.get("use_managed_identity", False)
            has_creds = (config.app_id and config.app_secret) or use_mi
            if not has_creds or not conversation_id:
                return
            # For simplicity, just send file info as text for now
            file_msg_activity = {
                "type": "message",
                "conversation": {"id": conversation_id},
                "replyToId": reply_to_id,
                "text": f"Agent sent file: {_fp.name} (Note: file content not directly supported yet, but I can tell you about it: {msg})",
            }
            await _send_teams_message(config, conversation_id, file_msg_activity)

        _cfs_s_token = _cfs_s.set(_teams_file_sender)
        from app.services.channel_delivery_service import channel_delivery_target as _cdt_s

        _cdt_s_token = _cdt_s.set(delivery_target)

        # Call LLM
        try:
            reply_text = await call_agent_llm(
                db,
                agent_id,
                user_text,
                history=history,
                user_id=platform_user_id,
                session_id=session_conv_id,
                session_source="microsoft_teams",
                session_channel="microsoft_teams",
                allow_bare_plan_confirmation=True,
                durable_run=True,
                durable_session=sess,
                durable_user=runtime_actor,
            )
            logger.info(f"Teams: LLM reply generated: {reply_text[:80]}")
        except Exception as e:
            logger.error(f"Teams: Failed to call LLM for agent {agent_id}: {e}", exc_info=True)
            reply_text = "Sorry, I encountered an error processing your message."
        finally:
            _cdt_s.reset(_cdt_s_token)
            _cfs_s.reset(_cfs_s_token)

        # Transport receipts are not assistant-authored conversation content.
        try:
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
            sess.last_message_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info(f"Teams: Saved reply to database for conversation {conversation_id}")
        except Exception as e:
            logger.error(f"Teams: Failed to save reply to database: {e}", exc_info=True)
            await db.rollback()

        # Send to Teams
        use_managed_identity = config.extra_config.get("use_managed_identity", False)
        has_credentials = (config.app_id and config.app_secret) or use_managed_identity
        if has_credentials and conversation_id:
            try:
                reply_activity = {
                    "type": "message",
                    "from": bot_channel_account,  # Required: Bot's channel account ID (from incoming activity's recipient)
                    "conversation": {"id": conversation_id},
                    "recipient": user_account,  # The user who sent the message (from incoming activity's from)
                    "replyToId": reply_to_id,  # Reply to the specific incoming message
                    "text": reply_text,
                }
                logger.info(
                    f"Teams: Attempting to send reply to conversation {conversation_id}, from={bot_channel_account.get('id')}, recipient={user_account.get('id')}"
                )
                await _send_teams_message(config, conversation_id, reply_activity)
                logger.info("Teams: Successfully sent reply to Teams")
            except Exception as e:
                logger.error(f"Teams: Failed to send message to Teams: {e}", exc_info=True)
        else:
            use_mi = config.extra_config.get("use_managed_identity", False)
            logger.warning(
                f"Teams: Cannot send reply - missing credentials (managed_identity={use_mi}, app_id={bool(config.app_id)}, app_secret={bool(config.app_secret)}), conversation_id={bool(conversation_id)}"
            )

        return {"ok": True}
    except Exception as e:
        logger.error(f"Teams: Unhandled exception in webhook handler for agent {agent_id}: {e}", exc_info=True)
        return Response(status_code=500, content="Internal server error")
