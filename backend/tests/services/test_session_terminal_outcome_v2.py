from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select


pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _seed(owner_sessionmaker):
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.tenant import Tenant
    from app.models.user import User

    tenant_id, user_id, agent_id, session_id, run_id = (uuid.uuid4() for _ in range(5))
    turn_id = f"turn-{run_id.hex}"
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Round Outcome Tenant", slug=f"round-outcome-{tenant_id.hex[:8]}"))
        db.add(
            User(
                id=user_id,
                username=f"round-{user_id.hex[:8]}",
                email=f"{user_id.hex[:8]}@round-outcome.test",
                password_hash="x",
                display_name="Round Outcome",
                tenant_id=tenant_id,
            )
        )
        await db.flush()
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="Round Outcome Agent", creator_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, agent_id=agent_id, tenant_id=tenant_id, user_id=user_id))
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
            prompt="produce exact result",
            metadata_json={"turn_id": turn_id},
        )
        db.add(task)
        await db.flush()
        db.add(
            RuntimeRootItem(
                tenant_id=tenant_id,
                root_runtime_task_id=run_id,
                runtime_task_id=run_id,
                source_agent_id=agent_id,
                root_user_id=user_id,
                root_session_id=str(session_id),
                intent_key=f"web-chat:{run_id}",
                work_type="web_chat_turn",
                target_ref=f"agent:{agent_id}",
                path_json=[f"agent:{agent_id}", f"session:{session_id}"],
                state="running",
                admission_disposition="admitted",
            )
        )
        await db.commit()
    return tenant_id, agent_id, session_id, run_id, turn_id


async def _prepare(
    db,
    *,
    tenant_id,
    agent_id,
    session_id,
    run_id,
    turn_id,
    round_index=1,
    continuation_index=0,
):
    from app.services.session_model_round import prepare_model_request

    wire_request = {
        "messages": [{"role": "user", "content": "exact input bytes"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "temperature": 0.25,
        "max_tokens": 8192,
        "reasoning": {"reasoning_effort": "high"},
    }
    request_id = await prepare_model_request(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        turn_id=turn_id,
        round_index=round_index,
        continuation_index=continuation_index,
        messages=wire_request["messages"],
        tools=wire_request["tools"],
        provider="openai",
        model="gpt-4.1",
        wire_request=wire_request,
        attempt_owner="round-outcome-test",
    )
    return request_id, wire_request


async def test_stream_batches_are_canonical_before_publish_and_share_result_identity(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionEventOutbox, SessionModelResult
    from app.services.session_model_round import append_model_stream_delta, seal_model_response

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        await db.commit()

        first = await append_model_stream_delta(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            content="literal failed is model text ",
            phase="unknown",
        )
        second = await append_model_stream_delta(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            content="and remains byte-faithful",
            phase="unknown",
        )
        await db.commit()

        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.result_id == result.id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert result is not None and result.state == "streaming"
        assert first.item_id == second.item_id == uuid.uuid5(result.id, "assistant-visible-text:0")
        assert [event.event_type for event in events] == [
            "result_commit.prepared",
            "assistant_text.delta",
            "result_commit.streaming",
            "assistant_text.delta",
        ]
        assert [event.ordinal for event in events if event.item_kind == "assistant_text"] == [0, 1]
        assert result.last_content_sequence == second.sequence
        assert await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == second.id)) is not None

        seal = await seal_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={
                "content": "literal failed is model text and remains byte-faithful",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {},
            },
        )
        assert seal["semantic_content"] == "literal failed is model text and remains byte-faithful"
        await db.commit()
        from app.services.session_event_contract import reduce_session_events, serialize_session_event

        content_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.result_id == result.id,
                        ChatTranscriptEvent.item_kind == "assistant_text",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        projected = reduce_session_events([serialize_session_event(event) for event in content_events])
        item = projected.items[str(first.item_id)]
        assert [event.lifecycle for event in content_events] == ["delta", "delta", "snapshot", "completed"]
        assert item.content == "literal failed is model text and remains byte-faithful"
        assert item.lifecycle == "completed"


