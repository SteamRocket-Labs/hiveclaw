"""WebSocket chat endpoint for real-time agent conversations."""

import asyncio
import os
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import select

from app.core.permissions import check_agent_access, is_agent_expired
from app.core.security import decode_access_token
from app.database import tenant_scoped_session
from app.kernel.contracts import ExecutionIdentityRef, TerminalReason
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.user import User
from app.runtime.invoker import AgentInvocationRequest, invoke_agent
from app.runtime.session import SessionContext
from app.services.session_live_input import submit_live_cancel_input, submit_live_human_input
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.web_chat_broker import web_chat_broker
from app.services.web_chat_runtime import (
    conversation_from_history_messages as _conversation_from_history_messages,
    get_active_web_chat_run,
)
from app.services.web_session_contract import apply_web_session_contract

router = APIRouter(tags=["websocket"])


def _process_role() -> str:
    from app.config import get_settings

    return str(get_settings().HIVE_PROCESS_ROLE or "runtime").strip().lower()


async def _emit_ws_session_lifecycle_hook(
    event: Any,
    *,
    agent_id: uuid.UUID | str,
    session_id: str | uuid.UUID | None,
    messages: list[dict[str, Any]],
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if _process_role() == "api":
        from app.services.runtime_control_bus import publish_session_lifecycle_hook

        lifecycle_metadata = dict(metadata or {})
        lifecycle_metadata.setdefault("message_count", len(messages))
        await publish_session_lifecycle_hook(
            event=event,
            agent_id=agent_id,
            session_id=session_id,
            source=source,
            metadata=lifecycle_metadata,
        )
        return

    from app.runtime.hooks import HookEvent, emit_hook

    hook_event = HookEvent(event) if isinstance(event, str) else event
    await emit_hook(
        hook_event,
        evidence_mode="independent",
        agent_id=agent_id,
        session_id=session_id,
        messages=messages,
        source=source,
        metadata=metadata or {},
    )


manager = web_chat_broker

_WS_IDLE_DREAM_DEFAULT_SECONDS = 180
_WS_IDLE_TIMEOUT_DEFAULT_SECONDS = 3600


def _read_non_negative_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("[WS] Invalid {}={!r}; falling back to {}", name, raw, default)
        return default


def _get_ws_idle_dream_seconds() -> int:
    return _read_non_negative_int_env("WS_IDLE_DREAM_SECONDS", _WS_IDLE_DREAM_DEFAULT_SECONDS)


def _get_ws_idle_timeout_seconds() -> int:
    return _read_non_negative_int_env("WS_IDLE_TIMEOUT_SECONDS", _WS_IDLE_TIMEOUT_DEFAULT_SECONDS)


async def _handle_websocket_control_message(websocket: WebSocket, data: dict) -> bool:
    message_type = data.get("type")
    if message_type == "ping":
        await websocket.send_json({"type": "pong"})
        return True
    if message_type == "pong":
        return True
    return False


async def _has_active_web_chat_run(agent_id: uuid.UUID, session_id: str | uuid.UUID | None) -> bool:
    if not session_id:
        return False
    try:
        # WS path has no middleware GUC — scope by the agent's tenant.
        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as run_db:
            active_run = await get_active_web_chat_run(
                db=run_db,
                agent_id=agent_id,
                session_id=session_id,
            )
            return bool(active_run)
    except Exception as exc:
        logger.debug("[WS] Active web chat run check failed during idle handling: {}", exc)
        return False


async def call_llm(
    model: LLMModel,
    messages: list[dict],
    agent_name: str,
    role_description: str,
    fallback_model: LLMModel | None = None,
    tenant_id: uuid.UUID | None = None,
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
    if not isinstance(effective_session_context.metadata, dict):
        effective_session_context.metadata = {}
    turn_seed = effective_session_context.metadata.get("runtime_task_id") or session_id or uuid.uuid4().hex
    effective_session_context.metadata.setdefault("turn_id", f"turn-{turn_seed}")
    effective_session_context.metadata.setdefault(
        "intent_id",
        f"intent-{effective_session_context.metadata.get('request_id') or turn_seed}",
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            fallback_model=fallback_model,
            messages=runtime_messages,
            agent_name=agent_name,
            role_description=role_description,
            tenant_id=tenant_id,
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
            emit_turn_stop=False,
        )
    )

    if auto_close_session and agent_id is not None:
        close_messages = list(runtime_memory_messages or runtime_messages)
        terminal_reason = getattr(result, "terminal_reason", TerminalReason.TURN_STOP)
        try:
            typed_terminal_reason = (
                terminal_reason if isinstance(terminal_reason, TerminalReason) else TerminalReason(str(terminal_reason))
            )
        except ValueError:
            # An untyped/unknown terminal receipt cannot be inferred from the
            # model-authored bytes. Treat the lifecycle authority as failed.
            typed_terminal_reason = TerminalReason.TURN_ABORT
        turn_aborted = typed_terminal_reason not in {
            TerminalReason.TURN_STOP,
            TerminalReason.CLARIFICATION_REQUIRED,
        }
        if not turn_aborted:
            close_messages.append({"role": "assistant", "content": result.content})
        try:
            from app.runtime.hooks import HookEvent, emit_hook

            await emit_hook(
                HookEvent.TURN_ABORT if turn_aborted else HookEvent.TURN_STOP,
                evidence_mode="independent",
                agent_id=agent_id,
                session_id=effective_session_context.session_id or session_id,
                source=effective_session_context.source,
                messages=close_messages,
                metadata={
                    "tenant_id": effective_session_context.metadata.get("tenant_id"),
                    "reason": "invoke_failed" if turn_aborted else "invoke_complete",
                    "channel": effective_session_context.channel,
                    "checkpoint_kind": "turn_abort" if turn_aborted else "user_turn_stop",
                    "semantic_memory_eligible": not turn_aborted,
                    "terminal_reason": typed_terminal_reason.value,
                    "turn_id": effective_session_context.metadata.get("turn_id"),
                    "intent_id": effective_session_context.metadata.get("intent_id"),
                    "runtime_task_id": effective_session_context.metadata.get("runtime_task_id"),
                    "request_id": effective_session_context.metadata.get("request_id"),
                    "trace_id": effective_session_context.metadata.get("trace_id"),
                },
            )
        except Exception as close_err:
            logger.debug("[call_llm] TURN_STOP hook failed (non-fatal): {}", close_err)

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
    1. Decode JWT, accept the transport, then require ``session.subscribe``.
    2. Resolve tenant/principal/Agent/Session authority without loading a model.
    3. Register a live buffer, capture the DB watermark and send ``session.ready``.
    4. Stream complete catch-up pages, drain the live buffer, then remain live.
    5. New user/control input enters the durable Session V2 command plane.
    """
    from app.services.session_subscription import (
        SESSION_SUBSCRIPTION_CLOSE_CODES,
        SessionSubscriptionError,
        build_session_ready,
        iter_session_catchup_events,
        load_session_catchup_window,
        parse_session_subscribe,
        resolve_subscription_cursor,
        session_subscription_error_frame,
    )

    async def close_subscription(error: SessionSubscriptionError) -> None:
        await websocket.send_json(session_subscription_error_frame(error))
        await websocket.close(code=SESSION_SUBSCRIPTION_CLOSE_CODES[error.code], reason=error.code)

    # Decode before accept when possible; once accepted, every failure is typed.
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception:
        await websocket.accept()
        await close_subscription(SessionSubscriptionError("auth_failed"))
        return

    await websocket.accept()
    subscription_registered = False
    try:
        subscribe_payload = await websocket.receive_json()
        expected_session_id = session_id or (
            subscribe_payload.get("session_id") if isinstance(subscribe_payload, dict) else None
        )
        subscription = parse_session_subscribe(
            subscribe_payload,
            expected_session_id=expected_session_id,
        )
        from app.core.execution_context import set_delegated_user_identity
        from app.models.chat_session import ChatSession

        ws_tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(ws_tenant_id) as db:
            user = await db.scalar(select(User).where(User.id == user_id))
            if user is None:
                raise SessionSubscriptionError("auth_failed")
            set_delegated_user_identity(user.id, user.display_name or user.username, channel="web")
            agent, _access_level = await check_agent_access(db, user, agent_id)
            if is_agent_expired(agent):
                raise SessionSubscriptionError("session_forbidden")
            active_session = await db.scalar(
                select(ChatSession).where(
                    ChatSession.id == subscription.session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
            if active_session is None:
                raise SessionSubscriptionError("session_not_found")
            if active_session.user_id != user.id:
                raise SessionSubscriptionError("session_forbidden")
            await apply_web_session_contract(db, session=active_session, agent_id=agent_id, user=user)
            await db.commit()

            await manager.begin_session_subscription(str(agent_id), websocket, str(active_session.id))
            subscription_registered = True
            catchup = await load_session_catchup_window(
                db,
                session_id=active_session.id,
                after_sequence=subscription.after_sequence,
            )
            accepted_after_sequence = resolve_subscription_cursor(
                subscription,
                last_committed_sequence=catchup.last_committed_sequence,
            )
            active_run = await get_active_web_chat_run(
                db=db,
                agent_id=agent_id,
                session_id=active_session.id,
            )
            ready = build_session_ready(
                session_id=active_session.id,
                connection_attempt_id=subscription.connection_attempt_id,
                accepted_after_sequence=accepted_after_sequence,
                last_committed_sequence=catchup.last_committed_sequence,
                active_run=active_run,
                sequence_projection=catchup.cursor.mode,
            )
            await websocket.send_json(ready)
            if subscription.cursor_mode == "resume":
                async for event in iter_session_catchup_events(
                    db,
                    session_id=active_session.id,
                    after_sequence=subscription.after_sequence,
                    through_sequence=catchup.last_committed_sequence,
                    cursor=catchup.cursor,
                    audience="user",
                ):
                    await websocket.send_json(event)
            await manager.activate_session_subscription(
                websocket,
                delivered_through_sequence=catchup.last_committed_storage_sequence,
                delivered_through_delivery_sequence=catchup.last_committed_sequence,
            )

            history_result = await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(active_session.id),
                )
                .order_by(ChatMessage.created_at.asc())
            )
            history_messages = list(history_result.scalars().all())
    except WebSocketDisconnect:
        if subscription_registered:
            await manager.disconnect(str(agent_id), websocket)
        return
    except SessionSubscriptionError as error:
        if subscription_registered:
            await manager.disconnect(str(agent_id), websocket)
        await close_subscription(error)
        return
    except Exception as error:
        logger.exception("[WS] Session subscription bootstrap failed: {}", error)
        if subscription_registered:
            await manager.disconnect(str(agent_id), websocket)
        await close_subscription(SessionSubscriptionError("event_store_retryable", retryable=True))
        return

    conv_id = str(active_session.id)
    _active_session = active_session
    agent_name = agent.name
    agent_id_str = str(agent_id)
    conversation = _conversation_from_history_messages(history_messages)
    logger.info("[WS] Session ready agent={} session={}", agent_name, conv_id)

    async def send_control_error(code: str, *, retryable: bool) -> None:
        await websocket.send_json(
            {
                "type": "session.error",
                "error": {
                    "code": code,
                    "retryable": retryable,
                    "message_key": f"session.{code}",
                },
            }
        )

    try:
        # Session idle detection: two-phase timeout
        # Phase 1: After IDLE seconds of no input → SESSION_IDLE hook (T0 segment boundary)
        # Phase 2: After WS_IDLE_TIMEOUT seconds total → SESSION_CLOSE + disconnect
        import asyncio as _aio_idle

        _DREAM_IDLE_SECONDS = _get_ws_idle_dream_seconds()
        _idle_timeout = _get_ws_idle_timeout_seconds()
        _idle_dreamed = False

        while True:
            logger.info(f"[WS] Waiting for message from {agent_name}...")
            # Pick timeout: dream threshold first, then full idle close
            _wait_timeout = (
                _DREAM_IDLE_SECONDS
                if (not _idle_dreamed and _DREAM_IDLE_SECONDS > 0 and len(conversation) > 1)
                else _idle_timeout
            )
            try:
                data = await _aio_idle.wait_for(websocket.receive_json(), timeout=_wait_timeout)
            except _aio_idle.TimeoutError:
                if await _has_active_web_chat_run(agent_id, conv_id):
                    await websocket.send_json({"type": "pong"})
                    continue
                if not _idle_dreamed and _DREAM_IDLE_SECONDS > 0 and len(conversation) > 1:
                    # Phase 1: SESSION_IDLE — extract memories while user is away
                    _idle_dreamed = True
                    logger.info("[WS] SESSION_IDLE triggered for {} (session {})", agent_name, conv_id)
                    try:
                        from app.runtime.hooks import HookEvent

                        await _aio_idle.wait_for(
                            _emit_ws_session_lifecycle_hook(
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
                        # User-facing session-list/search summary projection.
                        # Canonical episodic prompt memory reads T2 packages.
                        if _process_role() != "api" and agent.tenant_id and conv_id:
                            try:
                                from app.services.memory_service import _generate_session_summary, _save_session_summary

                                _idle_summary = await _aio_idle.wait_for(
                                    _generate_session_summary(
                                        conversation,
                                        agent.tenant_id,
                                        agent_id=agent_id,
                                        user_id=user_id,
                                    ),
                                    timeout=10.0,
                                )
                                if _idle_summary:
                                    await _save_session_summary(conv_id, _idle_summary, agent.tenant_id)
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
                        from app.runtime.hooks import HookEvent

                        await _emit_ws_session_lifecycle_hook(
                            HookEvent.SESSION_CLOSE,
                            agent_id=agent_id,
                            session_id=conv_id,
                            messages=conversation,
                            source="websocket",
                            metadata={"reason": "idle_timeout"},
                        )
                    except Exception as _close_err:
                        logger.debug("[WS] SESSION_CLOSE hook failed (non-fatal): {}", _close_err)
                    await websocket.send_json(
                        {"type": "info", "content": "Connection closed due to inactivity. Reconnect to continue."}
                    )
                    await websocket.close(code=1000)
                    return
            if await _handle_websocket_control_message(websocket, data):
                continue
            # User sent a message — reset dream flag
            _idle_dreamed = False
            content = data.get("content", "")
            display_content = data.get("display_content", "")  # User-facing display text
            file_name = data.get("file_name", "")  # Original file name for attachment display
            if data.get("type") == "abort":
                try:
                    run_id = data.get("run_id")
                    async with tenant_scoped_session(agent.tenant_id) as run_db:
                        active_run = None
                        if not run_id:
                            active_run = await get_active_web_chat_run(
                                db=run_db,
                                agent_id=agent_id,
                                session_id=conv_id,
                            )
                            run_id = active_run.get("run_id") if active_run else None
                        if run_id:
                            payload = await submit_live_cancel_input(
                                db=run_db,
                                agent=agent,
                                user=user,
                                session=_active_session,
                                run_id=run_id,
                                source="websocket_abort",
                                idempotency_key=data.get("idempotency_key"),
                                control_id=data.get("control_id"),
                            )
                            await websocket.send_json(
                                {
                                    "type": "session.control_receipt",
                                    "kind": "cancel",
                                    "receipt": payload,
                                }
                            )
                except Exception as abort_err:
                    logger.warning("[WS] Failed to cancel web chat run: {}", abort_err)
                    await send_control_error("control_input_retryable", retryable=True)
                continue
            logger.info(f"[WS] Received: {content[:50]}")

            if not content:
                continue

            # Durable web chat run: execution now lives outside the WebSocket
            # lifecycle. This keeps closing/reloading the page from cancelling
            # the underlying agent run; the socket only subscribes to events.
            try:
                async with tenant_scoped_session(agent.tenant_id) as run_db:
                    session_result = await run_db.execute(
                        select(ChatSession).where(
                            ChatSession.id == uuid.UUID(str(conv_id)),
                            ChatSession.agent_id == agent_id,
                            ChatSession.user_id == user_id,
                        )
                    )
                    run_session = session_result.scalar_one_or_none()
                    if not run_session:
                        await send_control_error("session_not_found", retryable=False)
                        continue
                    receipt = await submit_live_human_input(
                        db=run_db,
                        agent=agent,
                        user=user,
                        session=run_session,
                        content=content,
                        source="websocket_message",
                        input_id=data.get("input_id"),
                        idempotency_key=data.get("idempotency_key"),
                        display_content=display_content,
                        file_name=file_name,
                        attachments=data.get("attachments") if isinstance(data.get("attachments"), list) else None,
                        parts=data.get("parts") if isinstance(data.get("parts"), list) else None,
                    )
                await websocket.send_json(
                    {
                        "type": "session.control_receipt",
                        "kind": "human_input",
                        "receipt": receipt,
                    }
                )
            except Exception as run_err:
                logger.error("[WS] Failed to start durable web chat run: {}", run_err)
                await send_control_error("input_dispatch_retryable", retryable=True)
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
