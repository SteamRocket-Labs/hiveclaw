from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed_waiting_permission(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.session_model_round import prepare_model_request
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation

    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Permission Control Tenant", slug=f"permission-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"permission-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@permission-control.test",
                password_hash="x",
                display_name="Permission Control",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Permission Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
        db.add(
            RuntimeTask(
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
                prompt="permission response",
                metadata_json={"turn_id": turn_id},
            )
        )
        await db.flush()
        wire_request = {
            "messages": [{"role": "user", "content": "perform governed write"}],
            "tools": [{"type": "function", "function": {"name": "write_file"}}],
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
            attempt_owner="permission-control-test",
        )
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            provider_tool_use_id=f"permission-tool-{run_id.hex}",
            tool_name="write_file",
            arguments={"path": "workspace/a.md", "content": "approved bytes"},
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
        await db.commit()
        await db.refresh(invocation)
        assert invocation.permission_item_id is not None
        assert invocation.permission_authority_snapshot_hash is not None
    return tenant_id, user_id, agent_id, session_id, run_id, invocation.id


async def _authority(db, *, user_id, agent_id, session_id):
    from app.models.user import User
    from app.services.session_v2_persistence import resolve_session_mutation_authority

    user = await db.get(User, user_id)
    assert user is not None
    return await resolve_session_mutation_authority(
        db,
        user=user,
        agent_id=agent_id,
        session_id=session_id,
        action="respond_tool_permission",
    )


async def _accept_exact_response(db, *, authority, invocation, control_id, key, decision):
    from app.services.session_control_input import accept_tool_permission_response

    return await accept_tool_permission_response(
        db,
        authority=authority,
        control_id=control_id,
        idempotency_key=key,
        invocation_id=invocation.id,
        permission_item_id=invocation.permission_item_id,
        permission_request_version=invocation.permission_request_version,
        permission_authority_snapshot_hash=invocation.permission_authority_snapshot_hash,
        expected_run_id=invocation.run_id,
        decision=decision,
        response_schema="hive.tool_permission_response.v1",
    )


async def test_allow_permission_response_is_bound_idempotent_and_keeps_effect_prepared(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionToolInvocation
    from app.services.session_control_input import apply_permission_response_control_input

    tenant_id, user_id, agent_id, session_id, run_id, invocation_id = await _seed_waiting_permission(owner_sessionmaker)
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation is not None
        accepted = await _accept_exact_response(
            db,
            authority=authority,
            invocation=invocation,
            control_id=control_id,
            key=f"permission-allow:{invocation_id}",
            decision="allow_once",
        )
        applied = await apply_permission_response_control_input(
            db,
            authority=authority,
            control_id=control_id,
        )
        await db.commit()

        replay_accept = await _accept_exact_response(
            db,
            authority=authority,
            invocation=invocation,
            control_id=control_id,
            key=f"permission-allow:{invocation_id}",
            decision="allow_once",
        )
        replay_apply = await apply_permission_response_control_input(
            db,
            authority=authority,
            control_id=control_id,
        )
        await db.commit()

        control = await db.get(SessionControlInput, control_id)
        command = await db.get(SessionCommand, accepted.command_id)
        await db.refresh(invocation)
        assert accepted.status == "accepted"
        assert applied.status == "applied"
        assert replay_accept.replayed is True and replay_accept.status == "applied"
        assert replay_apply.replayed is True and replay_apply.status == "applied"
        assert control is not None
        assert control.request_item_id == invocation.permission_item_id
        assert control.request_version == invocation.permission_request_version
        assert control.authority_snapshot_hash == invocation.permission_authority_snapshot_hash
        assert control.expected_run_id == run_id
        assert command is not None and command.status == "applied"
        assert invocation.permission_state == "approved"
        assert invocation.effect_state == "prepared_not_started"
        assert invocation.result_event_id is None
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.invocation_id == invocation_id,
                    ChatTranscriptEvent.item_kind == "tool_result",
                )
            )
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.command_id == accepted.command_id,
                    ChatTranscriptEvent.item_kind == "control_input",
                    ChatTranscriptEvent.lifecycle == "applied",
                )
            )
            == 1
        )