async def test_private_reasoning_is_durable_but_redacted_from_user_projection(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_model_round import append_model_stream_delta, seal_model_response

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    private_bytes = "provider-private-reasoning-secret"
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        await append_model_stream_delta(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            content=private_bytes,
            phase="reasoning_private",
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
                "content": "public answer",
                "reasoning_content": private_bytes,
                "reasoning_signature": "provider-signature",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {},
            },
        )
        await db.commit()

        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(
                        ChatTranscriptEvent.result_id == result.id,
                        ChatTranscriptEvent.item_kind == "assistant_reasoning_private",
                    )
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert [event.lifecycle for event in events] == ["delta", "snapshot", "completed"]
        operator_projection = [serialize_session_event(event, audience="operator") for event in events]
        user_projection = [serialize_session_event(event, audience="direct_user") for event in events]
        assert private_bytes in [event["payload"].get("content") for event in operator_projection]
        assert all("content" not in event["payload"] for event in user_projection)
        assert all(event["item_id"] == str(events[0].item_id) for event in user_projection)
        assert all(event["visibility"]["audience"] == "private_provider" for event in user_projection)


@pytest.mark.parametrize(
    ("decision_outcome", "frame_status", "expected_lifecycle", "expected_outcome", "retryable"),
    [
        ("allow", "completed", "completed", "success", False),
        ("allow", "failed", "failed", "failed", True),
        ("deny", None, "denied", "denied", False),
        ("unavailable", None, "unavailable", "unavailable", True),
        ("require_approval", None, "waiting", None, True),
    ],
)
async def test_tool_invocation_has_pre_effect_fence_and_exactly_one_typed_result(
    owner_sessionmaker,
    decision_outcome,
    frame_status,
    expected_lifecycle,
    expected_outcome,
    retryable,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_tool_runtime import (
        complete_tool_invocation,
        mark_tool_effect_started,
        prepare_tool_invocation,
    )

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    tool_use_id = f"tool-{decision_outcome}-{run_id.hex}"
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            provider_tool_use_id=tool_use_id,
            tool_name="read_file",
            arguments={"path": "README.md"},
        )
        if decision_outcome != "require_approval":
            await mark_tool_effect_started(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                invocation_id=invocation.id,
            )
        evidence = {
            "schema": "hive.tool_execution_evidence.v1",
            "status": "settled",
            "retryable": retryable,
            "tool_decision": {
                "schema": "hive.tool_decision.v1",
                "decision_id": f"decision-{tool_use_id}",
                "outcome": decision_outcome,
                "input_hash": invocation.args_hash,
                "policy_snapshot_hash": "a" * 64,
                "capability_snapshot_hash": "b" * 64,
            },
            "execution_frame": ({"status": frame_status, "output_hash": "c" * 64} if frame_status else None),
        }
        result_events = await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content="literal failed text is not a machine outcome",
            execution_evidence=evidence,
        )
        await db.commit()

        row = await db.get(SessionToolInvocation, invocation.id)
        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.invocation_id == invocation.id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert row is not None
        if decision_outcome == "require_approval":
            assert row.effect_state == "prepared_not_started"
        else:
            assert row.effect_state in {"effect_committed", "failed"}
            assert [event.event_type for event in events[:2]] == ["tool_call.started", "tool_call.progress"]
        assert [event.lifecycle for event in events if event.item_kind == "tool_call"][-1] == expected_lifecycle
        tool_results = [event for event in events if event.item_kind == "tool_result"]
        if decision_outcome == "require_approval":
            assert tool_results == []
            assert any(event.event_type == "tool_permission.waiting" for event in events)
        else:
            assert len(tool_results) == 1
            assert tool_results[0].provider_tool_use_id == tool_use_id
            assert tool_results[0].metadata_json["v2_payload"]["outcome"] == expected_outcome

        replay = await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content="literal failed text is not a machine outcome",
            execution_evidence=evidence,
        )
        assert [event.id for event in replay] == [event.id for event in result_events]


