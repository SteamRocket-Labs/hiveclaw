"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import os
import uuid

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import get_current_user
from app.database import async_session, get_db
from app.kernel.contracts import ExecutionIdentityRef
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.user import User
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.llm_error_policy import is_llm_error_message
from app.services.web_chat_broker import web_chat_broker
from app.services.web_chat_runtime import (
    ActiveWebChatRunExists,
    cancel_web_chat_run,
    conversation_from_history_messages as _conversation_from_history_messages,
    get_active_web_chat_run,
    start_web_chat_run,
)
from app.services.web_session_contract import apply_web_session_contract

router = APIRouter(tags=["websocket"])


class ConnectionManager:
    """Manage WebSocket connections per agent."""

    def __init__(self):
        # agent_id_str -> list of (WebSocket, session_id_str | None)
        self.active_connections: dict[str, list[tuple]] = {}
        self._runtime_sessions: dict[str, SessionContext] = {}
        self._runtime_session_order: list[str] = []
        self._lock = asyncio.Lock()

    def _runtime_session_key(self, agent_id: str, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return f"{agent_id}:{session_id}"

    async def connect(self, agent_id: str, websocket: WebSocket, session_id: str | None = None):
        await websocket.accept()
        async with self._lock:
            if agent_id not in self.active_connections:
                self.active_connections[agent_id] = []
            self.active_connections[agent_id].append((websocket, session_id))

    async def disconnect(self, agent_id: str, websocket: WebSocket):
        async with self._lock:
            if agent_id in self.active_connections:
                self.active_connections[agent_id] = [
                    (ws, sid) for ws, sid in self.active_connections[agent_id] if ws != websocket
                ]

    async def send_message(self, agent_id: str, message: dict):
        async with self._lock:
            if agent_id in self.active_connections:
                dead = []
                for ws, _sid in self.active_connections[agent_id]:
                    try:
                        await ws.send_json(message)
                    except Exception:
                        dead.append((ws, _sid))
                for d in dead:
                    self.active_connections[agent_id] = [
                        c for c in self.active_connections[agent_id] if c != d
                    ]

    async def get_active_session_ids(self, agent_id: str) -> list[str]:
        """Return distinct session IDs for all active WS connections of an agent."""
        async with self._lock:
            if agent_id not in self.active_connections:
                return []
            return list(set(sid for _ws, sid in self.active_connections[agent_id] if sid))

    async def get_or_create_runtime_session(self, agent_id: str, session_id: str | None) -> SessionContext:
        """Return a stable SessionContext for a chat session across turns/reconnects."""
        if not session_id:
            return SessionContext(source="web", channel="web")

        async with self._lock:
            key = self._runtime_session_key(agent_id, session_id)
            assert key is not None
            session = self._runtime_sessions.get(key)
            if session is None:
                session = SessionContext(
                    session_id=session_id,
                    source="web",
                    channel="web",
                )
                self._runtime_sessions[key] = session
                self._runtime_session_order.append(key)
                if len(self._runtime_session_order) > 200:
                    evict_key = self._runtime_session_order.pop(0)
                    self._runtime_sessions.pop(evict_key, None)
            else:
                if key in self._runtime_session_order:
                    self._runtime_session_order.remove(key)
                self._runtime_session_order.append(key)
            return session


manager = web_chat_broker


async def _claim_pending_reply_suffix_for_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: str | None,
) -> str:
    if not session_id:
        return ""

    from app.models.chat_session import ChatSession
    from app.services.pending_reply_service import (
        claim_and_fulfill_pending_replies,
        format_pending_reply_context,
        sender_identity_from_session,
    )

    session_result = await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(str(session_id))))
    session = session_result.scalar_one_or_none()
    sender_identity = sender_identity_from_session(session)
    if not sender_identity:
        return ""

    claimed = await claim_and_fulfill_pending_replies(db, agent_id=agent_id, sender_identity=sender_identity)
    if not claimed:
        return ""
    await db.commit()
    return format_pending_reply_context(claimed)


