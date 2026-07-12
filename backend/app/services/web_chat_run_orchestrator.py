"""Owns the durable web-chat RuntimeTask execution state machine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.web_chat_runtime import (
        RunPhaseEmitter,
        RuntimePhase,
        _WebChatStreamMicroBatcher,
        asyncio,
        uuid,
    )


async def run_web_chat_task(
    run_id: str | uuid.UUID, *, cancel_event: asyncio.Event | None = None, support: Any
) -> None:
    # Bind an explicit per-call dependency snapshot so tests, DI, and runtime
    # overrides observe the same facade values without copying a module namespace.
    AgentInvocationRequest = support.AgentInvocationRequest
    ExecutionIdentityRef = support.ExecutionIdentityRef
    IntegrityError = support.IntegrityError
    RunPhaseEmitter = support.RunPhaseEmitter
    RuntimePhase = support.RuntimePhase
    SESSION_NATIVE_EVENT_TYPES = support.SESSION_NATIVE_EVENT_TYPES
    STREAM_RETRY_TOMBSTONE = support.STREAM_RETRY_TOMBSTONE
    TerminalReason = support.TerminalReason
    _CANCEL_EVENTS = support._CANCEL_EVENTS
    _CURRENT_BROADCAST_RUN_ID = support._CURRENT_BROADCAST_RUN_ID
    _TerminalToolCardSignal = support._TerminalToolCardSignal
    _USER_VISIBLE_WEB_CHAT_ERROR = support._USER_VISIBLE_WEB_CHAT_ERROR
    _WebChatStreamMicroBatcher = support._WebChatStreamMicroBatcher
    _active_channel_delivery_target_for_turn = support._active_channel_delivery_target_for_turn
    _channel_delivery_prompt_suffix_for_turn = support._channel_delivery_prompt_suffix_for_turn
    _channel_session_permission_prompt_for_tool_call = support._channel_session_permission_prompt_for_tool_call
    _claim_pending_mid_run_user_messages = support._claim_pending_mid_run_user_messages
    _claim_pending_reply_suffix_for_session = support._claim_pending_reply_suffix_for_session
    _clear_interactive_plan_mode = support._clear_interactive_plan_mode
    _clear_stale_plan_mode_for_new_turn = support._clear_stale_plan_mode_for_new_turn
    _declared_terminal_artifact_paths = support._declared_terminal_artifact_paths
    _emit_terminal_turn_hook = support._emit_terminal_turn_hook
    _finalize_web_chat_run_with_assistant = support._finalize_web_chat_run_with_assistant
    _finalize_web_chat_run_without_assistant = support._finalize_web_chat_run_without_assistant
    _interactive_pause_summary_for_tool_call = support._interactive_pause_summary_for_tool_call
    _is_final_assistant_marker_unique_violation = support._is_final_assistant_marker_unique_violation
    _is_web_origin_turn = support._is_web_origin_turn
    _load_runtime_context = support._load_runtime_context
    _maybe_handle_plan_mode_entry = support._maybe_handle_plan_mode_entry
    _merge_runtime_permission_metadata = support._merge_runtime_permission_metadata
    _persist_runtime_event = support._persist_runtime_event
    _persist_stream_step_event = support._persist_stream_step_event
    _persist_tool_call = support._persist_tool_call
    _phase_for_interactive_pause = support._phase_for_interactive_pause
    _phase_for_terminal_status = support._phase_for_terminal_status
    _plan_mode_unsubmitted_terminal_error = support._plan_mode_unsubmitted_terminal_error
    _rejected_terminal_artifact_paths_for_turn = support._rejected_terminal_artifact_paths_for_turn
    _resume_queued_plan_handoffs = support._resume_queued_plan_handoffs
    _run_id = support._run_id
    _runtime_action_event_from_tool_result = support._runtime_action_event_from_tool_result
    _runtime_actor_authority_bound = support._runtime_actor_authority_bound
    _runtime_actor_external_principal_id = support._runtime_actor_external_principal_id
    _runtime_actor_user_id = support._runtime_actor_user_id
    _runtime_prompt_metadata_update = support._runtime_prompt_metadata_update
    _runtime_turn_excluded_tool_names = support._runtime_turn_excluded_tool_names
    _should_persist_runtime_event = support._should_persist_runtime_event
    _simulation_title = support._simulation_title
    _sync_runtime_session_permission_metadata = support._sync_runtime_session_permission_metadata
    _terminal_artifact_paths_for_turn = support._terminal_artifact_paths_for_turn
    _terminal_artifact_prompt_suffix_for_turn = support._terminal_artifact_prompt_suffix_for_turn
    _terminal_file_change_lineage_for_turn = support._terminal_file_change_lineage_for_turn
    _terminal_file_change_paths_for_turn = support._terminal_file_change_paths_for_turn
    _terminal_file_change_states_for_turn = support._terminal_file_change_states_for_turn
    _terminal_reason_value_for_web_run = support._terminal_reason_value_for_web_run
    _tool_step_contract = support._tool_step_contract
    _update_runtime_task = support._update_runtime_task
    broadcast_web_chat_event = support.broadcast_web_chat_event
    build_chunk_event = support.build_chunk_event
    build_done_event = support.build_done_event
    build_session_native_event = support.build_session_native_event
    build_thinking_event = support.build_thinking_event
    build_tool_call_event = support.build_tool_call_event
    conversation_from_history_messages = support.conversation_from_history_messages
    invoke_agent = support.invoke_agent
    is_llm_error_message = support.is_llm_error_message
    json = support.json
    logger = support.logger
    plan_mode_core = support.plan_mode_core
    tenant_scoped_session = support.tenant_scoped_session
    web_chat_broker = support.web_chat_broker
    asyncio = support.asyncio

    run_uuid = _run_id(run_id)
    run_key = run_uuid.hex
    broadcast_run_token = _CURRENT_BROADCAST_RUN_ID.set(run_key)
    cancel_event = cancel_event or _CANCEL_EVENTS.setdefault(run_key, asyncio.Event())
    streamed_chunks: list[str] = []
    thinking_content: list[str] = []
    stream_batcher: _WebChatStreamMicroBatcher | None = None
    terminal_agent_id: uuid.UUID | None = None
    terminal_session_id: str | None = None
    terminal_runtime_metadata: dict[str, Any] | None = None
    phase_emitter: RunPhaseEmitter | None = None
    terminal_phase_hint: RuntimePhase | None = None
    runtime_session_context: Any | None = None
    actor_user_id: uuid.UUID | None = None
    actor_external_principal_id: uuid.UUID | None = None

    try:
        loaded_context = await _load_runtime_context(run_uuid)
        if len(loaded_context) == 6:
            runtime_task, agent, user, llm_model, fallback_model, history_messages = loaded_context
            session = None
        else:
            runtime_task, agent, user, llm_model, fallback_model, history_messages, session = loaded_context
        session_id = str(runtime_task.parent_session_id)
        terminal_agent_id = agent.id
        terminal_session_id = session_id
        phase_emitter = RunPhaseEmitter(
            lambda event: broadcast_web_chat_event(agent.id, session_id, event),
            run_id=run_key,
        )
        # §2: the budget finalization turn holds the `summarizing` phase for its
        # whole duration instead of cycling thinking/responding/tool phases.
        summary_turn_mode = bool(
            (runtime_task.metadata_json or {}).get("budget_summary_turn")
            if isinstance(runtime_task.metadata_json, dict)
            else False
        )
        reclaimed_claim = bool(
            (runtime_task.metadata_json or {}).get("reclaimed_expired_claim")
            if isinstance(runtime_task.metadata_json, dict)
            else False
        )
        initial_phase = (
            RuntimePhase.SUMMARIZING
            if summary_turn_mode
            else (RuntimePhase.RESUMING if reclaimed_claim else RuntimePhase.STARTING)
        )
        await phase_emitter.transition(initial_phase)
        conversation = conversation_from_history_messages(history_messages)
        prompt = runtime_task.prompt or ""
        metadata = _merge_runtime_permission_metadata(
            runtime_metadata=runtime_task.metadata_json if isinstance(runtime_task.metadata_json, dict) else {},
            session_metadata=getattr(session, "transcript_metadata_json", None) if session is not None else None,
        )
        actor_user_id = _runtime_actor_user_id(user)
        actor_external_principal_id = _runtime_actor_external_principal_id(user)
        actor_authority_bound = _runtime_actor_authority_bound(user)
        if actor_external_principal_id is not None and not actor_authority_bound:
            metadata["disable_tools"] = True
            metadata["tool_policy"] = "disabled_for_unbound_external_principal"
        terminal_runtime_metadata = metadata
        runtime_mailbox_role = str(metadata.get("runtime_mailbox_role") or "").strip().lower()
        internal_runtime_context_turn = bool(metadata.get("task_notification")) or runtime_mailbox_role == "system"
        if metadata.get("latest_user_prompt_overrides_history") and prompt and not internal_runtime_context_turn:
            for idx in range(len(conversation) - 1, -1, -1):
                if conversation[idx].get("role") == "user":
                    conversation[idx]["content"] = prompt
                    break
            else:
                conversation.append({"role": "user", "content": prompt})

        runtime_session_context = await web_chat_broker.get_or_create_runtime_session(str(agent.id), session_id)
        if hasattr(runtime_session_context, "begin_turn"):
            runtime_session_context.begin_turn()
        runtime_session_context.source = str(metadata.get("source") or runtime_session_context.source or "web")
        runtime_session_context.channel = str(metadata.get("channel") or runtime_session_context.channel or "web")
        runtime_session_context.metadata["tenant_id"] = str(agent.tenant_id) if agent.tenant_id else None
        runtime_session_context.metadata["runtime_task_id"] = run_uuid.hex
        if getattr(runtime_task, "budget_run_id", None) or metadata.get("budget_run_id"):
            runtime_session_context.metadata["budget_run_id"] = str(
                getattr(runtime_task, "budget_run_id", None) or metadata.get("budget_run_id")
            )
        if summary_turn_mode:
            # §2: mark the invocation so the budgeted LLM client reserves its
            # provider calls through the finalization lane.
            runtime_session_context.metadata["budget_summary_turn"] = True
        else:
            runtime_session_context.metadata.pop("budget_summary_turn", None)
        runtime_session_context.metadata["request_id"] = str(run_uuid)
        runtime_session_context.metadata["turn_id"] = str(metadata.get("turn_id") or f"turn-{run_uuid.hex}")
        runtime_session_context.metadata["intent_id"] = str(
            metadata.get("intent_id") or metadata.get("request_id") or f"intent-{run_uuid.hex}"
        )
        runtime_session_context.metadata["trace_id"] = (
            getattr(runtime_task, "trace_id", None)
            or metadata.get("trace_id")
            or f"{runtime_session_context.source or 'web'}-chat:{run_uuid.hex}"
        )
        if metadata.get("parent_trace_id"):
            runtime_session_context.metadata["parent_trace_id"] = metadata.get("parent_trace_id")
        if metadata.get("side_session"):
            runtime_session_context.metadata["side_session"] = True
            runtime_session_context.metadata["side_session_kind"] = metadata.get("side_session_kind") or "btw"
        if metadata.get("tool_policy"):
            runtime_session_context.metadata["tool_policy"] = metadata.get("tool_policy")
        _sync_runtime_session_permission_metadata(runtime_session_context, metadata)
        channel_delivery_suffix = _channel_delivery_prompt_suffix_for_turn(metadata, runtime_session_context)

        _clear_stale_plan_mode_for_new_turn(
            runtime_session_context,
            plan_mode_requested=bool(metadata.get("plan_mode_requested")),
            history_messages=history_messages,
        )

        plan_mode_response = None
        if not internal_runtime_context_turn:
            plan_mode_response = await _maybe_handle_plan_mode_entry(
                agent_id=agent.id,
                user_id=getattr(user, "id", None),
                session_id=session_id,
                content=prompt,
                classification_content=str(metadata.get("display_content") or prompt),
                plan_mode_requested=bool(metadata.get("plan_mode_requested")),
                runtime_session_context=runtime_session_context,
            )
        if plan_mode_response is not None:
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=actor_user_id,
                session_id=session_id,
                content=plan_mode_response,
                thinking=None,
                status="completed",
                result_summary=plan_mode_response[:500],
            )
            if finalized:
                await _emit_terminal_turn_hook(
                    agent_id=agent.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=metadata,
                    status="completed",
                    reason="invoke_complete",
                    source=runtime_session_context.source,
                )
                await broadcast_web_chat_event(agent.id, session_id, build_done_event(plan_mode_response))
            return

        if not llm_model:
            terminal_phase_hint = RuntimePhase.FAILED
            assistant_response = f"[LLM Error] {agent.name} has no LLM model configured. Please select a model in the agent's Settings tab."
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=actor_user_id,
                session_id=session_id,
                content=assistant_response,
                thinking=None,
                status="failed",
                result_summary=assistant_response[:500],
            )
            if finalized:
                await _emit_terminal_turn_hook(
                    agent_id=agent.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=metadata,
                    status="failed",
                    reason="llm_model_missing",
                    source=runtime_session_context.source,
                )
                await broadcast_web_chat_event(agent.id, session_id, build_done_event(assistant_response))
            return

        async def send_stream_event(kind: str, text: str, *, reset: bool = False) -> None:
            if kind == "thinking":
                event = build_thinking_event(text)
            else:
                event = build_chunk_event(text, reset=reset)
            if text and not reset:
                persisted_event = await _persist_stream_step_event(
                    agent_id=agent.id,
                    tenant_id=agent.tenant_id,
                    user_id=actor_user_id,
                    external_principal_id=actor_external_principal_id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    event_type=kind,
                    content=text,
                    part=event.get("part") if isinstance(event.get("part"), dict) else None,
                )
                if persisted_event:
                    event_parts = persisted_event.transcript_event.parts_json or []
                    event.update(
                        {
                            "transcript_event_id": str(persisted_event.event_id),
                            "sequence": persisted_event.sequence,
                            "event_type": kind,
                            "role": "assistant",
                            "content": persisted_event.transcript_event.content or text,
                            "message_id": str(persisted_event.message_id) if persisted_event.message_id else None,
                            "parts": event_parts or None,
                            "metadata": persisted_event.transcript_event.metadata_json or {},
                        }
                    )
            await broadcast_web_chat_event(agent.id, session_id, event)

        stream_batcher = _WebChatStreamMicroBatcher(send_stream_event)

        async def stream_to_ws(text: str) -> None:
            assert stream_batcher is not None
            if text == STREAM_RETRY_TOMBSTONE:
                streamed_chunks.clear()
                await stream_batcher.reset_chunk()
                return
            if phase_emitter is not None and not summary_turn_mode:
                await phase_emitter.transition(RuntimePhase.RESPONDING)
            streamed_chunks.append(text)
            await stream_batcher.emit_chunk(text)

        async def thinking_to_ws(text: str) -> None:
            assert stream_batcher is not None
            if phase_emitter is not None and not summary_turn_mode:
                await phase_emitter.transition(RuntimePhase.THINKING)
            thinking_content.append(text)
            await stream_batcher.emit_thinking(text)

        async def runtime_event_to_ws(data: dict[str, Any]) -> None:
            assert stream_batcher is not None
            if data.get("type") == "stream_retry_tombstone":
                streamed_chunks.clear()
                await stream_batcher.reset_chunk()
                return
            event_type = str(data.get("type") or data.get("event_type") or "")
            if phase_emitter is not None and not summary_turn_mode:
                if event_type == "compaction_started":
                    await phase_emitter.transition(RuntimePhase.COMPACTING)
                elif event_type in {"compaction_completed", "compaction_skipped"}:
                    await phase_emitter.transition(RuntimePhase.THINKING)
                elif event_type == "permission" and str(data.get("status") or "") == "session_permission_required":
                    await phase_emitter.transition(RuntimePhase.AWAITING_APPROVAL)
            if event_type in SESSION_NATIVE_EVENT_TYPES:
                event_payload = build_session_native_event(data)
            else:
                event_payload = data
            await stream_batcher.flush()
            await broadcast_web_chat_event(agent.id, session_id, event_payload)
            if _should_persist_runtime_event(data):
                await _persist_runtime_event(
                    agent_id=agent.id,
                    user_id=actor_user_id,
                    external_principal_id=actor_external_principal_id,
                    session_id=session_id,
                    data=data,
                )

        pending_reply_suffix = ""
        try:
            async with tenant_scoped_session(agent.tenant_id) as pending_db:
                pending_reply_suffix = await _claim_pending_reply_suffix_for_session(
                    pending_db,
                    agent_id=agent.id,
                    session_id=session_id,
                )
        except Exception as exc:
            logger.warning("[WebChatRun] Pending reply injection failed (non-fatal): {}", exc)

        if internal_runtime_context_turn and prompt:
            runtime_suffix = f"Runtime continuation context (system generated, not a user message):\n{prompt}"
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, runtime_suffix) if part)

        restart_resume_context = metadata.get("restart_resume_context")
        if isinstance(restart_resume_context, dict):
            resume_prompt = str(restart_resume_context.get("resume_prompt") or "").strip()
            if resume_prompt:
                restart_suffix = (
                    "Restart recovery context: this run was active before the worker restarted. "
                    "Use the following durable resume context to continue from the saved artifacts instead of "
                    f"starting over.\n{resume_prompt}"
                )
                pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, restart_suffix) if part)
        if channel_delivery_suffix:
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, channel_delivery_suffix) if part)
        pending_reply_suffix = "\n\n".join(
            part for part in (pending_reply_suffix, _terminal_artifact_prompt_suffix_for_turn()) if part
        )

        trusted_decline = None
        if not internal_runtime_context_turn:
            trusted_decline = plan_mode_core.trusted_decline_metadata(
                content=str(metadata.get("display_content") or prompt),
                messages=history_messages,
                explicit=bool(metadata.get("plan_mode_requested")),
            )
        if trusted_decline and actor_user_id is None:
            trusted_decline = None
        if trusted_decline and actor_user_id is not None:
            try:
                from app.services.plan_mode_recommendation_service import decline_latest_recommendation_for_user

                async with tenant_scoped_session(agent.tenant_id) as recommendation_db:
                    recommendation = await decline_latest_recommendation_for_user(
                        recommendation_db,
                        agent_id=agent.id,
                        user_id=actor_user_id,
                        session_id=session_id,
                    )
                    if recommendation is None:
                        trusted_decline = None
                    else:
                        trusted_decline["recommendation_id"] = str(recommendation.id)
                        await recommendation_db.commit()
            except Exception as exc:
                logger.warning("[WebChatRun] Plan recommendation decline binding failed (non-fatal): {}", exc)
                trusted_decline = None
        if trusted_decline:
            plan_decline_suffix = (
                "Plan Mode governance: the runtime verified that the user declined the immediately preceding "
                "Plan Mode recommendation. If you create or update a scheduled/monitoring trigger as a direct "
                "follow-up, call the trigger tool normally. Do not add opt-out fields to tool arguments, and do "
                "not use this opt-out for long tasks, delegation, or other high-risk actions."
            )
            pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, plan_decline_suffix) if part)

        if trusted_decline:
            runtime_session_context.metadata[plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY] = trusted_decline
        else:
            runtime_session_context.metadata.pop(plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY, None)
        # Plan Mode reminders are injected per-round by the kernel
        # (engine._plan_mode_reminder_content), no longer via system_prompt_suffix —
        # this keeps the frozen prefix cacheable. The metadata mirror below only
        # arms the interactive read-only ContextVar for tool governance.
        active_plan_mode_metadata = runtime_session_context.metadata.get("plan_mode")
        disable_tools_for_turn = bool(
            metadata.get("disable_tools") or metadata.get("tool_policy") == "disabled_by_default"
        )
        excluded_tool_names_for_turn = _runtime_turn_excluded_tool_names(
            metadata,
            runtime_session_context,
            prompt=prompt,
        )

        plan_mode_submitted = False
        interactive_pause_summary: str | None = None
        terminal_tool_card_finalized = False

        async def _finalize_terminal_tool_card_now(
            summary: str,
            *,
            channel_delivery_text: str | None = None,
        ) -> bool:
            """Release the active run as soon as a terminal tool card is visible."""
            nonlocal terminal_tool_card_finalized, terminal_phase_hint
            if terminal_tool_card_finalized:
                return True
            terminal_phase_hint = _phase_for_interactive_pause(summary, cancelled=bool(cancel_event.is_set()))
            metadata_update = {
                "cancelled_by_user": bool(cancel_event.is_set()),
                "interactive_pause": summary,
                "terminal_reason": _terminal_reason_value_for_web_run(
                    status="killed" if cancel_event.is_set() else "completed",
                    cancelled_by_user=bool(cancel_event.is_set()),
                ),
            }
            finalized = await _finalize_web_chat_run_without_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                status="killed" if cancel_event.is_set() else "completed",
                result_summary=summary,
                metadata_json=metadata_update,
                file_change_paths=_terminal_file_change_paths_for_turn(runtime_session_context),
                file_change_states=_terminal_file_change_states_for_turn(runtime_session_context),
                file_change_lineage=_terminal_file_change_lineage_for_turn(runtime_session_context),
                channel_delivery_text=channel_delivery_text,
            )
            terminal_tool_card_finalized = finalized or terminal_tool_card_finalized
            if finalized:
                await _emit_terminal_turn_hook(
                    agent_id=agent.id,
                    session_id=session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=metadata,
                    status="killed" if cancel_event.is_set() else "completed",
                    reason="terminal_tool_card",
                    source=runtime_session_context.source,
                    extra_metadata=metadata_update,
                )
                if stream_batcher is not None:
                    await stream_batcher.flush()
                await broadcast_web_chat_event(agent.id, session_id, build_done_event(""))
            return terminal_tool_card_finalized

        def _tool_result_payload(data: dict[str, Any]) -> dict[str, Any]:
            raw_result = data.get("result")
            if isinstance(raw_result, dict):
                return raw_result
            try:
                payload = json.loads(str(raw_result or "{}"))
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

        def _tool_result_exits_plan_mode(data: dict[str, Any]) -> bool:
            if data.get("name") != "exit_plan_mode" or data.get("status") != "done":
                return False
            return _tool_result_payload(data).get("status") in {"needs_plan", "planning_failed"}

        async def tool_call_to_ws(data: dict[str, Any]) -> None:  # type: ignore[no-redef]
            nonlocal interactive_pause_summary, plan_mode_submitted
            if terminal_tool_card_finalized:
                return
            if phase_emitter is not None and not summary_turn_mode:
                if data.get("status") != "done":
                    await phase_emitter.transition(
                        RuntimePhase.TOOL_RUNNING,
                        detail={"tool_name": str(data.get("name") or "")},
                    )
                else:
                    await phase_emitter.transition(RuntimePhase.THINKING)
            if stream_batcher is not None:
                await stream_batcher.flush()
            data = _tool_step_contract(data, fallback_run_id=run_uuid)
            if data.get("status") != "done":
                persisted_event = await _persist_tool_call(
                    agent_id=agent.id,
                    user_id=actor_user_id,
                    external_principal_id=actor_external_principal_id,
                    session_id=session_id,
                    data=data,
                )
                ws_event = build_tool_call_event(data)
                if persisted_event:
                    event_parts = persisted_event.transcript_event.parts_json or []
                    ws_event.update(
                        {
                            "transcript_event_id": str(persisted_event.event_id),
                            "sequence": persisted_event.sequence,
                            "event_type": "tool_call",
                            "role": "tool_call",
                            "content": persisted_event.transcript_event.content or "",
                            "message_id": str(persisted_event.message_id) if persisted_event.message_id else None,
                            "parts": event_parts or None,
                            "artifacts": [part for part in event_parts if part.get("type") == "artifact"] or None,
                            "metadata": persisted_event.transcript_event.metadata_json or {},
                        }
                    )
                await broadcast_web_chat_event(agent.id, session_id, ws_event)
                return

            persisted_event = await _persist_tool_call(
                agent_id=agent.id,
                user_id=actor_user_id,
                external_principal_id=actor_external_principal_id,
                session_id=session_id,
                data=data,
            )
            ws_event = build_tool_call_event(data)
            if persisted_event:
                event_parts = persisted_event.transcript_event.parts_json or []
                ws_event.update(
                    {
                        "transcript_event_id": str(persisted_event.event_id),
                        "sequence": persisted_event.sequence,
                        "event_type": "tool_result",
                        "role": "tool_call",
                        "content": persisted_event.transcript_event.content or "",
                        "message_id": str(persisted_event.message_id) if persisted_event.message_id else None,
                        "parts": event_parts or None,
                        "artifacts": [part for part in event_parts if part.get("type") == "artifact"] or None,
                        "metadata": persisted_event.transcript_event.metadata_json or {},
                    }
                )
            await broadcast_web_chat_event(agent.id, session_id, ws_event)
            runtime_action_event = _runtime_action_event_from_tool_result(data)
            if runtime_action_event:
                await _persist_runtime_event(
                    agent_id=agent.id,
                    user_id=actor_user_id,
                    external_principal_id=actor_external_principal_id,
                    session_id=session_id,
                    data=runtime_action_event,
                )
                await broadcast_web_chat_event(agent.id, session_id, build_session_native_event(runtime_action_event))
            if data.get("status") == "done":
                if _tool_result_exits_plan_mode(data):
                    plan_mode_submitted = True
                pause_summary = _interactive_pause_summary_for_tool_call(data)
                if pause_summary:
                    interactive_pause_summary = pause_summary
                    channel_prompt = None
                    if pause_summary == "awaiting_session_permission" and not _is_web_origin_turn(
                        metadata, runtime_session_context
                    ):
                        channel_prompt = _channel_session_permission_prompt_for_tool_call(data)
                    await _finalize_terminal_tool_card_now(
                        pause_summary,
                        channel_delivery_text=channel_prompt,
                    )
                    raise _TerminalToolCardSignal(pause_summary)

        plan_mode_token = None
        channel_delivery_token = None
        try:
            if isinstance(active_plan_mode_metadata, dict) and active_plan_mode_metadata.get("active"):
                from app.services.plan_mode_runtime_context import set_interactive_plan_mode

                plan_mode_token = set_interactive_plan_mode(active_plan_mode_metadata)
            active_channel_delivery_target = _active_channel_delivery_target_for_turn(
                metadata=metadata,
                runtime_session_context=runtime_session_context,
                session=session,
                prompt=prompt,
            )
            if active_channel_delivery_target:
                from app.services.channel_delivery_service import channel_delivery_target

                channel_delivery_token = channel_delivery_target.set(active_channel_delivery_target)
            try:
                result = await invoke_agent(
                    AgentInvocationRequest(
                        model=llm_model,
                        fallback_model=fallback_model,
                        messages=conversation,
                        agent_name=agent.name,
                        role_description=agent.role_description or "",
                        agent_id=agent.id,
                        user_id=actor_user_id,
                        execution_identity=ExecutionIdentityRef(
                            identity_type=(
                                "external_principal_bound"
                                if actor_external_principal_id is not None and actor_authority_bound
                                else (
                                    "external_principal"
                                    if actor_external_principal_id is not None
                                    else "delegated_user"
                                )
                            ),
                            identity_id=actor_external_principal_id or actor_user_id,
                            label=f"{user.display_name or user.username} via {runtime_session_context.channel or 'web'}",
                        ),
                        on_chunk=stream_to_ws,
                        on_tool_call=tool_call_to_ws,
                        on_thinking=thinking_to_ws,
                        on_event=runtime_event_to_ws,
                        supports_vision=getattr(llm_model, "supports_vision", False),
                        memory_session_id=session_id,
                        memory_messages=conversation,
                        cancel_event=cancel_event,
                        session_context=runtime_session_context,
                        system_prompt_suffix=pending_reply_suffix,
                        mid_run_message_drain=lambda: _claim_pending_mid_run_user_messages(run_uuid),
                        disable_tools=disable_tools_for_turn,
                        excluded_tool_names=excluded_tool_names_for_turn,
                        emit_turn_stop=False,
                    )
                )
            except _TerminalToolCardSignal as signal:
                interactive_pause_summary = signal.summary
                result = None
        finally:
            if channel_delivery_token is not None:
                from app.services.channel_delivery_service import channel_delivery_target

                channel_delivery_target.reset(channel_delivery_token)
            if plan_mode_token is not None:
                from app.services.plan_mode_runtime_context import reset_interactive_plan_mode

                reset_interactive_plan_mode(plan_mode_token)
            if plan_mode_submitted:
                _clear_interactive_plan_mode(runtime_session_context)
            runtime_session_context.metadata.pop(plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY, None)
        if terminal_tool_card_finalized:
            return
        if stream_batcher is not None:
            await stream_batcher.flush()
        if result is None and interactive_pause_summary:
            terminal_phase_hint = _phase_for_interactive_pause(
                interactive_pause_summary, cancelled=bool(cancel_event.is_set())
            )
            metadata_update = {
                "cancelled_by_user": bool(cancel_event.is_set()),
                "interactive_pause": interactive_pause_summary,
                "terminal_reason": _terminal_reason_value_for_web_run(
                    status="killed" if cancel_event.is_set() else "completed",
                    cancelled_by_user=bool(cancel_event.is_set()),
                ),
            }
            finalized = await _finalize_web_chat_run_without_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                status="killed" if cancel_event.is_set() else "completed",
                result_summary=interactive_pause_summary,
                metadata_json=metadata_update,
                file_change_paths=_terminal_file_change_paths_for_turn(runtime_session_context),
                file_change_states=_terminal_file_change_states_for_turn(runtime_session_context),
                file_change_lineage=_terminal_file_change_lineage_for_turn(runtime_session_context),
            )
            if not finalized:
                return
            await _emit_terminal_turn_hook(
                agent_id=agent.id,
                session_id=session_id,
                run_uuid=run_uuid,
                runtime_metadata=metadata,
                status="killed" if cancel_event.is_set() else "completed",
                reason="interactive_pause",
                source=runtime_session_context.source,
                extra_metadata=metadata_update,
            )
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(""))
            return
        assistant_response = result.content
        plan_mode_terminal_error = (
            None
            if plan_mode_submitted or interactive_pause_summary
            else _plan_mode_unsubmitted_terminal_error(runtime_session_context)
        )
        if plan_mode_terminal_error:
            assistant_response = plan_mode_terminal_error
            _clear_interactive_plan_mode(runtime_session_context)
        thinking = "".join(thinking_content) if thinking_content else None
        status = (
            "killed"
            if cancel_event.is_set()
            else ("failed" if plan_mode_terminal_error or is_llm_error_message(assistant_response) else "completed")
        )
        terminal_phase_hint = _phase_for_terminal_status(status)
        terminal_reason = _terminal_reason_value_for_web_run(
            status=status,
            result_reason=getattr(result, "terminal_reason", None),
            cancelled_by_user=bool(cancel_event.is_set()),
            plan_mode_terminal_error=bool(plan_mode_terminal_error),
            llm_error=is_llm_error_message(assistant_response),
        )
        metadata_update = {
            "cancelled_by_user": bool(cancel_event.is_set()),
            "terminal_reason": terminal_reason,
            # A4: expose this invocation's token total so the Goal continuation
            # bridge can charge it against the session goal's token budget.
            "turn_tokens_used": int(getattr(result, "tokens_used", 0) or 0),
            **_runtime_prompt_metadata_update(runtime_session_context),
        }
        if plan_mode_terminal_error:
            metadata_update["interactive_pause"] = "plan_mode_missing_terminal_tool"
        if interactive_pause_summary and not str(assistant_response or "").strip():
            metadata_update["interactive_pause"] = interactive_pause_summary
            finalized = await _finalize_web_chat_run_without_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                status=status,
                result_summary=interactive_pause_summary,
                metadata_json=metadata_update,
                file_change_paths=_terminal_file_change_paths_for_turn(runtime_session_context),
                file_change_states=_terminal_file_change_states_for_turn(runtime_session_context),
                file_change_lineage=_terminal_file_change_lineage_for_turn(runtime_session_context),
            )
            if not finalized:
                return
            await _emit_terminal_turn_hook(
                agent_id=agent.id,
                session_id=session_id,
                run_uuid=run_uuid,
                runtime_metadata=metadata,
                status=status,
                reason="interactive_pause",
                source=runtime_session_context.source,
                extra_metadata=metadata_update,
            )
            await broadcast_web_chat_event(agent.id, session_id, build_done_event(""))
            return
        file_change_paths = _terminal_file_change_paths_for_turn(runtime_session_context)
        file_change_states = _terminal_file_change_states_for_turn(runtime_session_context)
        file_change_lineage = _terminal_file_change_lineage_for_turn(runtime_session_context)
        declared_artifact_paths = _declared_terminal_artifact_paths(assistant_response)
        artifact_paths = _terminal_artifact_paths_for_turn(runtime_session_context, assistant_response)
        rejected_artifact_paths = _rejected_terminal_artifact_paths_for_turn(
            runtime_session_context, assistant_response
        )
        try:
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=actor_user_id,
                session_id=session_id,
                content=assistant_response,
                thinking=thinking,
                thinking_signature=getattr(result, "reasoning_signature", None),
                status=status,
                result_summary=_simulation_title(assistant_response),
                metadata_json=metadata_update,
                artifact_paths=artifact_paths,
                file_change_paths=file_change_paths,
                file_change_states=file_change_states,
                file_change_lineage=file_change_lineage,
                declared_artifact_paths=declared_artifact_paths,
                rejected_artifact_paths=rejected_artifact_paths,
            )
        except IntegrityError as exc:
            if _is_final_assistant_marker_unique_violation(exc):
                logger.info(
                    "[WebChatRun] Terminal assistant finalization lost idempotency race for run {}",
                    run_uuid.hex,
                )
                return
            raise
        if not finalized:
            return
        await _emit_terminal_turn_hook(
            agent_id=agent.id,
            session_id=session_id,
            run_uuid=run_uuid,
            runtime_metadata=metadata,
            status=status,
            reason="invoke_complete",
            source=runtime_session_context.source,
            extra_metadata=metadata_update,
        )
        await broadcast_web_chat_event(
            agent.id,
            session_id,
            build_done_event(assistant_response, thinking=thinking, artifacts=metadata_update.get("artifacts")),
        )
    except Exception as exc:
        logger.exception("[WebChatRun] Run {} failed", run_uuid.hex)
        was_cancelled = cancel_event.is_set()
        terminal_phase_hint = RuntimePhase.CANCELLED if was_cancelled else RuntimePhase.FAILED
        if was_cancelled:
            await _update_runtime_task(
                run_uuid,
                status="killed",
                result_summary="Generation stopped by user.",
                metadata_json={"cancelled_by_user": True},
            )
            if terminal_agent_id and terminal_session_id:
                await _emit_terminal_turn_hook(
                    agent_id=terminal_agent_id,
                    session_id=terminal_session_id,
                    run_uuid=run_uuid,
                    runtime_metadata=terminal_runtime_metadata,
                    status="killed",
                    reason="user_cancelled",
                    extra_metadata={"cancelled_by_user": True, "terminal_reason": TerminalReason.USER_CANCEL.value},
                )
            return
        result_summary = f"Web chat run failed: {type(exc).__name__}"
        metadata_update = {"error": str(exc)[:500], "terminal_reason": TerminalReason.PROVIDER_ERROR.value}
        try:
            if stream_batcher is not None:
                await stream_batcher.flush()
            runtime_task, agent, user, *_rest = await _load_runtime_context(run_uuid)
            failed_actor_user_id = _runtime_actor_user_id(user)
            session_id = str(runtime_task.parent_session_id)
            failed_turn_change_paths = (
                _terminal_file_change_paths_for_turn(runtime_session_context)
                if runtime_session_context is not None
                else []
            )
            failed_turn_change_kwargs = (
                {
                    "file_change_paths": failed_turn_change_paths,
                    "file_change_states": _terminal_file_change_states_for_turn(runtime_session_context),
                    "file_change_lineage": _terminal_file_change_lineage_for_turn(runtime_session_context),
                }
                if failed_turn_change_paths
                else {}
            )
            finalized = await _finalize_web_chat_run_with_assistant(
                run_uuid=run_uuid,
                agent_id=agent.id,
                user_id=failed_actor_user_id,
                session_id=session_id,
                content=_USER_VISIBLE_WEB_CHAT_ERROR,
                thinking=None,
                thinking_signature=None,
                status="failed",
                result_summary=result_summary,
                metadata_json=metadata_update,
                **failed_turn_change_kwargs,
            )
            if not finalized:
                await _update_runtime_task(
                    run_uuid,
                    status="failed",
                    result_summary=result_summary,
                    metadata_json=metadata_update,
                )
            await _emit_terminal_turn_hook(
                agent_id=agent.id,
                session_id=session_id,
                run_uuid=run_uuid,
                runtime_metadata=terminal_runtime_metadata,
                status="failed",
                reason="runtime_exception",
                extra_metadata=metadata_update,
            )
            await broadcast_web_chat_event(
                agent.id,
                session_id,
                {"type": "error", "content": _USER_VISIBLE_WEB_CHAT_ERROR},
            )
        except Exception as terminal_exc:
            logger.warning(
                "[WebChatRun] Failed to persist visible terminal error for {}: {}", run_uuid.hex, terminal_exc
            )
            persistence_metadata = {
                "error": str(terminal_exc)[:500],
                "original_error": str(exc)[:500],
                "terminal_reason": TerminalReason.PERSISTENCE_ERROR.value,
                "persistence_error": True,
            }
            await _update_runtime_task(
                run_uuid,
                status="failed",
                result_summary=f"Web chat persistence failed: {type(terminal_exc).__name__}",
                metadata_json=persistence_metadata,
            )
    finally:
        if phase_emitter is not None:
            settled_phase = terminal_phase_hint or (
                RuntimePhase.CANCELLED if cancel_event.is_set() else RuntimePhase.DONE
            )
            await phase_emitter.transition(settled_phase)
        _CURRENT_BROADCAST_RUN_ID.reset(broadcast_run_token)
        _CANCEL_EVENTS.pop(run_key, None)
        if terminal_agent_id is not None and terminal_session_id:
            try:
                await _resume_queued_plan_handoffs(
                    agent_id=terminal_agent_id,
                    session_id=terminal_session_id,
                    completed_run_id=run_key,
                )
            except Exception as exc:  # noqa: BLE001 - terminal cleanup must not mask run outcome
                logger.warning(
                    "[WebChatRun] queued Plan Mode handoff cleanup failed: run_id={} error={}",
                    run_key,
                    exc,
                )