async def test_approval_waiting_invocation_gets_one_real_result_only_after_user_allows(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.services.session_control_input import (
        accept_tool_permission_response,
        apply_permission_response_control_input,
    )
    from app.services.session_v2_persistence import resolve_session_mutation_authority
    from app.services.session_tool_runtime import (
        complete_tool_invocation,
        mark_tool_effect_started,
        prepare_tool_invocation,
    )

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    tool_use_id = f"tool-approval-{run_id.hex}"
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            provider_tool_use_id=tool_use_id,
            tool_name="write_file",
            arguments={"path": "workspace/a.md", "content": "ok"},
        )
        waiting_evidence = {
            "schema": "hive.tool_execution_evidence.v1",
            "status": "settled",
            "retryable": True,
            "tool_decision": {
                "schema": "hive.tool_decision.v1",
                "decision_id": f"decision-waiting-{tool_use_id}",
                "outcome": "require_approval",
                "input_hash": invocation.args_hash,
                "approval_id": str(uuid.uuid4()),
            },
            "execution_frame": None,
        }
        await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content="approval required",
            execution_evidence=waiting_evidence,
        )
        await db.commit()

        waiting_row = await db.get(SessionToolInvocation, invocation.id)
        assert waiting_row is not None
        assert waiting_row.effect_state == "prepared_not_started"
        assert waiting_row.result_event_id is None

        task = await db.get(RuntimeTask, run_id)
        assert task is not None
        user = await db.get(User, task.root_user_id)
        assert user is not None
        authority = await resolve_session_mutation_authority(
            db,
            user=user,
            agent_id=agent_id,
            session_id=session_id,
            action="resolve_tool_permission",
        )
        control_id = uuid.uuid4()
        await accept_tool_permission_response(
            db,
            authority=authority,
            control_id=control_id,
            idempotency_key=f"test-allow:{invocation.id}",
            invocation_id=invocation.id,
            permission_item_id=waiting_row.permission_item_id,
            permission_request_version=waiting_row.permission_request_version,
            permission_authority_snapshot_hash=waiting_row.permission_authority_snapshot_hash,
            expected_run_id=run_id,
            decision="allow_once",
            response_schema="hive.tool_permission_response.v1",
        )
        await apply_permission_response_control_input(
            db,
            authority=authority,
            control_id=control_id,
        )

        await mark_tool_effect_started(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            effective_arguments={"path": "workspace/a.md", "content": "ok"},
            permission_control_id=control_id,
        )
        allowed_evidence = {
            "schema": "hive.tool_execution_evidence.v1",
            "status": "settled",
            "retryable": False,
            "tool_decision": {
                "schema": "hive.tool_decision.v1",
                "decision_id": f"decision-allowed-{tool_use_id}",
                "outcome": "allow",
                "input_hash": invocation.args_hash,
            },
            "execution_frame": {"status": "completed", "output_hash": "d" * 64},
        }
        await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content="write complete",
            execution_evidence=allowed_evidence,
            effective_arguments={"path": "workspace/a.md", "content": "ok"},
        )
        await db.commit()

        events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent)
                    .where(ChatTranscriptEvent.invocation_id == invocation.id)
                    .order_by(ChatTranscriptEvent.sequence)
                )
            ).scalars()
        )
        assert len([event for event in events if event.item_kind == "tool_result"]) == 1
        assert [event.lifecycle for event in events if event.item_kind == "tool_permission"] == ["waiting", "completed"]


async def test_tool_invocation_without_typed_settlement_enters_reconciliation_without_fake_result(
    owner_sessionmaker,
) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.services.session_tool_runtime import complete_tool_invocation, prepare_tool_invocation

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        invocation = await prepare_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            provider_request_id=request_id,
            provider_tool_use_id=f"tool-ambiguous-{run_id.hex}",
            tool_name="write_file",
            arguments={"path": "x.txt", "content": "x"},
        )
        events = await complete_tool_invocation(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            invocation_id=invocation.id,
            provider_result_content="some prose",
            execution_evidence=None,
        )
        await db.commit()

        row = await db.get(SessionToolInvocation, invocation.id)
        persisted = list(
            (
                await db.execute(select(ChatTranscriptEvent).where(ChatTranscriptEvent.invocation_id == invocation.id))
            ).scalars()
        )
        assert row is not None and row.effect_state == "needs_reconciliation"
        assert [event.event_type for event in events] == ["tool_call.needs_reconciliation"]
        assert not any(event.item_kind == "tool_result" for event in persisted)