@router.get("/chat/{agent_id}/history")
async def get_chat_history(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return web chat message history for this user + agent."""
    from app.services.chat_message_parts import serialize_chat_message

    # check_agent_access already verifies tenant ownership (H-17)
    await check_agent_access(db, current_user, agent_id)
    conv_id = f"web_{current_user.id}"
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conv_id,
            ChatMessage.user_id == current_user.id,
        )
        .order_by(ChatMessage.created_at.asc())
        .limit(200)
    )
    messages = result.scalars().all()
    out = []
    for m in messages:
        out.append(serialize_chat_message(m))
    return out


async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    fallback_model: LLMModel | None = None,
    agent_id=None,
    user_id=None,
    on_chunk=None,
    on_tool_call=None,
    on_thinking=None,
    on_event=None,
    supports_vision=False,
    session_id: str | None = None,
    memory_messages: list[dict] | None = None,
    memory_context: str = "",
    cancel_event: asyncio.Event | None = None,
    execution_identity: ExecutionIdentityRef | None = None,
    session_context: SessionContext | None = None,
    auto_close_session: bool = False,
    session_source: str | None = None,
    session_channel: str | None = None,
    system_prompt_suffix: str = "",
    allowed_tool_names: tuple[str, ...] = (),
    excluded_tool_names: tuple[str, ...] = (),
) -> str:
    """Call LLM via the unified agent runtime."""
    runtime_messages = [msg for msg in messages if msg.get("role") != "system"]
    runtime_memory_messages = None
    if memory_messages is not None:
        runtime_memory_messages = [msg for msg in memory_messages if msg.get("role") != "system"]

    effective_session_context = session_context or SessionContext(
        session_id=session_id,
        source=session_source or "web",
        channel=session_channel or "web",
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            fallback_model=fallback_model,
            messages=runtime_messages,
            agent_name=agent_name,
            role_description=role_description,
            agent_id=agent_id,
            user_id=user_id,
            execution_identity=execution_identity,
            on_chunk=on_chunk,
            on_tool_call=on_tool_call,
            on_thinking=on_thinking,
            on_event=on_event,
            supports_vision=supports_vision,
            memory_session_id=session_id,
            memory_messages=runtime_memory_messages,
            cancel_event=cancel_event,
            session_context=effective_session_context,
            system_prompt_suffix=system_prompt_suffix,
            allowed_tool_names=allowed_tool_names,
            excluded_tool_names=excluded_tool_names,
        )
    )

    if auto_close_session and agent_id is not None:
        close_messages = list(runtime_memory_messages or runtime_messages)
        if not is_llm_error_message(result.content):
            close_messages.append({"role": "assistant", "content": result.content})
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.SESSION_CLOSE,
                agent_id=agent_id,
                session_id=effective_session_context.session_id or session_id,
                source=effective_session_context.source,
                messages=close_messages,
                metadata={
                    "reason": "invoke_complete",
                    "channel": effective_session_context.channel,
                },
            )
        except Exception as close_err:
            logger.debug("[call_llm] SESSION_CLOSE hook failed (non-fatal): {}", close_err)

    return result.content


@router.websocket("/ws/chat/{agent_id}")
async def websocket_chat(
    websocket: WebSocket,
    agent_id: uuid.UUID,
    token: str = Query(...),
    session_id: str = Query(None),
):
    """WebSocket endpoint for real-time chat with an agent.

    Flow:
    1. Client connects with JWT token + optional session_id as query params
    2. Server accepts immediately so browser onopen fires quickly
    3. Server authenticates and checks agent access
    4. If session_id provided, uses it; otherwise finds/creates the user's latest session
    5. Client sends messages as JSON: {"content": "..."}
    6. Server calls the agent's configured LLM and sends response back
    7. Messages are persisted to chat_messages table under the session
    """
    # Authenticate BEFORE accepting — reject unauthenticated connections immediately
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception:
        await websocket.accept()
        await websocket.send_json({"type": "error", "content": "Authentication failed"})
        await websocket.close(code=4001)
        return

    await websocket.accept()

    # Verify access and load agent + model
    agent_name = ""
    agent_type = ""  # Track agent type for OpenClaw routing
    welcome_message = ""
    llm_model = None
    fallback_llm_model = None
    history_messages = []

    try:
        async with async_session() as db:
            logger.info(f"[WS] Looking up user {user_id}")
            result = await db.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                logger.info("[WS] User not found")
                await websocket.send_json({"type": "error", "content": "User not found"})
                await websocket.close(code=4001)
                return

            # Set execution identity for audit trail
            from app.core.execution_context import set_delegated_user_identity
            set_delegated_user_identity(user.id, user.display_name or user.username, channel="web")

            logger.info(f"[WS] Checking agent access for {agent_id}")
            agent, _ = await check_agent_access(db, user, agent_id)
            # Check agent expiry
            if is_agent_expired(agent):
                await websocket.send_json({"type": "error", "content": "This Agent has expired and is off duty. Please contact your admin to extend its service."})
                await websocket.close(code=4003)
                return
            agent_name = agent.name
            agent_type = agent.agent_type or ""
            welcome_message = agent.welcome_message or ""
            logger.info(f"[WS] Agent: {agent_name}, type: {agent_type}, model_id: {agent.primary_model_id}")

            # Load the agent's primary model (tenant-scoped)
            if agent.primary_model_id:
                model_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                llm_model = model_result.scalar_one_or_none()
                logger.info(f"[WS] Primary model loaded: {llm_model.model if llm_model else 'None'}")

            # Load fallback model (tenant-scoped)
            if agent.fallback_model_id:
                fb_result = await db.execute(
                    select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
                )
                fallback_llm_model = fb_result.scalar_one_or_none()
                if fallback_llm_model:
                    logger.info(f"[WS] Fallback model loaded: {fallback_llm_model.model}")

            # Config-level fallback: primary missing -> use fallback
            if not llm_model and fallback_llm_model:
                llm_model = fallback_llm_model
                fallback_llm_model = None  # No further fallback available
                logger.info(f"[WS] Primary model unavailable, using fallback: {llm_model.model}")

            # Runtime fallback: if the configured fallback is missing/duplicated, use tenant default.
            if llm_model and agent.tenant_id:
                from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

                default_runtime_model = await resolve_default_model_for_tenant(
                    db,
                    agent.tenant_id,
                    exclude_model_id=llm_model.id,
                )
                previous_fallback = fallback_llm_model
                llm_model, fallback_llm_model = choose_runtime_model_pair(
                    llm_model,
                    fallback_llm_model,
                    default_runtime_model,
                )
                if fallback_llm_model and fallback_llm_model is not previous_fallback:
                    logger.info(f"[WS] Tenant default fallback loaded: {fallback_llm_model.model}")

            # Resolve or create chat session
            from app.models.chat_session import ChatSession
            from sqlalchemy import select as _sel
            from datetime import datetime as _dt, timezone as _tz
            conv_id = session_id
            _active_session = None
            if conv_id:
                # Validate the session belongs to this agent AND this user
                _sr = await db.execute(
                    _sel(ChatSession).where(
                        ChatSession.id == uuid.UUID(conv_id),
                        ChatSession.agent_id == agent_id,
                        ChatSession.user_id == user_id,
                    )
                )
                _existing = _sr.scalar_one_or_none()
                if not _existing:
                    conv_id = None  # fall through to create
                else:
                    _active_session = _existing
            if not conv_id:
                # Find most recent session for this user+agent
                _sr = await db.execute(
                    _sel(ChatSession)
                    .where(ChatSession.agent_id == agent_id, ChatSession.user_id == user_id)
                    .order_by(ChatSession.last_message_at.desc().nulls_last(), ChatSession.created_at.desc())
                    .limit(1)
                )
                _latest = _sr.scalar_one_or_none()
                if _latest:
                    conv_id = str(_latest.id)
                    _active_session = _latest
                else:
                    # Create a default session
                    now = _dt.now(_tz.utc)
                    _new_session = ChatSession(
                        agent_id=agent_id, user_id=user_id,
                        title=f"Session {now.strftime('%m-%d %H:%M')}",
                        source_channel="web",
                        created_at=now,
                    )
                    db.add(_new_session)
                    await db.commit()
                    await db.refresh(_new_session)
                    conv_id = str(_new_session.id)
                    _active_session = _new_session
                    logger.info(f"[WS] Created default session {conv_id}")

            if _active_session is not None:
                await apply_web_session_contract(db, session=_active_session, agent_id=agent_id, user=user)
                await db.commit()

            try:
                # Dynamic history limit based on model context window
                from app.services.memory_service import compute_history_limit
                _hist_limit = compute_history_limit(
                    llm_model.provider if llm_model else "openai",
                    llm_model.model if llm_model else "",
                    getattr(llm_model, "max_input_tokens", None) if llm_model else None,
                )
                history_result = await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.agent_id == agent_id, ChatMessage.conversation_id == conv_id)
                    .order_by(ChatMessage.created_at.desc())
                    .limit(_hist_limit)
                )
                history_messages = list(reversed(history_result.scalars().all()))
                logger.info(f"[WS] Loaded {len(history_messages)}/{_hist_limit} history messages for session {conv_id}")
            except Exception as e:
                logger.warning(f"[WS] History load failed (non-fatal): {e}")
    except Exception as e:
        logger.error(f"[WS] Setup error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        await websocket.send_json({"type": "error", "content": "Setup failed"})
        await websocket.close(code=4002)  # Config error — client should NOT retry
        return

    agent_id_str = str(agent_id)
    if agent_id_str not in manager.active_connections:
        manager.active_connections[agent_id_str] = []
    manager.active_connections[agent_id_str].append((websocket, conv_id))
    logger.info(f"[WS] Ready! Agent={agent_name}")

    # Build conversation context from history.
    # IMPORTANT: Include tool_call messages so the LLM maintains tool-calling behavior.
    conversation = _conversation_from_history_messages(history_messages)

    try:
        # Send welcome message on new session (no history)
        if welcome_message and not history_messages:
            await websocket.send_json({"type": "done", "role": "assistant", "content": welcome_message})

        # Session idle detection: two-phase timeout
        # Phase 1: After IDLE seconds of no input → SESSION_IDLE hook (T0 log + session summary)
        # Phase 2: After WS_IDLE_TIMEOUT seconds total → SESSION_CLOSE + disconnect
        import asyncio as _aio_idle
        _DREAM_IDLE_SECONDS = int(os.environ.get("WS_IDLE_DREAM_SECONDS", "180"))
        _idle_timeout = int(os.environ.get("WS_IDLE_TIMEOUT_SECONDS", "600"))
        _idle_dreamed = False

        while True:
            logger.info(f"[WS] Waiting for message from {agent_name}...")
            # Pick timeout: dream threshold first, then full idle close
            _wait_timeout = _DREAM_IDLE_SECONDS if (not _idle_dreamed and _DREAM_IDLE_SECONDS > 0 and len(conversation) > 1) else _idle_timeout
            try:
                data = await _aio_idle.wait_for(websocket.receive_json(), timeout=_wait_timeout)
            except _aio_idle.TimeoutError:
                if not _idle_dreamed and _DREAM_IDLE_SECONDS > 0 and len(conversation) > 1:
                    # Phase 1: SESSION_IDLE — extract memories while user is away
                    _idle_dreamed = True
                    logger.info("[WS] SESSION_IDLE triggered for {} (session {})", agent_name, conv_id)
                    try:
                        from app.runtime.hooks import HookEvent, emit_hook

                        await _aio_idle.wait_for(
                            emit_hook(
                                HookEvent.SESSION_IDLE,
                                agent_id=agent_id,
                                session_id=conv_id,
                                messages=conversation,
                                source="websocket",
                                metadata={
                                    "idle_seconds": _DREAM_IDLE_SECONDS,
                                    "tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
                                    "agent_name": agent.name or "Agent",
                                },
                            ),
                            timeout=15.0,
                        )
                        # Session summary for Episodic layer (DB) — kept for retriever._retrieve_episodic()
                        if agent.tenant_id and conv_id:
                            try:
                                from app.services.memory_service import _generate_session_summary, _save_session_summary

                                _idle_summary = await _aio_idle.wait_for(
                                    _generate_session_summary(conversation, agent.tenant_id),
                                    timeout=10.0,
                                )
                                if _idle_summary:
                                    await _save_session_summary(conv_id, _idle_summary)
                            except Exception as _sum_err:
                                logger.debug("[WS] Session summary on idle failed (non-fatal): {}", _sum_err)
                        logger.info("[WS] SESSION_IDLE completed for {}", agent_name)
                    except Exception as _dream_err:
                        logger.debug("[WS] SESSION_IDLE failed (non-fatal): {}", _dream_err)
                    continue  # Back to waiting for user messages
                else:
                    # Phase 2: SESSION_CLOSE — idle timeout, close connection
                    logger.info(f"[WS] SESSION_CLOSE (idle timeout {_idle_timeout}s) for {agent_name}")
                    try:
                        from app.runtime.hooks import HookEvent, emit_hook

                        await emit_hook(
                            HookEvent.SESSION_CLOSE,
                            agent_id=agent_id,
                            session_id=conv_id,
                            messages=conversation,
                            source="websocket",
                            metadata={"reason": "idle_timeout"},
                        )
                    except Exception as _close_err:
                        logger.debug("[WS] SESSION_CLOSE hook failed (non-fatal): {}", _close_err)
                    await websocket.send_json({"type": "info", "content": "Connection closed due to inactivity. Reconnect to continue."})
                    await websocket.close(code=1000)
                    return
            # User sent a message — reset dream flag
            _idle_dreamed = False
            content = data.get("content", "")
            display_content = data.get("display_content", "")  # User-facing display text
            file_name = data.get("file_name", "")  # Original file name for attachment display
            if data.get("type") == "abort":
                try:
                    run_id = data.get("run_id")
                    async with async_session() as run_db:
                        active_run = None
                        if not run_id:
                            active_run = await get_active_web_chat_run(
                                db=run_db,
                                agent_id=agent_id,
                                session_id=conv_id,
                            )
                            run_id = active_run.get("run_id") if active_run else None
                        if run_id:
                            payload = await cancel_web_chat_run(
                                db=run_db,
                                agent_id=agent_id,
                                session_id=conv_id,
                                run_id=run_id,
                                user_id=user_id,
                            )
                            await websocket.send_json({"type": "run_cancelled", **payload})
                except Exception as abort_err:
                    logger.warning("[WS] Failed to cancel web chat run: {}", abort_err)
                    await websocket.send_json({"type": "error", "content": "Failed to stop run"})
                continue
            logger.info(f"[WS] Received: {content[:50]}")

            if not content:
                continue

            # Durable web chat run: execution now lives outside the WebSocket
            # lifecycle. This keeps closing/reloading the page from cancelling
            # the underlying agent run; the socket only subscribes to events.
            try:
                async with async_session() as run_db:
                    session_result = await run_db.execute(
                        select(ChatSession).where(
                            ChatSession.id == uuid.UUID(str(conv_id)),
                            ChatSession.agent_id == agent_id,
                            ChatSession.user_id == user_id,
                        )
                    )
                    run_session = session_result.scalar_one_or_none()
                    if not run_session:
                        await websocket.send_json({"type": "error", "content": "Session not found"})
                        continue
                    payload = await start_web_chat_run(
                        db=run_db,
                        agent=agent,
                        user=user,
                        session=run_session,
                        content=content,
                        display_content=display_content,
                        file_name=file_name,
                    )
                await websocket.send_json({"type": "run_started", **payload})
            except ActiveWebChatRunExists as active_err:
                await websocket.send_json({"type": "run_started", **active_err.run})
            except Exception as run_err:
                logger.error("[WS] Failed to start durable web chat run: {}", run_err)
                await websocket.send_json({"type": "error", "content": "Failed to start run"})
            continue

    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected: {agent_name}")
        await manager.disconnect(agent_id_str, websocket)
    except Exception as e:
        logger.error(f"[WS] Error in message loop: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        await manager.disconnect(agent_id_str, websocket)
        try:
            await websocket.close(code=1011)
        except Exception as e:
            logger.debug(f"Suppressed: {e}")
