from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_waiting_batch(owner_sessionmaker, *, tool_count: int = 1) -> SimpleNamespace:
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.session_model_round import (
        commit_sealed_model_round,
        prepare_model_request,
        seal_model_response,
    )
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation

    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Permission Runtime Tenant", slug=f"perm-run-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"perm-run-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@permission-runtime.test",
                password_hash="x",
                display_name="Permission Runtime",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="Permission Runtime Agent",
                creator_id=user_id,
                owner_user_id=user_id,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_channel="web",
            )
        )
        task = RuntimeTask(
            id=run_id,
            task_type="web_chat_turn",
            status="running",
            parent_agent_id=agent_id,
            child_agent_id=agent_id,
            tenant_id=tenant_id,
            parent_session_id=str(session_id),
            child_session_id=str(session_id),
            root_user_id=user_id,
            root_session_id=str(session_id),
            root_runtime_task_id=run_id,
            prompt="read governed files",
            metadata_json={"turn_id": turn_id},
        )
        db.add(task)
        await db.flush()

        calls = [
            {
                "id": f"permission-tool-{index}-{run_id.hex}",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": f'{{"path":"workspace/file-{index}.md"}}',
                },
            }
            for index in range(tool_count)
        ]
        wire_request = {
            "messages": [{"role": "user", "content": "read the requested files"}],
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "max_tokens": 8192,
        }
        request_id = await prepare_model_request(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            continuation_index=0,
            messages=wire_request["messages"],
            tools=wire_request["tools"],
            provider="openai",
            model="gpt-4.1",
            wire_request=wire_request,
            attempt_owner="permission-runtime-test",
        )
        await seal_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={
                "content": None,
                "tool_calls": calls,
                "finish_reason": "tool_calls",
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )
        await commit_sealed_model_round(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
        )
        invocations = []
        for index, call in enumerate(calls):
            arguments = {"path": f"workspace/file-{index}.md"}
            invocation = await prepare_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
                provider_request_id=request_id,
                provider_tool_use_id=call["id"],
                tool_name="read_file",
                arguments=arguments,
            )
            await complete_tool_invocation(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation.id,
                provider_result_content="",
                execution_evidence={
                    "schema": "hive.tool_execution_evidence.v1",
                    "status": "settled",
                    "retryable": True,
                    "tool_decision": {
                        "schema": "hive.tool_decision.v1",
                        "decision_id": f"permission-decision-{invocation.id}",
                        "outcome": "require_approval",
                        "input_hash": invocation.args_hash,
                        "approval_id": str(uuid.uuid4()),
                    },
                    "execution_frame": None,
                },
            )
            invocations.append(invocation)
        task.status = "suspended"
        task.metadata_json = {
            **dict(task.metadata_json or {}),
            "interactive_pause": {
                "schema": "hive.tool_permission_wait.v1",
                "invocation_ids": [str(row.id) for row in invocations],
            },
        }
        await db.commit()
        for invocation in invocations:
            await db.refresh(invocation)
            assert invocation.permission_item_id is not None
    return SimpleNamespace(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        request_id=request_id,
        invocation_ids=[row.id for row in invocations],
        permission_ids=[row.permission_item_id for row in invocations],
    )


async def _authority(db, seed: SimpleNamespace):
    from app.models.user import User
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    user = await db.get(User, seed.user_id)
    assert user is not None
    authority = await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=seed.agent_id,
        session_id=seed.session_id,
        action="respond_tool_permission",
    )
    return user, authority


def _install_successful_executor(monkeypatch, *, result: str = "approved file bytes") -> list[str]:
    from app.services import agent_tools
    from app.services.approval_ticket import hash_tool_input

    calls: list[str] = []

    async def execute(tool_name, arguments, **kwargs):
        calls.append(str(kwargs.get("runtime_task_id")))
        callback = kwargs["pre_effect_callback"]
        await callback({"arguments": dict(arguments)})
        sink = kwargs["trace_metadata_sink"]
        sink.update(
            {
                "tool_decision": {
                    "schema": "hive.tool_decision.v1",
                    "decision_id": f"approved:{kwargs['tool_call_id']}",
                    "outcome": "allow",
                    "input_hash": hash_tool_input(tool_name, arguments),
                    "policy_snapshot_hash": "a" * 64,
                    "capability_snapshot_hash": "b" * 64,
                },
                "tool_execution_frame": {
                    "status": "completed",
                    "output_hash": "c" * 64,
                },
                "idempotency_key": f"tool-call:{kwargs['tool_call_id']}",
            }
        )
        return result

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    return calls