async def test_exact_wire_snapshot_and_four_obligation_kinds_coexist(owner_sessionmaker) -> None:
    from app.models.session_v2 import SessionModelResult, SessionRoundObligation
    from app.services.session_model_round import commit_model_response

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        request_id, wire_request = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={
                "content": "model bytes before tools",
                "tool_calls": [{"id": "provider-tool-1", "function": {"name": "read_file", "arguments": "{}"}}],
                "finish_reason": "tool_calls",
                "usage": {"input_tokens": 12, "output_tokens": 7},
            },
            pending_obligations=[
                {
                    "kind": "pending_input",
                    "source_generation": 3,
                    "source_ref": "session-input:steer-1",
                    "payload": {"input_ids": ["steer-1"], "mailbox_generation": 3},
                },
                {
                    "kind": "hook_retry",
                    "source_generation": 2,
                    "source_ref": "hook-run:stop-1",
                    "payload": {"hook_run_id": "stop-1", "hook_fence_ref": "hook:2"},
                },
                {
                    "kind": "compact_continue",
                    "source_generation": 4,
                    "source_ref": "compaction:4",
                    "payload": {"compaction_generation": 4, "compaction_ref": "compact:4"},
                },
            ],
        )
        await db.commit()

    async with owner_sessionmaker() as db:
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        assert result is not None and result.state == "round_committed"
        assert result.model_request_snapshot_json["wire_request"] == wire_request
        assert result.model_request_snapshot_json["temperature"] == 0.25
        assert result.model_request_snapshot_json["max_tokens"] == 8192
        assert result.seal_json["semantic_content"] == "model bytes before tools"
        assert result.seal_json["continuation"]["verdict"] == "continue"
        obligations = list(
            (
                await db.execute(
                    select(SessionRoundObligation)
                    .where(SessionRoundObligation.run_id == run_id)
                    .order_by(SessionRoundObligation.kind)
                )
            ).scalars()
        )
        assert [row.kind for row in obligations] == [
            "compact_continue",
            "hook_retry",
            "pending_input",
            "tool_followup",
        ]
        assert all(row.state == "pending" for row in obligations)


async def test_fence_drift_preserves_abandoned_plan_and_requires_new_generation(owner_sessionmaker) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionNextRoundPlan
    from app.services.session_model_round import commit_model_response
    from app.services.session_round_obligation import (
        AssemblyPlanDrift,
        commit_next_round_plan,
        dispatch_committed_plan,
    )

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={
                "content": "tool round",
                "tool_calls": [{"id": "tool-1", "function": {"name": "read_file", "arguments": "{}"}}],
                "finish_reason": "tool_calls",
                "usage": {},
            },
        )
        source = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        assert source is not None
        plan1 = await commit_next_round_plan(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            source_result_id=source.id,
            next_round_id=f"{run_id}:round:2",
        )
        plan1_id = plan1.id
        task = await db.get(RuntimeTask, run_id)
        metadata = dict(task.metadata_json or {})
        metadata["session_v2_generations"] = {"hook_generation": 9}
        task.metadata_json = metadata
        await db.flush()
        with pytest.raises(AssemblyPlanDrift):
            await dispatch_committed_plan(db, plan_id=plan1.id, claim_owner="worker-a")
        plan2 = await commit_next_round_plan(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=run_id,
            source_result_id=source.id,
            next_round_id=f"{run_id}:round:2",
        )
        assert plan2.id != plan1_id
        assert plan2.plan_generation == 2
        await dispatch_committed_plan(db, plan_id=plan2.id, claim_owner="worker-b")
        await db.commit()

    async with owner_sessionmaker() as db:
        plans = list(
            (
                await db.execute(
                    select(SessionNextRoundPlan)
                    .where(SessionNextRoundPlan.run_id == run_id)
                    .order_by(SessionNextRoundPlan.plan_generation)
                )
            ).scalars()
        )
        assert [(row.plan_generation, row.state) for row in plans] == [(1, "abandoned"), (2, "dispatched")]
        with pytest.raises(AssemblyPlanDrift):
            await dispatch_committed_plan(db, plan_id=plan1_id, claim_owner="stale-worker")


