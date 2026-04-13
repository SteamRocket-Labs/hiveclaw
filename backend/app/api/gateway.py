"""Gateway API for OpenClaw agent communication.

OpenClaw agents authenticate via X-Api-Key header and use these endpoints
to poll for messages, report results, send messages, and send heartbeat pings.
"""

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage
from app.models.user import User
from app.services.session_service import find_or_create_agent_pair_session, session_conversation_id
from app.schemas.schemas import (
    GatewayPollResponse, GatewayMessageOut, GatewayReportRequest,
    GatewayHistoryItem, GatewayRelationshipItem, GatewaySendMessageRequest,
)

router = APIRouter(prefix="/gateway", tags=["gateway"])


def _hash_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def _legacy_gateway_conversation_ids(source_agent_id: str | uuid.UUID, target_agent_id: str | uuid.UUID) -> list[str]:
    source = str(source_agent_id)
    target = str(target_agent_id)
    return [
        f"gw_agent_{source}_{target}",
        f"gw_agent_{target}_{source}",
    ]


async def _find_or_create_gateway_agent_pair_session(
    db: AsyncSession,
    *,
    source_agent_id: uuid.UUID,
    source_agent_name: str,
    target_agent_id: uuid.UUID,
    target_agent_name: str,
    owner_user_id: uuid.UUID,
    migrate_legacy_transcripts: bool = False,
):
    session = await find_or_create_agent_pair_session(
        db,
        source_agent_id=source_agent_id,
        target_agent_id=target_agent_id,
        owner_user_id=owner_user_id,
        source_name=source_agent_name,
        target_name=target_agent_name,
    )
    conv_id = session_conversation_id(session)

    if migrate_legacy_transcripts:
        from app.models.audit import ChatMessage

        await db.execute(
            update(ChatMessage)
            .where(ChatMessage.conversation_id.in_(_legacy_gateway_conversation_ids(source_agent_id, target_agent_id)))
            .values(conversation_id=conv_id)
        )

    return session, conv_id