async def _event_count(db, *, invocation_id: uuid.UUID, item_kind: str, lifecycle: str | None = None) -> int:
    from app.models.chat_transcript_event import ChatTranscriptEvent

    statement = (
        select(func.count())
        .select_from(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.invocation_id == invocation_id,
            ChatTranscriptEvent.item_kind == item_kind,
        )
    )
    if lifecycle is not None:
        statement = statement.where(ChatTranscriptEvent.lifecycle == lifecycle)
    return int(await db.scalar(statement) or 0)


async def test_allow_once_executes_once_pairs_result_and_resumes_same_runtime_task(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_permission_runtime import resolve_session_tool_permission
    from app.services.web_chat_runtime import _session_permission_resume_history

    seed = await _seed_waiting_batch(owner_sessionmaker)
    executor_calls = _install_successful_executor(monkeypatch)
    async with owner_sessionmaker() as db:
        _user, authority = await _authority(db, seed)
        first = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[0],
            decision="allow_once",
        )
        replay = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[0],
            decision="allow_once",
        )

        task = await db.get(RuntimeTask, seed.run_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        task_count = await db.scalar(
            select(func.count())
            .select_from(RuntimeTask)
            .where(
                RuntimeTask.parent_agent_id == seed.agent_id,
                RuntimeTask.parent_session_id == str(seed.session_id),
            )
        )
        history, round_index, turn_tokens_used = await _session_permission_resume_history(db, task)

        assert first.status == replay.status == "resolved"
        assert first.run_id == replay.run_id == str(seed.run_id)
        assert first.result_event_id == replay.result_event_id
        assert task is not None and task.status == "resumable"
        assert task_count == 1
        assert invocation is not None and invocation.effect_state == "effect_committed"
        assert invocation.permission_state == "approved"
        assert executor_calls == [str(seed.run_id)]
        assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 1
        assert round_index == 1
        assert turn_tokens_used == 25
        assert history[0]["role"] == "assistant"
        assert history[0]["tool_calls"][0]["id"] == invocation.provider_tool_use_id
        assert history[1] == {
            "role": "tool",
            "tool_call_id": invocation.provider_tool_use_id,
            "content": "approved file bytes",
        }


async def test_allow_session_grant_is_exact_not_tool_wide(owner_sessionmaker, monkeypatch) -> None:
    from app.models.chat_session import ChatSession
    from app.models.session_v2 import SessionToolInvocation
    from app.services.approval_ticket import hash_tool_input
    from app.services.session_permission_runtime import resolve_session_tool_permission

    seed = await _seed_waiting_batch(owner_sessionmaker)
    _install_successful_executor(monkeypatch)
    async with owner_sessionmaker() as db:
        _user, authority = await _authority(db, seed)
        await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[0],
            decision="allow_session",
        )
        session = await db.get(ChatSession, seed.session_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        metadata = dict(session.transcript_metadata_json or {})
        grants = list(metadata.get("session_permission_grants") or [])
        arguments = dict(invocation.effective_arguments_json or {})

        assert len(grants) == 1
        assert grants[0]["tool_name"] == "read_file"
        assert grants[0]["input_hash"] == hash_tool_input("read_file", arguments)
        assert metadata.get("session_permission_allowed_tools") in (None, [])


async def test_deny_and_expiry_each_create_one_matching_result_and_resume_same_run(
    owner_sessionmaker,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_permission_runtime import (
        expire_stale_session_permission_requests,
        resolve_session_tool_permission,
    )

    denied = await _seed_waiting_batch(owner_sessionmaker)
    expired = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        _user, authority = await _authority(db, denied)
        denial = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=denied.permission_ids[0],
            decision="deny",
        )
        expired_invocation = await db.get(SessionToolInvocation, expired.invocation_ids[0])
        expired_invocation.permission_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        first_expiry_count = await expire_stale_session_permission_requests(
            db=db,
            now=datetime.now(timezone.utc),
            limit=1000,
        )
        second_expiry_count = await expire_stale_session_permission_requests(
            db=db,
            now=datetime.now(timezone.utc),
            limit=1000,
        )

        denied_task = await db.get(RuntimeTask, denied.run_id)
        expired_task = await db.get(RuntimeTask, expired.run_id)
        denied_invocation = await db.get(SessionToolInvocation, denied.invocation_ids[0])
        await db.refresh(expired_invocation)

        assert denial.status == "resolved"
        assert denied_task.status == expired_task.status == "resumable"
        assert denied_invocation.permission_state == "denied"
        assert expired_invocation.permission_state == "expired"
        assert first_expiry_count >= 1
        assert second_expiry_count == 0
        assert await _event_count(db, invocation_id=denied_invocation.id, item_kind="tool_result") == 1
        assert await _event_count(db, invocation_id=expired_invocation.id, item_kind="tool_result") == 1


async def test_parallel_permission_batch_resumes_only_after_every_item_is_settled(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.services.session_permission_runtime import resolve_session_tool_permission

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_count=2)
    async with owner_sessionmaker() as db:
        _user, authority = await _authority(db, seed)
        first = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[0],
            decision="deny",
        )
        task = await db.get(RuntimeTask, seed.run_id)
        assert first.status == "waiting_for_sibling_permissions"
        assert task.status == "suspended"

        second = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[1],
            decision="deny",
        )
        await db.refresh(task)
        queued_count = await db.scalar(
            select(func.count())
            .select_from(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.item_id == seed.run_id,
                ChatTranscriptEvent.item_kind == "run",
                ChatTranscriptEvent.lifecycle == "queued",
                ChatTranscriptEvent.metadata_json["v2_payload"]["reason_code"].astext == "session_permission_resolved",
            )
        )
        assert second.status == "resolved"
        assert task.status == "resumable"
        assert queued_count == 1
        for invocation_id in seed.invocation_ids:
            assert await _event_count(db, invocation_id=invocation_id, item_kind="tool_result") == 1