async def test_deny_permission_response_creates_exactly_one_typed_matching_result(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_control_input import apply_permission_response_control_input

    tenant_id, user_id, agent_id, session_id, _run_id, invocation_id = await _seed_waiting_permission(
        owner_sessionmaker
    )
    control_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation is not None
        accepted = await _accept_exact_response(
            db,
            authority=authority,
            invocation=invocation,
            control_id=control_id,
            key=f"permission-deny:{invocation_id}",
            decision="deny",
        )
        first = await apply_permission_response_control_input(
            db,
            authority=authority,
            control_id=control_id,
        )
        await db.commit()
        replay = await apply_permission_response_control_input(
            db,
            authority=authority,
            control_id=control_id,
        )
        await db.commit()

        await db.refresh(invocation)
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.invocation_id == invocation_id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        tool_results = [event for event in events if event.item_kind == "tool_result"]
        permission_events = [event for event in events if event.item_kind == "tool_permission"]
        assert first.status == replay.status == "applied"
        assert replay.replayed is True
        assert invocation.permission_state == "denied"
        assert invocation.effect_state == "prepared_not_started"
        assert invocation.result_event_id == tool_results[0].id
        assert len(tool_results) == 1
        assert tool_results[0].metadata_json["v2_payload"]["outcome"] == "denied"
        assert tool_results[0].metadata_json["v2_payload"]["retryable"] is False
        assert [event.lifecycle for event in permission_events] == ["waiting", "denied"]
        assert (
            await db.scalar(
                select(func.count())
                .select_from(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.command_id == accepted.command_id,
                    ChatTranscriptEvent.item_kind == "control_input",
                    ChatTranscriptEvent.lifecycle == "applied",
                )
            )
            == 1
        )


async def test_competing_permission_responses_have_one_authoritative_winner(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionControlInput, SessionToolInvocation
    from app.services.session_v2_persistence import IdempotencyConflict

    tenant_id, user_id, agent_id, session_id, _run_id, invocation_id = await _seed_waiting_permission(
        owner_sessionmaker
    )

    async def submit(*, decision: str, control_id: uuid.UUID):
        async with owner_sessionmaker() as db:
            authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
            invocation = await db.get(SessionToolInvocation, invocation_id)
            assert invocation is not None
            try:
                receipt = await _accept_exact_response(
                    db,
                    authority=authority,
                    invocation=invocation,
                    control_id=control_id,
                    key=f"permission-race:{decision}:{control_id}",
                    decision=decision,
                )
                await db.commit()
                return receipt
            except Exception:
                await db.rollback()
                raise

    outcomes = await asyncio.gather(
        submit(decision="allow_once", control_id=uuid.uuid4()),
        submit(decision="deny", control_id=uuid.uuid4()),
        return_exceptions=True,
    )
    assert len([outcome for outcome in outcomes if not isinstance(outcome, Exception)]) == 1
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, IdempotencyConflict)]
    assert len(conflicts) == 1

    async with owner_sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionControlInput)
                .where(
                    SessionControlInput.session_id == session_id,
                    SessionControlInput.kind == "permission_response",
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    ("stale_field", "expected_error"),
    [
        ("permission_item_id", "tool_permission_request_item_mismatch"),
        ("permission_request_version", "tool_permission_request_version_mismatch"),
        ("permission_authority_snapshot_hash", "tool_permission_authority_snapshot_mismatch"),
        ("expected_run_id", "tool_permission_expected_run_mismatch"),
    ],
)
async def test_permission_response_rejects_each_stale_optimistic_binding(
    owner_sessionmaker,
    stale_field,
    expected_error,
) -> None:
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_control_input import accept_tool_permission_response

    _tenant_id, user_id, agent_id, session_id, _run_id, invocation_id = await _seed_waiting_permission(
        owner_sessionmaker
    )
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation is not None and invocation.permission_item_id is not None
        arguments = {
            "permission_item_id": invocation.permission_item_id,
            "permission_request_version": invocation.permission_request_version,
            "permission_authority_snapshot_hash": invocation.permission_authority_snapshot_hash,
            "expected_run_id": invocation.run_id,
        }
        arguments[stale_field] = (
            invocation.permission_request_version + 1
            if stale_field == "permission_request_version"
            else "0" * 64
            if stale_field == "permission_authority_snapshot_hash"
            else uuid.uuid4()
        )
        with pytest.raises(ValueError, match=expected_error):
            await accept_tool_permission_response(
                db,
                authority=authority,
                control_id=uuid.uuid4(),
                idempotency_key=f"permission-stale:{stale_field}",
                invocation_id=invocation.id,
                decision="allow_once",
                response_schema="hive.tool_permission_response.v1",
                **arguments,
            )


async def test_permission_response_rejects_stale_binding_and_idempotency_payload_conflict(
    owner_sessionmaker,
) -> None:
    from app.models.session_v2 import SessionControlInput, SessionToolInvocation
    from app.services.session_control_input import accept_tool_permission_response
    from app.services.session_v2_persistence import IdempotencyConflict

    tenant_id, user_id, agent_id, session_id, _run_id, invocation_id = await _seed_waiting_permission(
        owner_sessionmaker
    )
    control_id = uuid.uuid4()
    key = f"permission-idempotency:{invocation_id}"
    async with owner_sessionmaker() as db:
        authority = await _authority(db, user_id=user_id, agent_id=agent_id, session_id=session_id)
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation is not None and invocation.permission_item_id is not None
        with pytest.raises(ValueError, match="tool_permission_request_version_mismatch"):
            await accept_tool_permission_response(
                db,
                authority=authority,
                control_id=uuid.uuid4(),
                idempotency_key="permission-stale",
                invocation_id=invocation.id,
                permission_item_id=invocation.permission_item_id,
                permission_request_version=invocation.permission_request_version + 1,
                permission_authority_snapshot_hash=invocation.permission_authority_snapshot_hash,
                expected_run_id=invocation.run_id,
                decision="allow_once",
                response_schema="hive.tool_permission_response.v1",
            )
        await db.rollback()
        invocation = await db.get(SessionToolInvocation, invocation_id)
        assert invocation is not None

        accepted = await _accept_exact_response(
            db,
            authority=authority,
            invocation=invocation,
            control_id=control_id,
            key=key,
            decision="allow_once",
        )
        await db.commit()
        with pytest.raises(IdempotencyConflict):
            await accept_tool_permission_response(
                db,
                authority=authority,
                control_id=control_id,
                idempotency_key=key,
                invocation_id=invocation.id,
                permission_item_id=invocation.permission_item_id,
                permission_request_version=invocation.permission_request_version,
                permission_authority_snapshot_hash=invocation.permission_authority_snapshot_hash,
                expected_run_id=invocation.run_id,
                decision="deny",
                response_schema="hive.tool_permission_response.v1",
            )
        await db.rollback()
        assert accepted.status == "accepted"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionControlInput)
                .where(
                    SessionControlInput.session_id == session_id,
                    SessionControlInput.kind == "permission_response",
                )
            )
            == 1
        )
