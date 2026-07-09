from __future__ import annotations

import uuid

import pytest

from app.runtime.invoker import AgentInvocationRequest
from app.runtime.retrieval.kb_candidates import KnowledgeCandidateRecord
from app.runtime.session import SessionContext


class _Provider:
    async def search(self, *, query, scope, acl_context, limit):
        assert scope == "personal"
        return [
            KnowledgeCandidateRecord(
                scope="personal",
                item_id="segment-1",
                title="Retrieval notes",
                preview="Use source refs.",
                source_ref="kb://person/owner/documents/doc#segment=seg",
                score=0.8,
            )
        ]


@pytest.mark.asyncio
async def test_invoker_records_personal_kb_candidates_and_builds_hint() -> None:
    from app.runtime.invoker import _record_knowledge_activation_for_request

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_context = SessionContext(
        session_id="sess-1",
        metadata={"tenant_id": str(tenant_id), "owner_user_id": str(owner_id)},
    )
    request = AgentInvocationRequest(
        model=None,
        messages=[{"role": "user", "content": "Find my source refs notes"}],
        agent_name="Agent",
        role_description="Research assistant",
        agent_id=agent_id,
        user_id=owner_id,
        session_context=session_context,
        memory_session_id="sess-1",
    )

    hint = await _record_knowledge_activation_for_request(request, provider=_Provider())

    state = session_context.metadata["runtime_assembly_state"]
    assert state["activation_candidates"][0]["candidate_kind"] == "knowledge_base"
    assert hint is not None
    assert "## Personal Knowledge Hint" in hint
    assert "Retrieval notes" in hint
    assert "kb://person/owner/documents/doc#segment=seg" in hint


class _TwoDocProvider:
    async def search(self, *, query, scope, acl_context, limit):
        return [
            KnowledgeCandidateRecord(
                scope="personal",
                item_id="doc-strong",
                title="Strong lexical match",
                preview="Strong match preview.",
                source_ref="kb://person/owner/documents/strong#segment=s1",
                score=0.85,
            ),
            KnowledgeCandidateRecord(
                scope="personal",
                item_id="doc-contextual",
                title="Session context doc",
                preview="Previously consulted.",
                source_ref="kb://person/owner/documents/contextual#segment=s2",
                score=0.7,
            ),
        ]


@pytest.mark.asyncio
async def test_kb_ordering_consumes_working_set_activation(tmp_path, monkeypatch) -> None:
    """M8 consumption contract: the unified activation signal must actually
    change the KB ordering (recorded candidates AND the prompt hint), and the
    KB activations must join the session working set — compute-but-don't-use
    is the audited QKV failure mode and is forbidden."""
    from app.memory.session_working_set import advance_working_set, load_working_set, save_working_set
    from app.runtime import invoker as invoker_mod
    from app.runtime.invoker import _record_knowledge_activation_for_request

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = str(uuid.uuid4())

    monkeypatch.setattr(
        invoker_mod,
        "_activation_data_root",
        lambda: tmp_path,
    )

    contextual_ref = "kb://person/owner/documents/contextual#segment=s2"
    save_working_set(tmp_path, agent_id, session_id, advance_working_set(None, [contextual_ref]))

    session_context = SessionContext(
        session_id=session_id,
        metadata={"tenant_id": str(tenant_id), "owner_user_id": str(owner_id)},
    )
    request = AgentInvocationRequest(
        model=None,
        messages=[{"role": "user", "content": "notes about the project"}],
        agent_name="Agent",
        role_description="Research assistant",
        agent_id=agent_id,
        user_id=owner_id,
        session_context=session_context,
        memory_session_id=session_id,
    )

    hint = await _record_knowledge_activation_for_request(request, provider=_TwoDocProvider())

    assert hint is not None
    strong_pos = hint.index("Strong lexical match")
    contextual_pos = hint.index("Session context doc")
    assert contextual_pos < strong_pos, "working-set activation must reorder the hint, not just be recorded"

    recorded = session_context.metadata["runtime_assembly_state"]["activation_candidates"]
    assert recorded[0]["source_refs"] == [contextual_ref], "recorded order must match the consumed order"
    assert recorded[0]["metadata"]["context_boost"] > 0
    trace = recorded[0]["metadata"]["activation_score_trace"]
    assert trace["base_score"] == pytest.approx(0.7)
    assert trace["activation_rank_score"] > trace["base_score"]

    state = load_working_set(tmp_path, agent_id, session_id)
    refs = {item["ref"] for item in state.items}
    assert contextual_ref in refs
    assert "kb://person/owner/documents/strong#segment=s1" in refs, "KB activations must join W_t"
    assert state.turn_index == 1, "KB touch must not advance the turn counter"