async def test_exception_after_effect_fence_quarantines_run_without_result_or_retry(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services import agent_tools
    from app.services.session_permission_runtime import resolve_session_tool_permission

    seed = await _seed_waiting_batch(owner_sessionmaker)
    executor_calls = 0

    async def execute(_tool_name, arguments, **kwargs):
        nonlocal executor_calls
        executor_calls += 1
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        raise RuntimeError("executor receipt lost after effect fence")

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    async with owner_sessionmaker() as db:
        _user, authority = await _authority(db, seed)
        receipt = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[0],
            decision="allow_once",
        )
        task = await db.get(RuntimeTask, seed.run_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])

        assert receipt.status == "needs_reconciliation"
        assert receipt.retryable is False
        assert receipt.recovery_action == "reconcile_tool_effect"
        assert task.status == "needs_reconciliation"
        assert invocation.effect_state == "needs_reconciliation"
        assert executor_calls == 1
        assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 0
        assert (
            await _event_count(
                db,
                invocation_id=invocation.id,
                item_kind="tool_call",
                lifecycle="needs_reconciliation",
            )
            == 1
        )


async def test_exception_before_effect_fence_is_truthfully_aborted_and_same_run_resumes(
    owner_sessionmaker,
    monkeypatch,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services import agent_tools
    from app.services.session_permission_runtime import resolve_session_tool_permission

    seed = await _seed_waiting_batch(owner_sessionmaker)

    async def execute(_tool_name, _arguments, **_kwargs):
        raise RuntimeError("runtime unavailable before effect authority")

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    async with owner_sessionmaker() as db:
        _user, authority = await _authority(db, seed)
        receipt = await resolve_session_tool_permission(
            db,
            authority=authority,
            permission_request_id=seed.permission_ids[0],
            decision="allow_once",
        )
        task = await db.get(RuntimeTask, seed.run_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])

        assert receipt.status == "resolved"
        assert task.status == "resumable"
        assert invocation.effect_state == "prepared_not_started"
        assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 1


async def test_channel_permission_command_uses_canonical_pending_aggregate_and_requires_id_for_ambiguity(
    owner_sessionmaker,
) -> None:
    from app.models.user import User
    from app.services.channel_agent_runtime import try_resolve_channel_session_permission_from_text

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_count=2)
    async with owner_sessionmaker() as db:
        user = await db.get(User, seed.user_id)
        ambiguous = await try_resolve_channel_session_permission_from_text(
            db=db,
            agent_id=seed.agent_id,
            user=user,
            user_text="/deny",
            session_id=str(seed.session_id),
            session_source="slack",
        )
        resolved = await try_resolve_channel_session_permission_from_text(
            db=db,
            agent_id=seed.agent_id,
            user=user,
            user_text=f"/deny {seed.permission_ids[0]}",
            session_id=str(seed.session_id),
            session_source="slack",
        )

        assert "多个待处理权限请求" in ambiguous
        assert str(seed.permission_ids[0]) in ambiguous
        assert "权限决定已记录" in resolved
        assert "仍在等待同一轮的其他权限请求" in resolved
