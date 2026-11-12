"""Document revocation follows derived memory through every recall surface."""

# Owner ruling 11-10: a memory derived from a revoked document may be saved in the revocation round but never reaches the model from the next round on.

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
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

    loaded, read_invocation = await memory_call(
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
    # personal KB bodies are not replayed from history; later rounds need a live read
    read_binding = next(
        entry for entry in bindings["messages"]
        if entry.get("source_ref") == f"session-tool-result:{read_invocation.id}"
    )
    # message_index addresses the final wire messages, including system memory context.
    replayed_read = snapshot["wire_request"]["messages"][read_binding["message_index"]]
    replayed_body = json.loads(replayed_read["content"])
    assert replayed_body["content_omitted"] is True, replayed_body
    assert replayed_body["references"], replayed_body
    assert DOCUMENT_MARKER not in json.dumps(replayed_read), replayed_read
    if surface == "automatic":
        # The resident index preview carries the derived fact; its receipt
        # binds the context section so the model gate can recheck it.
        assert DERIVED_MARKER in before, before
        assert memory_dependency in recalled.source_refs, recalled
    else:
        assert any(
            entry.get("source_ref") == f"session-tool-result:{recalled.id}"
            and entry["status"] == "bound" and memory_dependency in entry["dependencies"]
            for entry in bindings["messages"]
        ), bindings

    # The run loses access only when every grant that authorises it is revoked;
    # an owner's agent grant bound to the requester authorises the run by itself
    # (personal_knowledge_access.py).
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        for grant_id in (grant.id, runtime_grant.id):
            assert await service.delete_personal_grant(
                db, tenant_id=n.tenant, owner_user_id=n.user, current_user_id=n.user, grant_id=grant_id,
            )
        await db.commit()

    direct_read, _ = await memory_call(
        n, app_user_sessionmaker, "read_personal_kb", {"document_id": str(document.id)},
        user=n.foreign, session=s.second_session, run=s.second_run, command="Read the revoked release document",
    )
    assert DOCUMENT_MARKER not in direct_read, direct_read

    after, recalled = await _recall(s, app_user_sessionmaker, surface, [entry_id])
    denied = await _model_request(
        s, app_user_sessionmaker, context=recalled if surface == "automatic" else None,
    )
    denied_snapshot = denied.model_request_snapshot_json
    assert_source_withheld([denied], content=DERIVED_MARKER, code="content_source_access_denied")
    # Document absence alone is not withholding proof: its history body was already omitted.
    assert DOCUMENT_MARKER not in json.dumps(denied_snapshot["wire_request"]), denied_snapshot
    for marker in (DOCUMENT_MARKER, DERIVED_MARKER):
        assert marker not in after, after
    if surface != "automatic":
        assert "content_source_access_denied" in after, after
    # A withheld source stays in provenance (A4d round-5 ruling); whatever stays
    # bound to the revoked document must carry none of its content.
    document_bindings = [
        entry for entry in denied_snapshot["input_bindings"]["messages"]
        if dependency in entry["dependencies"]
    ]
    assert document_bindings, denied_snapshot["input_bindings"]
    for binding in document_bindings:
        if binding["status"] == "bound":
            wire_message = denied_snapshot["wire_request"]["messages"][binding["message_index"]]
            for marker in (DOCUMENT_MARKER, DERIVED_MARKER):
                assert marker not in json.dumps(wire_message), (binding, wire_message)
