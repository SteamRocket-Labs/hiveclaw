import json
import uuid

import pytest
from sqlalchemy import func, select

from tests.services.test_session_semantic_history import (
    _bind_round_one, _seal_round, _seed, _submit_turn,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _settled_result(owner_sessionmaker, tool_name, content):
    from app.services.session_tool_runtime import (
        _sha256, complete_tool_invocation, mark_tool_effect_started, prepare_tool_invocation,
    )

    tenant, user, agent, session = await _seed(owner_sessionmaker)
    _, run, turn = await _submit_turn(owner_sessionmaker, user_id=user, agent_id=agent,
        session_id=session, content="marker")
    common = dict(tenant_id=tenant, agent_id=agent, session_id=session, run_id=run)
    await _bind_round_one(owner_sessionmaker, **common, turn_id=turn)
    async with owner_sessionmaker() as db:
        from app.models.session_v2 import SessionTurnInput
        from app.services.session_model_round import prepare_model_request

        input_id = await db.scalar(select(SessionTurnInput.id).where(SessionTurnInput.session_id == session))
        request = await prepare_model_request(db, **common, turn_id=turn, round_index=1,
            messages=[{"role": "user", "content": "marker", "source_ref": f"session-input:{input_id}"}],
            tools=None, provider="openai", model="gpt-test")
        arguments = {"path": f"memory/t0/sessions/{session}/source.py"} if tool_name == "read_file" else {"document_id": str(uuid.uuid4())}
        invocation = await prepare_tool_invocation(db, **common, provider_request_id=request,
            provider_tool_use_id="ordinary-call", tool_name=tool_name, arguments=arguments)
        await mark_tool_effect_started(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id)
        await complete_tool_invocation(db, tenant_id=tenant, agent_id=agent, session_id=session,
            invocation_id=invocation.id, provider_result_content=content, execution_evidence={
                "schema": "hive.tool_execution_evidence.v1", "status": "settled", "retryable": False,
                "pre_effect_fence_ref": "ordinary-result-test",
                "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "ordinary-result",
                    "outcome": "allow", "input_hash": invocation.args_hash,
                    "policy_snapshot_hash": "a" * 64, "capability_snapshot_hash": "b" * 64},
                "execution_frame": {"status": "completed", "output_hash": _sha256(content)},
            })
        await _seal_round(db, **common, turn_id=turn, round_index=1, provider_request_id=request,
            content="", tool_calls=[{"id": "ordinary-call", "name": tool_name, "arguments": arguments}])
        await db.commit()
        return tenant, user, agent, session, invocation.id


@pytest.mark.parametrize("check", ["dependencies", "receipt", "replay"])
async def test_ordinary_read_file_keeps_dependencies_receipt_and_replay(owner_sessionmaker, check):
    from app.models.chat_session import ChatSession
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionToolInvocation
    from app.models.user import User
    from app.services.content_input_receipts import _validate_session_dependency
    from app.services.session_model_inputs import _origin_dependencies, tool_result_source_hash
    from app.services.session_semantic_history import _event_payload, load_session_semantic_history

    content = 'print("ordinary Python source")\n' * 2300
    assert 65_536 < len(content.encode()) < 100_000
    tenant, user, agent, session, invocation_id = await _settled_result(owner_sessionmaker, "read_file", content)
    async with owner_sessionmaker() as db:
        event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.invocation_id == invocation_id,
            ChatTranscriptEvent.event_type == "tool_result.completed"))
        if check == "dependencies":
            deps, complete = await _origin_dependencies(db, tenant_id=tenant, agent_id=agent,
                ref=f"session-tool-result:{invocation_id}")
            assert complete and deps
        elif check == "receipt":
            await _validate_session_dependency(db, tenant,
                {"source_id": str(invocation_id), "source_version": tool_result_source_hash(_event_payload(event))},
                row=(await db.get(SessionToolInvocation, invocation_id), await db.get(ChatSession, session), event),
                model=SessionToolInvocation, user=await db.get(User, user), runtime_request=None)
        assert event.content == content
        for expression in (ChatTranscriptEvent.content.label("body"), func.coalesce(ChatTranscriptEvent.content, "")):
            assert await db.scalar(select(expression).where(ChatTranscriptEvent.id == event.id)) == content
        subquery = select(ChatTranscriptEvent.content).where(ChatTranscriptEvent.id == event.id).subquery()
        assert await db.scalar(select(subquery.c.content)) == content
        history = await load_session_semantic_history(db, tenant_id=tenant, agent_id=agent, session_id=session)
        assert any(message.get("role") == "tool" and message.get("content") == content for message in history.messages)


@pytest.mark.parametrize("writer", ["legacy", "v2"])
async def test_large_company_knowledge_keeps_pl3_sensitivity(owner_sessionmaker, writer):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.chat_transcript import append_session_event

    publication, document, segment = (str(uuid.uuid4()) for _ in range(3))
    content = json.dumps({"status": "ok", "document_id": document, "publication_id": publication,
        "source_version": "a" * 64, "segments": [{"segment_id": segment, "content": "sensitive " * 8000,
            "source_ref": f"company-publication://{publication}", "sensitivity": "PL3_sensitive"}],
        "sensitivity": "PL3_sensitive"})
    if writer == "v2":
        tenant, _, agent, session, invocation = await _settled_result(owner_sessionmaker, "read_company_kb", content)
        async with owner_sessionmaker() as db:
            event = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.invocation_id == invocation,
                ChatTranscriptEvent.event_type == "tool_result.completed"))
            metadata = event.metadata_json
    else:
        tenant, _, agent, session = await _seed(owner_sessionmaker)
        async with owner_sessionmaker() as db:
            event = await append_session_event(db=db, tenant_id=tenant, agent_id=agent, session_id=session,
                actor_type="tool", event_type="tool_result", bridge_to_t0=False, materialize_chat_message=False,
                content=json.dumps({"name": "read_company_kb", "result": content}))
            metadata = event.transcript_event.metadata_json
    assert metadata["content_sensitivity"] == "PL3_sensitive"
    assert metadata["semantic_memory_eligible"] is False
