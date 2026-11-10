"""Document revocation follows derived memory through every recall surface."""

# Owner ruling 11-10: a memory derived from a revoked document may be saved in the revocation round but never reaches the model from the next round on.

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.memory.retriever import MemoryRetriever
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
from app.models.organization_identity import ContentInputReceipt
from app.models.session_v2 import SessionModelResult
from app.services import memory_service
from app.services.personal_knowledge_service import PersonalKnowledgeService
from tests.integration.source_withholding_helpers import assert_source_withheld
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import (
    memory_call,
    shared_memory,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

DOCUMENT_MARKER = "A4V_DOCUMENT_MARKER"
DERIVED_MARKER = "A4V_DERIVED_MARKER"


async def _recall(s, session_factory, surface, entry_ids):
    n = s.n
    if surface == "automatic":
        context = await memory_service.build_memory_context(
            n.agent,
            n.tenant,
            session_id=str(s.second_session),
            runtime_task_id=str(s.second_run),
            current_user_id=n.foreign,
            query="Recall the document release cadence",
            return_result=True,
        )
        return context.content, context
    tool = "load_memory" if surface == "load" else "search_memory"
    arguments = {"ids": entry_ids} if surface == "load" else {"query": "release cadence", "scope": "facts"}
    content, invocation = await memory_call(
        n, session_factory, tool, arguments, user=n.foreign, session=s.second_session, run=s.second_run,
        command="Recall the document release cadence", include_history=True,
    )
    return content, invocation


async def _model_request(s, session_factory, *, context=None):
    """Persist the real next-request boundary with canonical history and recall."""
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import (
        commit_sealed_model_round,
        prepare_model_request,
        seal_model_response,
    )
    from app.services.session_semantic_history import load_session_semantic_history

    n = s.n
    key = str(s.second_run)
    n.memory_rounds[key] += 1
    async with tenant_scoped_session(n.tenant, session_factory=session_factory) as db:
        history = await load_session_semantic_history(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=s.second_session,
        )
        messages = [LLMMessage(
            role=m.role, content=m.content, tool_calls=m.tool_calls, tool_call_id=m.tool_call_id, source_ref=m.id,
        ) for m in history.messages]
        if context is not None:
            messages.insert(0, LLMMessage(
                role="system", content=context.content,
                context_origin=ContextMessageOrigin.capture(
                    kind="memory", role="system", content=context.content,
                    sources=context.source_refs, sections=context.sections, unbound=context.source_unbound,
                ),
            ))
        request_id = await prepare_model_request(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=s.second_session, run_id=s.second_run,
            turn_id=f"turn-{s.second_run.hex}", round_index=n.memory_rounds[key], messages=messages, tools=[],
            provider="synthetic", model="local-reference",
        )
        await seal_model_response(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=s.second_session, run_id=s.second_run,
            turn_id=f"turn-{s.second_run.hex}", round_index=n.memory_rounds[key], provider_request_id=request_id,
            response={"content": "Continue with the authorized release evidence.", "tool_calls": [],
                "finish_reason": "stop", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )
        await commit_sealed_model_round(
            db, tenant_id=n.tenant, agent_id=n.agent, session_id=s.second_session, run_id=s.second_run,
            turn_id=f"turn-{s.second_run.hex}", round_index=n.memory_rounds[key], provider_request_id=request_id,
        )
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == request_id,
        ))
        await db.commit()
        return model


