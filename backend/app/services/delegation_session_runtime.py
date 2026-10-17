"""Session V2 persistence ports for the existing delegated Agent kernel."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.database import tenant_scoped_session
from app.kernel.contracts import SessionRestartRecoveryRequired, ToolLifecyclePersistenceError
from app.models.runtime_task import RuntimeTask
from app.services import session_model_round as rounds
from app.services.runtime_task_fence import assert_runtime_task_fence


@dataclass
class DelegationSessionRuntime:
    scope: dict[str, Any]
    attempt_owner: str
    pending_replay_round: int | None = None
    active_provider_request_id: str | None = None
    final_content: str | None = None

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
            from app.services.delegation_context_sources import bind_delegation_context_sources

            await bind_delegation_context_sources(db, task=task, invocation=invocation)
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
                pending_replay_round=receipt.get("pending_tool_round") or receipt.get("pending_final_round"))
        invocation.messages.extend(history)
        invocation.initial_round_index = round_index
        invocation.initial_turn_tokens_used = tokens
        invocation.session_context.metadata.update(turn_id=turn_id, session_restart_resume=receipt)
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
                    raise SessionRestartRecoveryRequired(reason_code="delegation_session_claim_lost", detail={})
                assert_runtime_task_fence(task)
                yield db
                await db.commit()
            except rounds.ModelRoundNeedsReconciliation as exc:
                await db.commit()
                raise SessionRestartRecoveryRequired(reason_code="model_round_needs_reconciliation",
                    detail={"run_id": str(self.scope["run_id"])}) from exc
            except SQLAlchemyError as exc:
                raise SessionRestartRecoveryRequired(reason_code="delegation_session_persistence_unavailable",
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
        if payload.get("logical_round_complete", True):
            self.active_provider_request_id = payload["provider_request_id"]
            response = payload.get("response") or {}
            self.final_content = str(response.get("content") or "") if not response.get("tool_calls") else None
        return seal

    async def fail(self, **payload):
        async with self.transaction() as db:
            await rounds.fail_model_request(db, **self.scope, **payload)

    async def tool_call(self, data):
        from app.services.web_chat_runtime import _persist_tool_call

        try:
            await _persist_tool_call(agent_id=self.scope["agent_id"], user_id=None,
                session_id=str(self.scope["session_id"]), data={**data,
                    "provider_request_id": self.active_provider_request_id,
                    "runtime_task_id": str(self.scope["run_id"])})
        except ToolLifecyclePersistenceError:
            raise
        except Exception as exc:
            raise ToolLifecyclePersistenceError(tool_name=str(data.get("name") or ""),
                provider_tool_use_id=str(data.get("tool_call_id") or ""),
                lifecycle=str(data.get("status") or "")) from exc