async def test_terminal_outcome_is_zero_copy_atomic_and_idempotent(owner_sessionmaker) -> None:
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.services.session_model_round import commit_model_response
    from app.services.session_terminal_outcome import commit_terminal_outcome, prepare_and_seal_run_outcome

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    exact_final = "模型原始终答字节\nsecond block line"
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        seal = await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={"content": exact_final, "tool_calls": [], "finish_reason": "stop", "usage": {}},
        )
        outcome = await prepare_and_seal_run_outcome(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            terminal_result_id=uuid.UUID(seal["result_id"]),
        )
        await commit_terminal_outcome(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            outcome_id=outcome.id,
        )
        await db.commit()

        # ACK loss/read-after-write reuses the same outcome and creates no
        # second final or terminal event.
        event_count = len(
            list(
                (
                    await db.execute(select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id))
                ).scalars()
            )
        )
        await commit_terminal_outcome(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            outcome_id=outcome.id,
        )
        await db.commit()
        assert (
            len(
                list(
                    (
                        await db.execute(
                            select(ChatTranscriptEvent).where(ChatTranscriptEvent.session_id == session_id)
                        )
                    ).scalars()
                )
            )
            == event_count
        )

    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        root_item = await db.scalar(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == run_id))
        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == run_id))
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        assert task is not None and task.status == "completed"
        assert root_item is not None and root_item.state == "completed"
        assert root_item.reason_code == "session_v2_terminal_outcome_committed"
        assert f"session-run-outcome://{outcome.id}" in root_item.result_refs_json
        assert task.result_summary == exact_final
        assert outcome is not None and outcome.state == "terminal_committed"
        assert result is not None
        final_events = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.session_id == session_id,
                        ChatTranscriptEvent.item_kind == "assistant_final",
                    )
                )
            ).scalars()
        )
        assert len(final_events) == 1
        payload = final_events[0].metadata_json["v2_payload"]
        assert payload["zero_copy"] is True
        assert "content" not in payload
        assert payload["source_blocks"] == outcome.seal_json["source_blocks"]


async def test_unresolved_obligation_blocks_terminal_outcome(owner_sessionmaker) -> None:
    from app.services.session_model_round import commit_model_response
    from app.services.session_terminal_outcome import TerminalOutcomeIneligible, prepare_and_seal_run_outcome

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
        )
        seal = await commit_model_response(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
            round_index=1,
            provider_request_id=request_id,
            response={"content": "not terminal", "tool_calls": [], "finish_reason": "stop", "usage": {}},
            pending_obligations=[
                {
                    "kind": "hook_retry",
                    "source_generation": 1,
                    "source_ref": "hook:blocked",
                    "payload": {"hook_run_id": "blocked"},
                }
            ],
        )
        with pytest.raises(TerminalOutcomeIneligible):
            await prepare_and_seal_run_outcome(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                terminal_result_id=uuid.UUID(seal["result_id"]),
            )


async def test_recovery_finishes_sealed_round_and_same_terminal_candidate_without_provider_replay(
    owner_sessionmaker,
) -> None:
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_root_item import RuntimeRootItem
    from app.models.session_v2 import SessionModelResult, SessionRunOutcome
    from app.services.session_model_round import seal_model_response
    from app.services.runtime_task_worker import (
        recover_session_model_rounds_once,
        recover_session_terminal_outcomes_once,
    )

    tenant_id, agent_id, session_id, run_id, turn_id = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        request_id, _ = await _prepare(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            turn_id=turn_id,
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
            response={"content": "recover exact bytes", "tool_calls": [], "finish_reason": "stop", "usage": {}},
        )
        await db.commit()

    recovery = await recover_session_model_rounds_once(
        worker_id="recovery-a",
        run_id=run_id,
        session_factory=owner_sessionmaker,
    )
    assert recovery == {"round_committed": 1, "needs_reconciliation": 0}
    async with owner_sessionmaker() as db:
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id))
        assert result is not None and result.state == "round_committed"

    recovered = await recover_session_terminal_outcomes_once(
        worker_id="recovery-b",
        run_id=run_id,
        session_factory=owner_sessionmaker,
    )
    assert recovered == {
        "sealed_terminal_committed": 0,
        "sealed_needs_reconciliation": 0,
        "candidate_terminal_committed": 1,
        "candidate_held": 0,
    }
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, run_id)
        root_item = await db.scalar(select(RuntimeRootItem).where(RuntimeRootItem.runtime_task_id == run_id))
        outcome = await db.scalar(select(SessionRunOutcome).where(SessionRunOutcome.run_id == run_id))
        result_count = len(
            list(
                (
                    await db.execute(
                        select(SessionModelResult).where(SessionModelResult.provider_request_id == request_id)
                    )
                ).scalars()
            )
        )
        assert task is not None and task.status == "completed"
        assert root_item is not None and root_item.state == "completed"
        assert outcome is not None and outcome.state == "terminal_committed"
        assert result_count == 1