@pytest.mark.parametrize("surface", ["load", "search", "automatic"])
async def test_revoked_document_derived_memory_is_withheld_from_later_model_requests(
    shared_memory, owner_sessionmaker, app_user_sessionmaker, monkeypatch, surface,  # noqa: F811
):
    s = shared_memory
    n = s.n
    service = PersonalKnowledgeService(data_root=n.root.parent, extractor=None)
    async with owner_sessionmaker() as db:
        imported = await service.ingest_markdown(
            db, tenant_id=n.tenant, owner_user_id=n.user, title="Document release cadence",
            markdown=f"# Release cadence\n{DOCUMENT_MARKER}\n", source_kind="paste", sensitivity="PL3_sensitive",
        )
        document = await db.get(KnowledgeDocument, imported.document_id)
        grant = KnowledgeGrant(
            id=uuid4(), tenant_id=n.tenant, scope_id=n.user, resource_type="document",
            resource_id=document.id, document_id=document.id, grantee_type="user", grantee_id=n.foreign,
            created_by_user_id=n.user, permission="read", sensitivity_ceiling="PL3_sensitive",
        )
        runtime_grant = KnowledgeGrant(
            id=uuid4(), tenant_id=n.tenant, scope_id=n.user, resource_type="document",
            resource_id=document.id, document_id=document.id, grantee_type="agent", grantee_id=n.agent,
            requester_user_id=n.foreign, session_id=str(s.second_session), purpose="interactive_session",
            permission="read", sensitivity_ceiling="PL3_sensitive",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add_all([grant, runtime_grant])
        await db.commit()
        dependency = {
            "source_kind": "knowledge_document", "source_id": str(document.id),
            "source_version": document.canonical_md_sha256,
        }

    selected_bodies = []

    async def choose(_self, *, items, **_kwargs):
        selected_bodies.append("\n".join(item.content for item in items))
        return [item.metadata["selection_candidate_id"] for item in items], "Use the authorized release facts"

    async def reference_config(*_args, **_kwargs):
        return {"provider": "synthetic", "model": "local-reference"}

    monkeypatch.setattr(MemoryRetriever, "_select_with_model", choose)
    monkeypatch.setattr(memory_service, "_get_rerank_model_config", reference_config)
    loaded, _ = await memory_call(
        n, app_user_sessionmaker, "read_personal_kb", {"document_id": str(document.id)},
        user=n.foreign, session=s.second_session, run=s.second_run, command="Read the granted release document",
    )
    assert DOCUMENT_MARKER in loaded, loaded
    saved, invocation = await memory_call(
        n, app_user_sessionmaker, "save_memory",
        {"content": f"Remember the document release cadence {DERIVED_MARKER}", "category": "project"},
        user=n.foreign, session=s.second_session, run=s.second_run,
        command="Remember the release fact from the document for later work", include_history=True,
    )
    assert "Saved memory [project] (capabilities):" in saved, saved
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        receipt = await db.get(ContentInputReceipt, uuid5(invocation.id, "explicit-memory-source"))
        assert dependency in receipt.dependencies_json, receipt.dependencies_json
        entry_id = receipt.authority_json["entry_id"]
        memory_dependency = {
            "source_kind": "explicit_memory", "source_id": str(receipt.id),
            "source_version": receipt.content_sha256,
        }

    # Positive control: the chosen surface supplies its body to a later provider request.
    before, recalled = await _recall(s, app_user_sessionmaker, surface, [entry_id])
    assert DERIVED_MARKER in before, before
    positive = await _model_request(
        s, app_user_sessionmaker, context=recalled if surface == "automatic" else None,
    )
    snapshot = positive.model_request_snapshot_json
    assert DERIVED_MARKER in json.dumps(snapshot["wire_request"]), snapshot
    bindings = snapshot["input_bindings"]
    assert memory_dependency in bindings["dependencies"], bindings
    if surface == "automatic":
        assert selected_bodies and DERIVED_MARKER in selected_bodies[-1], selected_bodies
        assert memory_dependency in recalled.source_refs, recalled
    else:
        assert any(
            entry.get("source_ref") == f"session-tool-result:{recalled.id}"
            and entry["status"] == "bound" and memory_dependency in entry["dependencies"]
            for entry in bindings["messages"]
        ), bindings

    execute = n.service.execute

    async def revoke_before_save(_self, *args, **kwargs):
        # The DELETE /grants/{grant_id} product route calls this revocation service.
        # Revoke B's member grant after sealing this round, before its save effect;
        # the runtime grant remains live and cannot replace B's own document access.
        async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
            assert await service.delete_personal_grant(
                db, tenant_id=n.tenant, owner_user_id=n.user, current_user_id=n.user, grant_id=grant.id,
            )
            await db.commit()
        return await execute(*args, **kwargs)

    with monkeypatch.context() as revocation:
        revocation.setattr(type(n.service), "execute", revoke_before_save)
        same_round, revocation_invocation = await memory_call(
            n, app_user_sessionmaker, "save_memory",
            {"content": f"Remember the document release cadence again {DERIVED_MARKER}", "category": "project"},
            user=n.foreign, session=s.second_session, run=s.second_run,
            command="Save the release note in this round", include_history=True,
        )
    assert "Saved memory [project] (capabilities):" in same_round, same_round

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        same_round_receipt = await db.get(ContentInputReceipt, uuid5(revocation_invocation.id, "explicit-memory-source"))
        same_round_entry_id = same_round_receipt.authority_json["entry_id"]
    selected_bodies.clear()
    after, recalled = await _recall(s, app_user_sessionmaker, surface, [entry_id, same_round_entry_id])
    denied = await _model_request(
        s, app_user_sessionmaker, context=recalled if surface == "automatic" else None,
    )
    for marker in (DOCUMENT_MARKER, DERIVED_MARKER):
        assert_source_withheld([denied], content=marker, code="content_source_access_denied")
        assert marker not in after, after
        assert all(marker not in body for body in selected_bodies), selected_bodies
    if surface != "automatic":
        assert "content_source_access_denied" in after, after
    dependencies = denied.model_request_snapshot_json["input_bindings"]["dependencies"]
    assert all(ref["source_kind"] != "knowledge_document" for ref in dependencies), dependencies