async def _get_agent_by_key(api_key: str, db: AsyncSession) -> Agent:
    """Authenticate an OpenClaw agent by its API key."""
    key_hash = _hash_key(api_key)
    result = await db.execute(
        select(Agent).where(
            Agent.api_key_hash == key_hash,
            Agent.agent_type == "openclaw",
        )
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return agent


# ─── Poll for messages ──────────────────────────────────

@router.get("/poll", response_model=GatewayPollResponse)
async def poll_messages(
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent polls for pending messages.

    Returns all pending messages and marks them as delivered.
    Also updates openclaw_last_seen for online status tracking.
    """
    logger.info(f"[Gateway] poll called, key_prefix={x_api_key[:8]}...")
    agent = await _get_agent_by_key(x_api_key, db)

    # Update last seen
    agent.openclaw_last_seen = datetime.now(timezone.utc)
    agent.status = "running"

    # Fetch pending messages
    result = await db.execute(
        select(GatewayMessage)
        .where(GatewayMessage.agent_id == agent.id, GatewayMessage.status == "pending")
        .order_by(GatewayMessage.created_at.asc())
    )
    messages = result.scalars().all()

    # Mark as delivered
    now = datetime.now(timezone.utc)
    out = []
    for msg in messages:
        msg.status = "delivered"
        msg.delivered_at = now

        # Resolve sender names
        sender_agent_name = None
        sender_user_name = None
        if msg.sender_agent_id:
            r = await db.execute(select(Agent.name).where(Agent.id == msg.sender_agent_id))
            sender_agent_name = r.scalar_one_or_none()
        if msg.sender_user_id:
            r = await db.execute(select(User.display_name).where(User.id == msg.sender_user_id))
            sender_user_name = r.scalar_one_or_none()

        # Fetch conversation history (last 10 messages) for context
        history = []
        if msg.conversation_id:
            from app.models.audit import ChatMessage
            hist_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == msg.conversation_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(10)
            )
            hist_msgs = list(reversed(hist_result.scalars().all()))
            for h in hist_msgs:
                # Resolve sender name for each history message
                h_sender = None
                if h.role == "user" and h.user_id:
                    r = await db.execute(select(User.display_name).where(User.id == h.user_id))
                    h_sender = r.scalar_one_or_none()
                elif h.role == "assistant":
                    h_sender = agent.name
                history.append(GatewayHistoryItem(
                    role=h.role,
                    content=h.content or "",
                    sender_name=h_sender,
                    created_at=h.created_at,
                ))

        out.append(GatewayMessageOut(
            id=msg.id,
            conversation_id=msg.conversation_id,
            sender_agent_name=sender_agent_name,
            sender_user_name=sender_user_name,
            sender_user_id=str(msg.sender_user_id) if msg.sender_user_id else None,
            content=msg.content,
            created_at=msg.created_at,
            history=history,
        ))

    # Fetch agent relationships for context
    from app.models.org import AgentRelationship, AgentAgentRelationship
    from sqlalchemy.orm import selectinload

    rel_items = []

    # Human relationships (with available channels)
    h_result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentRelationship.member))
    )
    for r in h_result.scalars().all():
        if r.member:
            channels = []
            if getattr(r.member, 'feishu_user_id', None) or getattr(r.member, 'feishu_open_id', None):
                channels.append("feishu")
            if getattr(r.member, 'email', None):
                channels.append("email")
            rel_items.append(GatewayRelationshipItem(
                name=r.member.name,
                type="human",
                role=r.relation,
                description=r.description or None,
                channels=channels,
            ))

    # Agent-to-agent relationships
    a_result = await db.execute(
        select(AgentAgentRelationship)
        .where(AgentAgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentAgentRelationship.target_agent))
    )
    for r in a_result.scalars().all():
        if r.target_agent:
            rel_items.append(GatewayRelationshipItem(
                name=r.target_agent.name,
                type="agent",
                role=r.relation,
                description=r.description or None,
                channels=["agent"],
            ))

    await db.commit()
    return GatewayPollResponse(messages=out, relationships=rel_items)


# ─── Report results ─────────────────────────────────────

@router.post("/report")
async def report_result(
    body: GatewayReportRequest,
    x_api_key: str = Header(None, alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent reports the result of a processed message."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")
    logger.info(f"[Gateway] report called, key_prefix={x_api_key[:8]}..., msg_id={body.message_id}")
    agent = await _get_agent_by_key(x_api_key, db)

    result = await db.execute(
        select(GatewayMessage).where(
            GatewayMessage.id == body.message_id,
            GatewayMessage.agent_id == agent.id,
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    msg.status = "completed"
    msg.result = body.result
    msg.completed_at = datetime.now(timezone.utc)

    # Update last seen
    agent.openclaw_last_seen = datetime.now(timezone.utc)

    # Save result as assistant chat message and push via WebSocket
    # (only for user-originated messages; agent-to-agent skips this)
    if body.result and msg.conversation_id and msg.sender_user_id:
        from app.models.audit import ChatMessage
        assistant_msg = ChatMessage(
            agent_id=agent.id,
            user_id=msg.sender_user_id,
            role="assistant",
            content=body.result,
            conversation_id=msg.conversation_id,
        )
        db.add(assistant_msg)

    await db.commit()

    # Push to WebSocket if user is connected
    if body.result and msg.conversation_id and msg.sender_user_id:
        try:
            from app.api.websocket import manager
            await manager.send_message(str(agent.id), {
                "type": "done",
                "role": "assistant",
                "content": body.result,
            })
        except Exception:
            pass  # User may have disconnected

    # If the original message was from another agent (OpenClaw-to-OpenClaw),
    # write the reply back as a gateway_message for the sender agent to poll
    if body.result and msg.sender_agent_id:
        async with async_session() as reply_db:
            conv_id = msg.conversation_id
            if not conv_id or conv_id.startswith("gw_agent_"):
                sender_result = await reply_db.execute(select(Agent).where(Agent.id == msg.sender_agent_id))
                sender_agent = sender_result.scalar_one_or_none()
                owner_user_id = agent.creator_id or getattr(sender_agent, "creator_id", None) or msg.sender_agent_id
                _, conv_id = await _find_or_create_gateway_agent_pair_session(
                    reply_db,
                    source_agent_id=msg.sender_agent_id,
                    source_agent_name=getattr(sender_agent, "name", "Agent"),
                    target_agent_id=agent.id,
                    target_agent_name=agent.name,
                    owner_user_id=owner_user_id,
                    migrate_legacy_transcripts=bool(msg.conversation_id),
                )
            gw_reply = GatewayMessage(
                agent_id=msg.sender_agent_id,
                sender_agent_id=agent.id,
                content=body.result,
                status="pending",
                conversation_id=conv_id,
            )
            reply_db.add(gw_reply)
            await reply_db.commit()
            logger.info(f"[Gateway] Reply routed back to sender agent {msg.sender_agent_id}")

    return {"status": "ok"}


# ─── Heartbeat ──────────────────────────────────────────

@router.post("/heartbeat")
async def heartbeat(
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Pure heartbeat ping — keeps the OpenClaw agent marked as online."""
    agent = await _get_agent_by_key(x_api_key, db)
    agent.openclaw_last_seen = datetime.now(timezone.utc)
    agent.status = "running"
    await db.commit()
    return {"status": "ok", "agent_id": str(agent.id)}


# ─── Send message ───────────────────────────────────────

# Track background tasks to prevent garbage collection
_background_tasks: set = set()

async def _send_to_agent_background(
    source_agent_id: str,
    source_agent_name: str,
    target_agent_id: str,
    target_agent_name: str,
    target_primary_model_id: str,
    target_role_description: str,
    target_creator_id: str,
    target_tenant_id: str,
    content: str,
):
    """Background task: invoke target agent LLM and write reply to gateway_messages.
    
    Accepts plain values (not ORM objects) to avoid stale session references
    since this runs after the request's DB session has closed.
    """
    logger.info(f"[Gateway] _send_to_agent_background started: {source_agent_name} -> {target_agent_name}")
    try:
        from app.api.websocket import call_llm
        from app.kernel.contracts import ExecutionIdentityRef
        from app.models.llm import LLMModel
        from app.models.audit import ChatMessage

        async with async_session() as db:
            # Load target agent's LLM model
            if not target_primary_model_id or not target_tenant_id:
                logger.warning(f"Target agent {target_agent_name} has no LLM model or tenant")
                return
            result = await db.execute(
                select(LLMModel).where(LLMModel.id == target_primary_model_id, LLMModel.tenant_id == target_tenant_id)
            )
            model = result.scalar_one_or_none()
            if not model:
                return

            source_agent_uuid = uuid.UUID(str(source_agent_id))
            target_agent_uuid = uuid.UUID(str(target_agent_id))
            target_creator_uuid = uuid.UUID(str(target_creator_id))
            session, conv_id = await _find_or_create_gateway_agent_pair_session(
                db,
                source_agent_id=source_agent_uuid,
                source_agent_name=source_agent_name,
                target_agent_id=target_agent_uuid,
                target_agent_name=target_agent_name,
                owner_user_id=target_creator_uuid,
                migrate_legacy_transcripts=True,
            )
            session_agent_id = session.agent_id
            session.last_message_at = datetime.now(timezone.utc)

            # Load recent conversation history for context
            hist_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(10)
            )
            hist_msgs = list(reversed(hist_result.scalars().all()))

            memory_messages: list[dict] = []
            messages: list[dict] = []
            for h in hist_msgs:
                history_entry = {"role": h.role, "content": h.content or ""}
                messages.append(history_entry)
                memory_messages.append(history_entry)

            # Add the new message
            user_msg = f"[Message from agent: {source_agent_name}]\n{content}"
            user_entry = {"role": "user", "content": user_msg}
            messages.append(user_entry)
            memory_messages.append(user_entry)

            # Save user message to conversation
            db.add(ChatMessage(
                agent_id=session_agent_id,
                conversation_id=conv_id,
                role="user",
                content=user_msg,
                user_id=target_creator_uuid,
            ))
            await db.commit()

        # Call LLM
        collected = []
        async def on_chunk(text):
            collected.append(text)

        reply = await call_llm(
            model=model,
            messages=messages,
            agent_name=target_agent_name,
            role_description=target_role_description,
            agent_id=target_agent_uuid,
            user_id=target_creator_uuid,
            on_chunk=on_chunk,
            session_id=conv_id,
            memory_messages=memory_messages,
            execution_identity=ExecutionIdentityRef(
                identity_type="agent_bot",
                identity_id=source_agent_uuid,
                label=f"Agent: {source_agent_name} (agent_message)",
            ),
            auto_close_session=True,
            session_source="gateway",
            session_channel="gateway",
        )
        final_reply = reply or "".join(collected)

        # Save assistant reply to conversation
        async with async_session() as db:
            db.add(ChatMessage(
                agent_id=session_agent_id,
                conversation_id=conv_id,
                role="assistant",
                content=final_reply,
                user_id=target_creator_uuid,
            ))

            # Write reply to gateway_messages for source (OpenClaw) to poll
            gw_reply = GatewayMessage(
                agent_id=source_agent_uuid,
                sender_agent_id=target_agent_uuid,
                content=final_reply,
                status="pending",
                conversation_id=conv_id,
            )
            db.add(gw_reply)
            await db.commit()

        logger.info(f"[Gateway] Agent {target_agent_name} replied to {source_agent_name}")

    except Exception as e:
        logger.error(f"[Gateway] send_to_agent_background failed: {e}")
        import traceback
        traceback.print_exc()


@router.post("/send-message")
async def send_message(
    body: GatewaySendMessageRequest,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """OpenClaw agent sends a message to a person or another agent.

    Routes automatically based on target type:
    - Agent target: triggers LLM processing, reply returned via next poll
    - Human target: sends via available channel (feishu, etc.)
    """
    agent = await _get_agent_by_key(x_api_key, db)
    agent.openclaw_last_seen = datetime.now(timezone.utc)

    target_name = body.target.strip()
    content = body.content.strip()
    channel_hint = (body.channel or "").strip().lower()

    # 1. Try to find target as another Agent
    result = await db.execute(
        select(Agent).where(Agent.name.ilike(f"%{target_name}%"))
    )
    target_agent = result.scalars().first()

    logger.info(f"[Gateway] send_message: target='{target_name}', found_agent={target_agent.name if target_agent else None}, agent_type={getattr(target_agent, 'agent_type', None) if target_agent else None}, channel_hint='{channel_hint}'")

    if target_agent and (not channel_hint or channel_hint == "agent"):
        owner_user_id = target_agent.creator_id or agent.creator_id or agent.id
        chat_session, conv_id = await _find_or_create_gateway_agent_pair_session(
            db,
            source_agent_id=agent.id,
            source_agent_name=agent.name,
            target_agent_id=target_agent.id,
            target_agent_name=target_agent.name,
            owner_user_id=owner_user_id,
        )
        chat_session.last_message_at = datetime.now(timezone.utc)

        if getattr(target_agent, 'agent_type', None) == 'openclaw':
            # OpenClaw-to-OpenClaw: write to gateway_messages directly
            gw_msg = GatewayMessage(
                agent_id=target_agent.id,
                sender_agent_id=agent.id,
                content=content,
                status="pending",
                conversation_id=conv_id,
            )
            db.add(gw_msg)
            await db.commit()
            return {
                "status": "accepted",
                "target": target_agent.name,
                "type": "openclaw_agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
            }
        else:
            # Native agent: async LLM processing
            # Extract plain values before session closes to avoid stale ORM references
            _src_id = str(agent.id)
            _src_name = agent.name
            _tgt_id = str(target_agent.id)
            _tgt_name = target_agent.name
            _tgt_model = str(target_agent.primary_model_id) if target_agent.primary_model_id else ""
            _tgt_role = target_agent.role_description or ""
            _tgt_creator = str(owner_user_id)
            _tgt_tenant = str(target_agent.tenant_id) if target_agent.tenant_id else ""
            await db.commit()
            task = asyncio.create_task(_send_to_agent_background(
                _src_id, _src_name, _tgt_id, _tgt_name,
                _tgt_model, _tgt_role, _tgt_creator, _tgt_tenant, content,
            ))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            return {
                "status": "accepted",
                "target": target_agent.name,
                "type": "agent",
                "message": f"Message sent to {target_agent.name}. Reply will appear in your next poll.",
            }

    # 2. Try to find target as a human (via relationships)
    from app.models.org import AgentRelationship
    from sqlalchemy.orm import selectinload

    rel_result = await db.execute(
        select(AgentRelationship)
        .where(AgentRelationship.agent_id == agent.id)
        .options(selectinload(AgentRelationship.member))
    )
    rels = rel_result.scalars().all()

    target_member = None
    for r in rels:
        if r.member and r.member.name == target_name:
            target_member = r.member
            break
    # Fuzzy match if exact match fails
    if not target_member:
        for r in rels:
            if r.member and target_name.lower() in r.member.name.lower():
                target_member = r.member
                break

    if not target_member:
        await db.commit()
        raise HTTPException(
            status_code=404,
            detail=f"Target '{target_name}' not found. Check your relationships list."
        )

    stable_user_id = target_member.external_id or target_member.feishu_user_id
    stable_open_id = target_member.open_id or target_member.feishu_open_id

    # Send via feishu if available
    if (stable_user_id or stable_open_id) and (not channel_hint or channel_hint == "feishu"):
        from app.models.channel_config import ChannelConfig
        from app.services.feishu_service import feishu_service
        import json as _json

        config_result = await db.execute(
            select(ChannelConfig).where(ChannelConfig.agent_id == agent.id)
        )
        config = config_result.scalar_one_or_none()
        if not config:
            # Try to find any feishu config in the org
            config_result = await db.execute(
                select(ChannelConfig).where(ChannelConfig.channel == "feishu").limit(1)
            )
            config = config_result.scalar_one_or_none()

        if not config:
            await db.commit()
            raise HTTPException(status_code=400, detail="No Feishu channel configured")

        # Prefer user_id (tenant-stable, works across apps), fallback to open_id
        resp = None
        if stable_user_id:
            resp = await feishu_service.send_message(
                config.app_id, config.app_secret,
                receive_id=stable_user_id,
                msg_type="text",
                content=_json.dumps({"text": content}, ensure_ascii=False),
                receive_id_type="user_id",
            )
        if (resp is None or resp.get("code") != 0) and stable_open_id:
            resp = await feishu_service.send_message(
                config.app_id, config.app_secret,
                receive_id=stable_open_id,
                msg_type="text",
                content=_json.dumps({"text": content}, ensure_ascii=False),
                receive_id_type="open_id",
            )
        await db.commit()

        if resp and resp.get("code") == 0:
            return {
                "status": "sent",
                "target": target_member.name,
                "type": "human",
                "channel": "feishu",
            }
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Feishu send failed: {resp.get('msg') if resp else 'no ID available'} (code {resp.get('code') if resp else 'N/A'})"
            )

    await db.commit()
    raise HTTPException(
        status_code=400,
        detail=(
            f"No available channel to reach {target_member.name}. "
            f"feishu_user_id={'yes' if stable_user_id else 'no'}, "
            f"feishu_open_id={'yes' if stable_open_id else 'no'}"
        ),
    )


# ─── Setup guide ────────────────────────────────────────

@router.get("/setup-guide/{agent_id}")
async def get_setup_guide(
    agent_id: uuid.UUID,
    x_api_key: str = Header(..., alias="X-Api-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Return the pre-filled Skill file and Heartbeat instruction for this agent."""
    agent = await _get_agent_by_key(x_api_key, db)
    if agent.id != agent_id:
        raise HTTPException(status_code=403, detail="Key does not match this agent")

    # Note: we use the raw key from the header since the agent already authenticated
    base_url = "https://try.hive.ai"

    skill_content = f"""---
name: hive_sync
description: Sync with Hive platform — check inbox, submit results, and send messages.
---

# Hive Sync

## When to use
Check for new messages from the Hive platform during every heartbeat cycle.
You can also proactively send messages to people and agents in your relationships.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: {base_url}/api/gateway/poll
- Header: X-Api-Key: {x_api_key}

The response contains a `messages` array. Each message includes:
- `id` — unique message ID (use this for reporting)
- `content` — the message text
- `sender_user_name` — name of the Hive user who sent it
- `sender_user_id` — unique ID of the sender
- `conversation_id` — the conversation this message belongs to
- `history` — array of previous messages in this conversation for context

The response also contains a `relationships` array describing your colleagues:
- `name` — the person or agent name
- `type` — "human" or "agent"
- `role` — relationship type (e.g. collaborator, supervisor)
- `channels` — available communication channels (e.g. ["feishu"], ["agent"])

**IMPORTANT**: Use the `history` array to understand conversation context before replying.
Different `sender_user_name` values mean different people — address them accordingly.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: {base_url}/api/gateway/report
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"message_id": "<id from the message>", "result": "<your response>"}}

### 3. Send a message to someone
To proactively contact a person or agent, make an HTTP POST request:
- URL: {base_url}/api/gateway/send-message
- Header: X-Api-Key: {x_api_key}
- Header: Content-Type: application/json
- Body: {{"target": "<name of person or agent>", "content": "<your message>"}}

The system auto-detects the best channel. For agents, the reply appears in your next poll.
For humans, the message is delivered via their available channel (e.g. Feishu).
"""

    heartbeat_line = "- Check Hive inbox using the hive_sync skill and process any pending messages"

    return {
        "skill_filename": "hive_sync.md",
        "skill_content": skill_content,
        "heartbeat_addition": heartbeat_line,
    }
