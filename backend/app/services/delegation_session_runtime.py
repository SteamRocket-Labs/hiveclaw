"""Session V2 persistence ports for the existing delegated Agent kernel."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from sqlalchemy import String, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database import tenant_scoped_session
from app.kernel.contracts import SessionRestartRecoveryRequired, ToolLifecyclePersistenceError
from app.models.runtime_task import RuntimeTask
from app.services import session_model_round as rounds
from app.services.runtime_task_fence import assert_runtime_task_fence


async def suspend_delegation_for_question(request, *, provider_request_id):
    """Keep a native question or permission wait on the original delegated Run."""
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionInputAdmission, SessionModelResult, SessionToolInvocation, SessionTurnInput
    from app.services.runtime_root_ledger import transition_runtime_root_item_by_task
    from app.services.runtime_terminal_settlement import lock_runtime_task_with_session_authority

    async with tenant_scoped_session(request.tenant_id, require_tenant=True) as db:
        resumed = await db.scalar(select(SessionModelResult.id).join(RuntimeTask,
            RuntimeTask.id == SessionModelResult.run_id).join(ChatSession,
            ChatSession.id == SessionModelResult.session_id).where(
            RuntimeTask.id == uuid.UUID(str(request.runtime_task_id)), RuntimeTask.tenant_id == request.tenant_id,
            RuntimeTask.task_type == "delegation", RuntimeTask.status == "resumable",
            func.replace(func.lower(RuntimeTask.child_session_id), '-', '') == func.replace(ChatSession.id.cast(String), '-', ''),
            RuntimeTask.child_agent_id == ChatSession.agent_id, ChatSession.tenant_id == RuntimeTask.tenant_id,
            ChatSession.runtime_task_id == RuntimeTask.id, SessionModelResult.tenant_id == RuntimeTask.tenant_id,
            SessionModelResult.provider_request_id == provider_request_id, SessionModelResult.state == "round_committed",
            RuntimeTask.metadata_json["session_permission_resume"]["source_result_id"].astext == SessionModelResult.id.cast(String)))
        if resumed is not None:
            return  # Observe the committed response without reading or mutating a stale worker claim.
        task = await lock_runtime_task_with_session_authority(db, task_id=uuid.UUID(str(request.runtime_task_id)))
        if task is None:
            raise SessionRestartRecoveryRequired(reason_code="delegation_session_scope_unavailable", detail={})
        child_session_id = uuid.UUID(str(task.child_session_id))
        model = await db.scalar(select(SessionModelResult).join(ChatSession,
            ChatSession.id == SessionModelResult.session_id).where(
            SessionModelResult.tenant_id == task.tenant_id, ChatSession.tenant_id == task.tenant_id,
            ChatSession.agent_id == task.child_agent_id, ChatSession.runtime_task_id == task.id,
            SessionModelResult.session_id == child_session_id, SessionModelResult.run_id == task.id,
            SessionModelResult.provider_request_id == provider_request_id, SessionModelResult.state == "round_committed"))
        if model is None:
            raise SessionRestartRecoveryRequired(reason_code="delegation_committed_round_missing", detail={})
        metadata = dict(task.metadata_json or {})
        assert_runtime_task_fence(task)
        pending = list(await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.tenant_id == task.tenant_id, SessionToolInvocation.run_id == task.id,
            SessionToolInvocation.session_id == child_session_id,
            SessionToolInvocation.provider_request_id == model.provider_request_id,
            SessionToolInvocation.permission_state == "waiting", SessionToolInvocation.effect_state == "prepared_not_started",
            SessionToolInvocation.result_event_id.is_(None))))
        if pending and all(row.permission_item_id is not None for row in pending):
            task.metadata_json = {**metadata, "interactive_pause": "awaiting_session_permission",
                "delegation_waiting_permission_ids": [str(row.permission_item_id) for row in pending],
                "terminal_reason": "clarification_required"}
            task.status, task.result_summary = "suspended", "awaiting_session_permission"
            task.claimed_by = task.claim_expires_at = task.completed_at = None
            await transition_runtime_root_item_by_task(db, runtime_task_id=task.id,
                requested_state="suspended", reason_code="delegation_permission")
            from app.services.original_work_permission_notices import enqueue_original_work_permission_notices

            await enqueue_original_work_permission_notices(db, run=task, model=model, invocations=pending)
            await db.commit()
            return
        question = await db.scalar(select(ChatTranscriptEvent).join(
            SessionModelResult, SessionModelResult.id == ChatTranscriptEvent.result_id).where(
            ChatTranscriptEvent.tenant_id == task.tenant_id, ChatTranscriptEvent.agent_id == task.child_agent_id,
            ChatTranscriptEvent.session_id == child_session_id, ChatTranscriptEvent.run_id == task.id,
            ChatTranscriptEvent.item_kind == "user_question",
            SessionModelResult.provider_request_id == provider_request_id,
            SessionModelResult.state == "round_committed",
        ).order_by(ChatTranscriptEvent.sequence.desc()).limit(1))
        if question is None or question.lifecycle not in {"waiting", "completed"}:
            raise SessionRestartRecoveryRequired(reason_code="delegation_question_receipt_missing", detail={})
        answer = await db.scalar(select(SessionTurnInput.id).join(
            SessionInputAdmission, SessionInputAdmission.input_id == SessionTurnInput.id).where(
            SessionTurnInput.tenant_id == task.tenant_id, SessionTurnInput.session_id == child_session_id,
            SessionTurnInput.target_run_id == task.id, SessionTurnInput.request_item_id == question.item_id,
            SessionTurnInput.intent == "answer_request", SessionTurnInput.status == "queued",
            SessionInputAdmission.input_revision == SessionTurnInput.revision,
            SessionInputAdmission.state == "admitted"))
        task.status = "pending" if answer is not None else "suspended"
        task.metadata_json = {**(task.metadata_json or {}), "delegation_waiting_question_item_id": str(question.item_id),
            "terminal_reason": "clarification_required"}
        task.claimed_by, task.claim_expires_at = None, None
        await transition_runtime_root_item_by_task(db, runtime_task_id=task.id,
            requested_state="queued" if answer is not None else "suspended", reason_code="delegation_question")
        if answer is None and question.lifecycle == "waiting":
            from app.services.employee_first_task import enqueue_first_task_question_notice

            await enqueue_first_task_question_notice(db, task=task, question=question)
        await db.commit()


@dataclass
class DelegationSessionRuntime:
    scope: dict[str, Any]
    attempt_owner: str
    session_context: Any
    pending_replay_round: int | None = None
    active_provider_request_id: str | None = None
    final_content: str | None = None
    user_id: uuid.UUID | None = None
    lane: str = 'delegation'

    @classmethod
    async def attach_business_task(cls, invocation, *, runtime_task_id, business_task_id):
        from app.models.chat_session import ChatSession
        from app.services.business_task_context import bind_business_task_context
        from app.services.delegation_context_sources import bind_native_recovery_context
        from app.services.web_chat_runtime import _session_restart_resume_history

        session_id = uuid.UUID(invocation.session_context.session_id)
        async with tenant_scoped_session(invocation.tenant_id, require_tenant=True) as db:
            task = await db.get(RuntimeTask, runtime_task_id, populate_existing=True)
            session = await db.get(ChatSession, session_id, populate_existing=True)
            if (task is None or task.task_type != 'business_task' or task.tenant_id != invocation.tenant_id
                or task.parent_agent_id != invocation.agent_id or task.root_user_id != invocation.user_id
                or task.child_session_id != str(session_id) or task.status != 'running'
                or (task.metadata_json or {}).get('business_task_id') != str(business_task_id)
                or session is None or session.runtime_task_id != task.id or session.user_id != invocation.user_id
                or session.agent_id != invocation.agent_id or session.tenant_id != invocation.tenant_id):
                raise RuntimeError('business_task_session_runtime_scope_unavailable')
            assert_runtime_task_fence(task)
            await bind_business_task_context(db, run=task, invocation=invocation)
            await bind_native_recovery_context(db, task=task, session=invocation.session_context)
            history, round_index, tokens, receipt = await _session_restart_resume_history(db, task)
            turn_id = f'turn-{task.id.hex}'
            runtime = cls(scope={'tenant_id': invocation.tenant_id, 'agent_id': invocation.agent_id,
                'session_id': session_id, 'run_id': task.id, 'turn_id': turn_id},
                attempt_owner=f'{task.claimed_by}:{task.claim_version}:{task.attempt_count}',
                session_context=invocation.session_context, user_id=invocation.user_id, lane='business_task',
                pending_replay_round=receipt.get('pending_tool_round') or receipt.get('pending_final_round'))
        invocation.messages.extend(history)
        invocation.initial_round_index, invocation.initial_turn_tokens_used = round_index, tokens
        invocation.session_context.metadata.update(turn_id=turn_id, session_restart_resume=receipt,
            session_v2_durable_tool_lifecycle=True)
        invocation.round_input_bind, invocation.model_request_prepare = runtime.bind, runtime.prepare
        invocation.model_response_commit, invocation.model_request_fail = runtime.commit, runtime.fail
        return runtime

    @classmethod
    async def attach(cls, invocation, request):
        from app.services.web_chat_runtime import _session_restart_resume_history

        run_id = uuid.UUID(str(request.runtime_task_id))
        session_id = uuid.UUID(str(invocation.session_context.session_id))
        async with tenant_scoped_session(invocation.tenant_id, require_tenant=True) as db:
            task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == run_id,
                RuntimeTask.tenant_id == invocation.tenant_id, RuntimeTask.task_type == "delegation",
                RuntimeTask.child_agent_id == invocation.agent_id,
                func.replace(func.lower(RuntimeTask.child_session_id), "-", "") == session_id.hex,
                RuntimeTask.root_user_id == request.owner_id, RuntimeTask.status == "running",
                RuntimeTask.parent_agent_id == uuid.UUID(str(request.parent_agent_id)),
                func.replace(func.lower(RuntimeTask.parent_session_id), "-", "") == uuid.UUID(str(request.parent_session_id)).hex))
            if task is None:
                raise RuntimeError("delegation_session_runtime_scope_unavailable")
            assert_runtime_task_fence(task)
            from app.services.delegation_context_sources import bind_delegation_context_sources, bind_native_recovery_context

            await bind_delegation_context_sources(db, task=task, invocation=invocation)
            await bind_native_recovery_context(db, task=task, session=invocation.session_context)
            from app.services.delegation_input_origin import load_delegation_instruction_origin

            if (task.metadata_json or {}).get("conversation_messages") == request.conversation_messages:
                origin = await load_delegation_instruction_origin(db, request=request,
                    tenant_id=invocation.tenant_id, content=invocation.messages[0]["content"])
                if origin is not None:
                    invocation.messages[0]["context_origin"] = origin
            history, round_index, tokens, receipt = await _session_restart_resume_history(db, task)
            await db.commit()
            turn_id = str((task.metadata_json or {}).get("turn_id") or f"turn-{run_id.hex}")
            runtime = cls(scope={"tenant_id": invocation.tenant_id, "agent_id": invocation.agent_id,
                "session_id": session_id, "run_id": run_id, "turn_id": turn_id},
                attempt_owner=f"{task.claimed_by}:{task.claim_version}:{task.attempt_count}",
                session_context=invocation.session_context,
                pending_replay_round=receipt.get("pending_tool_round") or receipt.get("pending_final_round"))
        invocation.messages.extend(history)
        invocation.initial_round_index = round_index
        invocation.initial_turn_tokens_used = tokens
        invocation.session_context.metadata.update(turn_id=turn_id, session_restart_resume=receipt,
            session_v2_durable_tool_lifecycle=True)
        invocation.round_input_bind = runtime.bind
        invocation.model_request_prepare = runtime.prepare
        invocation.model_response_commit = runtime.commit
        invocation.model_request_fail = runtime.fail
        return runtime

    @asynccontextmanager
    async def transaction(self):
        from app.services.chat_transcript import lock_transcript_session

        async with tenant_scoped_session(self.scope["tenant_id"], require_tenant=True) as db:
            try:
                await lock_transcript_session(db, session_id=self.scope["session_id"])
                task = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == self.scope["run_id"],
                    RuntimeTask.tenant_id == self.scope["tenant_id"]).with_for_update())
                if (task is None or task.status != "running" or task.claim_expires_at is None
                    or task.claim_expires_at <= datetime.now(timezone.utc)
                    or f"{task.claimed_by}:{task.claim_version}:{task.attempt_count}" != self.attempt_owner):
                    raise SessionRestartRecoveryRequired(reason_code=f"{self.lane}_session_claim_lost", detail={})
                assert_runtime_task_fence(task)
                yield db
                await db.commit()
            except rounds.ModelRoundNeedsReconciliation as exc:
                await db.commit()
                raise SessionRestartRecoveryRequired(reason_code="model_round_needs_reconciliation",
                    detail={"run_id": str(self.scope["run_id"])}) from exc
            except SQLAlchemyError as exc:
                raise SessionRestartRecoveryRequired(reason_code=f"{self.lane}_session_persistence_unavailable",
                    detail={"run_id": str(self.scope["run_id"])}) from exc

    async def bind(self, round_index):
        async with self.transaction() as db:
            return await rounds.bind_round_inputs(db, **self.scope, round_index=round_index)

    async def prepare(self, **payload):
        run_id = self.scope["run_id"]
        payload.setdefault("logical_root_result_id", uuid.uuid5(run_id,
            f"session-model-result:{run_id}:round:{int(payload['round_index'])}"))
        async with self.transaction() as db:
            provider_request_id = await rounds.prepare_model_request(db, **self.scope, **payload,
                attempt_owner=self.attempt_owner, resume_committed_round=(
                    int(payload.get("continuation_index") or 0) == 0
                    and int(payload["round_index"]) == self.pending_replay_round))
            receipt = await rounds.resume_committed_round_receipt(db,
                **{key: value for key, value in self.scope.items() if key != "turn_id"},
                round_index=int(payload["round_index"]), provider_request_id=provider_request_id,
                continuation_index=0) if int(payload.get("continuation_index") or 0) == 0 else None
            if receipt:
                await self.enqueue_progress(db, provider_request_id)
        self.active_provider_request_id = provider_request_id
        if receipt:
            response = (receipt.get("sealed_round_resume") or {}).get("response") or {}
            self.final_content = str(response.get("content") or "") if not response.get("tool_calls") else None
        return receipt or provider_request_id

    async def commit(self, **payload):
        result_payload = {key: value for key, value in payload.items() if key not in {"provider", "model"}}
        async with self.transaction() as db:
            seal = await rounds.seal_model_response(db, **self.scope, **result_payload)
        # Preserve the same crash-recoverable seal/registry boundary as web turns.
        async with self.transaction() as db:
            await rounds.commit_sealed_model_round(db, **self.scope,
                round_index=int(payload["round_index"]), provider_request_id=payload["provider_request_id"],
                continuation_index=int(payload.get("continuation_index") or 0))
            await self.enqueue_progress(db, payload['provider_request_id'])
        if payload.get("logical_round_complete", True):
            self.active_provider_request_id = payload["provider_request_id"]
            response = payload.get("response") or {}
            self.final_content = str(response.get("content") or "") if not response.get("tool_calls") else None
        return seal

    async def enqueue_progress(self, db, provider_request_id):
        if self.lane != 'delegation':
            return
        from fastapi import HTTPException
        from app.services.content_input_receipts import ContentInputReceiptError
        from app.services.original_work_progress import enqueue_original_work_progress

        try:
            async with db.begin_nested():
                await enqueue_original_work_progress(db, run_id=self.scope['run_id'], provider_request_id=provider_request_id)
        except (ContentInputReceiptError, HTTPException, SQLAlchemyError) as exc:
            from logging import getLogger

            getLogger(__name__).warning('Original-work progress unavailable: run=%s code=%s',
                self.scope['run_id'], getattr(exc, 'code', type(exc).__name__))

    async def fail(self, **payload):
        async with self.transaction() as db:
            await rounds.fail_model_request(db, **self.scope, **payload)

    async def tool_call(self, data):
        from app.services.web_chat_runtime import _persist_tool_call

        try:
            await _persist_tool_call(agent_id=self.scope["agent_id"], user_id=self.user_id,
                session_id=str(self.scope["session_id"]), data={**data,
                    "provider_request_id": self.active_provider_request_id,
                    "runtime_task_id": str(self.scope["run_id"])})
        except (SessionRestartRecoveryRequired, ToolLifecyclePersistenceError):
            raise
        except Exception as exc:
            raise ToolLifecyclePersistenceError(tool_name=str(data.get("name") or ""),
                provider_tool_use_id=str(data.get("tool_call_id") or ""),
                lifecycle=str(data.get("status") or "")) from exc
        if data.get("status") in {"done", "completed"}:
            from app.services.delegation_context_sources import capture_native_recovery_context, NativeRecoveryContextSource

            try:
                async with self.transaction() as db:
                    task = await db.get(RuntimeTask, self.scope["run_id"])
                    await capture_native_recovery_context(db, task=task, session=self.session_context,
                        provider_request_id=self.active_provider_request_id)
            except Exception:
                checkpoint = self.session_context.metadata.get("_native_recovery_checkpoint")
                self.session_context.metadata.pop("_native_recovery_context_source", None)
                if isinstance(checkpoint, dict):
                    self.session_context.metadata["_native_recovery_context_source"] = NativeRecoveryContextSource(
                        checkpoint, (), ("native_recovery_capture_unavailable",))
