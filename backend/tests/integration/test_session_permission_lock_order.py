"""Permission writers share the transcript advisory before taking row locks."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.database import pin_rls_tenant_context
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionControlInput, SessionToolInvocation
from app.services.chat_transcript import lock_transcript_session
from app.services.session_control_input import (
    accept_tool_permission_response,
    apply_permission_response_control_input,
)
from app.services.session_permission_runtime import (
    expire_stale_session_permission_requests,
    resolve_session_tool_permission,
)
from tests.services.test_session_permission_runtime import _authority, _event_count, _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _wait_for_advisory(owner_sessionmaker, pid: int, job: asyncio.Task) -> None:
    async with asyncio.timeout(10), owner_sessionmaker() as inspector:
        while True:
            if job.done():
                job.result()
                pytest.fail("The permission writer bypassed the held session advisory")
            wait = (await inspector.execute(text(
                "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = :pid"
            ), {"pid": pid})).first()
            await inspector.rollback()
            if wait is not None and tuple(wait) == ("Lock", "advisory"):
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("operation", ["expiry", "resolve", "accept", "apply", "lifecycle"])
async def test_permission_writer_does_not_hold_rows_while_waiting_for_transcript(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, operation,
):
    seed = await _seed_waiting_batch(owner_sessionmaker)
    control_id = uuid.uuid5(seed.permission_ids[0], "permission-response:deny")
    async with app_user_sessionmaker() as writer, app_user_sessionmaker() as contender:
        await pin_rls_tenant_context(writer, seed.tenant_id)
        await pin_rls_tenant_context(contender, seed.tenant_id)
        role = (await contender.execute(text(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ))).one()
        assert tuple(role) == (False, False)
        _user, authority = await _authority(writer, seed)
        invocation = await writer.get(SessionToolInvocation, seed.invocation_ids[0])
        assert invocation is not None and invocation.permission_expires_at is not None
        expiry_time = invocation.permission_expires_at + timedelta(seconds=1)
        binding = {
            "authority": authority,
            "control_id": control_id,
            "idempotency_key": f"permission:{seed.permission_ids[0]}:deny",
            "invocation_id": invocation.id,
            "permission_item_id": invocation.permission_item_id,
            "permission_request_version": invocation.permission_request_version,
            "permission_authority_snapshot_hash": invocation.permission_authority_snapshot_hash,
            "expected_run_id": seed.run_id,
            "decision": "deny",
            "response_schema": "hive.tool_permission_response.v1",
        }
        if operation == "apply":
            await accept_tool_permission_response(writer, **binding)
        await writer.commit()
        await lock_transcript_session(writer, session_id=seed.session_id)
        pid = await contender.scalar(text("SELECT pg_backend_pid()"))

        if operation == "lifecycle":
            from app.services import tenant_resolver, web_chat_runtime

            @asynccontextmanager
            async def runtime_scope(tenant_id):
                assert tenant_id == seed.tenant_id
                yield contender

            monkeypatch.setattr(tenant_resolver, "async_session", app_user_sessionmaker)
            monkeypatch.setattr(web_chat_runtime, "tenant_scoped_session", runtime_scope)

        async def competing_write():
            if operation == "expiry":
                return await expire_stale_session_permission_requests(db=contender, now=expiry_time)
            if operation == "lifecycle":
                return await web_chat_runtime._persist_tool_call(
                    agent_id=seed.agent_id, user_id=seed.user_id, session_id=str(seed.session_id),
                    data={"status": "completed", "provider_request_id": seed.request_id,
                          "tool_call_id": invocation.provider_tool_use_id, "runtime_task_id": str(seed.run_id),
                          "name": invocation.tool_name, "args": invocation.provider_arguments_json,
                          "result": "late stale output"},
                )
            if operation == "resolve":
                result = await resolve_session_tool_permission(
                    contender, authority=authority,
                    permission_request_id=seed.permission_ids[0], decision="deny",
                )
            elif operation == "accept":
                result = await accept_tool_permission_response(contender, **binding)
            else:
                result = await apply_permission_response_control_input(
                    contender, authority=authority, control_id=control_id,
                )
            await contender.commit()
            return result

        job = asyncio.create_task(competing_write())
        try:
            await _wait_for_advisory(owner_sessionmaker, pid, job)
            # A writer already holding the advisory must still be able to lock
            # the invocation. NOWAIT exposes the reverse edge deterministically.
            await writer.execute(select(SessionToolInvocation).where(
                SessionToolInvocation.id == seed.invocation_ids[0],
            ).with_for_update(nowait=True))
            receipt = await resolve_session_tool_permission(
                writer, authority=authority,
                permission_request_id=seed.permission_ids[0], decision="deny",
            )
            await writer.commit()
            assert receipt.status == "resolved"
        finally:
            await writer.rollback()
            try:
                other = await asyncio.wait_for(job, timeout=10)
            finally:
                if not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
        if operation == "expiry":
            assert other == 0
        elif operation == "lifecycle":
            assert other and "permission_denied" in str(other) and "late stale output" not in str(other)
        else:
            assert other.status in {"resolved", "applied"}

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        task = await db.get(RuntimeTask, seed.run_id)
        assert invocation.permission_state == "denied"
        assert invocation.result_event_id is not None and invocation.execution_fence_ref is None
        assert task.status == "resumable"
        assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 1
        controls = (await db.scalars(select(SessionControlInput).where(
            SessionControlInput.session_id == seed.session_id,
            SessionControlInput.kind == "permission_response",
        ))).all()
        assert len(controls) == 1 and controls[0].id == control_id and controls[0].status == "applied"


@pytest.mark.parametrize("operation", ["prepare", "start", "fresh_start", "reconcile"])
async def test_tool_retry_reloads_a_settlement_over_its_cached_approved_invocation(
    owner_sessionmaker, app_user_sessionmaker, operation,
):
    from app.services.session_tool_runtime import (
        complete_tool_invocation,
        mark_tool_effect_started,
        mark_tool_invocation_needs_reconciliation,
        prepare_tool_invocation,
    )
    from app.models.chat_transcript_event import ChatTranscriptEvent

    seed = await _seed_waiting_batch(owner_sessionmaker)
    control_id = uuid.uuid5(seed.permission_ids[0], "permission-response:allow_once")
    async with app_user_sessionmaker() as writer, app_user_sessionmaker() as retry:
        await pin_rls_tenant_context(writer, seed.tenant_id)
        await pin_rls_tenant_context(retry, seed.tenant_id)
        _user, authority = await _authority(writer, seed)
        invocation = await writer.get(SessionToolInvocation, seed.invocation_ids[0])
        await accept_tool_permission_response(
            writer, authority=authority, control_id=control_id,
            idempotency_key=f"permission:{seed.permission_ids[0]}:allow_once",
            invocation_id=invocation.id, permission_item_id=invocation.permission_item_id,
            permission_request_version=invocation.permission_request_version,
            permission_authority_snapshot_hash=invocation.permission_authority_snapshot_hash,
            expected_run_id=seed.run_id, decision="allow_once", response_schema="hive.tool_permission_response.v1",
        )
        await apply_permission_response_control_input(writer, authority=authority, control_id=control_id)
        await writer.commit()
        cached = await retry.get(SessionToolInvocation, invocation.id)
        assert cached.permission_state == "approved" and cached.effect_state == "prepared_not_started"
        assert cached.result_event_id is None
        await complete_tool_invocation(
            writer, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            session_id=seed.session_id, invocation_id=invocation.id,
            provider_result_content="Current policy refused this read.",
            execution_evidence={
                "schema": "hive.tool_execution_evidence.v1", "status": "settled", "retryable": False,
                "tool_decision": {
                    "schema": "hive.tool_decision.v1", "decision_id": f"policy-denied:{invocation.id}",
                    "outcome": "deny", "input_hash": invocation.effective_args_hash or invocation.args_hash,
                    "policy_snapshot_hash": "d" * 64,
                },
                "execution_frame": None,
            },
        )
        await writer.commit()
        settled = (await retry.execute(select(
            SessionToolInvocation.result_event_id, SessionToolInvocation.effect_state,
        ).where(SessionToolInvocation.id == cached.id))).one()
        assert settled.result_event_id is not None and settled.effect_state == "prepared_not_started"
        terminal = await retry.get(ChatTranscriptEvent, settled.result_event_id)
        assert terminal.item_kind == "tool_result" and terminal.lifecycle == "completed"
        assert terminal.metadata_json["v2_payload"]["outcome"] == "denied"
        # Keep the pre-settlement ORM object alive across the other writer's
        # commit; row-locking alone must not reuse its approved/prepared state.
        assert cached.result_event_id is None
        common = {"tenant_id": seed.tenant_id, "agent_id": seed.agent_id,
                  "session_id": seed.session_id, "invocation_id": cached.id}
        if operation == "prepare":
            replay = await prepare_tool_invocation(
                retry, tenant_id=seed.tenant_id, agent_id=seed.agent_id, session_id=seed.session_id,
                run_id=seed.run_id, provider_request_id=seed.request_id,
                provider_tool_use_id=cached.provider_tool_use_id, tool_name=cached.tool_name,
                arguments=cached.provider_arguments_json,
            )
            assert replay.result_event_id == settled.result_event_id
        elif operation in {"start", "fresh_start"}:
            if operation == "fresh_start":
                retry.expire_all()
            with pytest.raises(RuntimeError, match="^tool_effect_start_requires_unsettled_invocation$"):
                await mark_tool_effect_started(retry, **common, permission_control_id=control_id)
        else:
            replay = await mark_tool_invocation_needs_reconciliation(
                retry, **common, reason_code="receipt_retry", recovery_owner="native-test",
            )
            assert any(event.id == settled.result_event_id for event in replay)
        await retry.rollback()
        await writer.refresh(invocation)
        assert invocation.result_event_id == settled.result_event_id
        assert invocation.effect_state == "prepared_not_started"
        assert invocation.execution_fence_ref is None
        assert await _event_count(writer, invocation_id=invocation.id, item_kind="tool_result") == 1
