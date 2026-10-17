"""A receiving Agent needs its own live source grant before prompt injection."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tests.integration.test_m1g22_artifact_versions_postgres import delivered  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("source", ["original", "human_grant"])
async def test_actual_receiver_reads_originals_and_rechecks_scoped_upstream_grants(
    delivered, owner_sessionmaker, app_user_sessionmaker, monkeypatch, source,  # noqa: F811
):
    from app.agents.delegation_token import issue_delegation_token
    from app.core.execution_context import ExecutionPrincipal
    from app.database import pin_rls_tenant_context
    from app.kernel.contracts import ExecutionIdentityRef
    from app.models.agent import Agent
    from app.models.chat_session import ChatSession
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
    from app.models.organization_identity import ContentInputReceipt
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.runtime.invoker import AgentInvocationRequest
    from app.runtime.session import SessionContext
    from app.runtime.a2a_workflow import A2AOutputArtifact
    from app.services import agent_tools
    from app.services.a2a_workflow_artifacts import load_artifact_packet, load_delegated_artifact_context

    d = delivered
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", d.root.parent)
    receiver, session, run, publisher = (uuid4() for _ in range(4))
    token = issue_delegation_token(parent_agent_id=d.agent, child_agent_id=receiver)
    principal = ExecutionPrincipal(tenant_id=d.tenant, source_agent_id=receiver, requester_user_id=d.user.id,
        root_session_id=str(d.session), root_runtime_task_id=str(d.run), origin="a2a_delegation",
        delegation_chain=(f"agent:{d.agent}", f"agent:{receiver}"))
    async with owner_sessionmaker() as db:
        db.add(Agent(id=receiver, tenant_id=d.tenant, creator_id=d.user.id, name="Receiving employee"))
        db.add(User(id=publisher, tenant_id=d.tenant, username=f"source-{publisher.hex}",
            email=f"{publisher.hex}@fixture.test", password_hash="fixture", display_name="Source owner", role="member"))
        await db.flush()
        db.add(ChatSession(id=session, tenant_id=d.tenant, agent_id=receiver, user_id=d.user.id,
            source_channel="agent", session_kind="delegation_run", runtime_source="delegation"))
        db.add(RuntimeTask(id=run, tenant_id=d.tenant, task_type="delegation", status="running",
            parent_agent_id=d.agent, child_agent_id=receiver, root_user_id=d.user.id,
            parent_session_id=str(d.session), child_session_id=str(session), root_session_id=str(d.session),
            root_runtime_task_id=d.run, prompt="Use the referenced report", metadata_json={}))
        if source == "human_grant":
            path = d.root.parent / "private-source.md"
            path.write_text("PRIVATE_SOURCE_FOR_BROWSER_ONLY")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            doc = KnowledgeDocument(id=uuid4(), tenant_id=d.tenant, scope_type="person", scope_id=publisher,
                owner_user_id=publisher, source_kind="text", source_sha256=digest, title="Private source",
                status="ready", canonical_md_path=path.name, canonical_md_sha256=digest)
            db.add(doc)
            await db.flush()
            human_grant = KnowledgeGrant(tenant_id=d.tenant, scope_id=publisher, resource_type="document",
                resource_id=doc.id, document_id=doc.id, grantee_type="user", grantee_id=d.user.id, permission="read")
            db.add(human_grant)
            receipt = await db.get(ContentInputReceipt, UUID(d.first.snapshot_json["content_input_receipt_id"]))
            # Known-source consumer fixture; no normal model input list is prefilled.
            receipt.dependencies_json = [{"source_kind": "knowledge_document", "source_id": str(doc.id),
                "source_version": digest}]
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        packet = await load_artifact_packet(db, tenant_id=d.tenant, requester_user_id=d.user.id,
            run_id=d.run, node_id="producer", producer_agent_id=d.agent, producer_session_id=d.session,
            producer_runtime_task_id=d.run, output=A2AOutputArtifact(name="report", path="workspace/report.txt"),
            max_bytes=1000)
        assert packet["content"] == "AAAA"
    spec = {"artifact_ref": packet["artifact_ref"], "name": "report", "recipient_agent_id": str(receiver),
        "recipient_session_id": str(session), "max_bytes": 1000, "kind": "text"}
    messages = [{"role": "user", "content": "Use this reference", "a2a_artifact_inputs": [spec]}]
    invocation = AgentInvocationRequest(model=SimpleNamespace(), messages=[], agent_name="Receiver", role_description="",
        tenant_id=d.tenant, agent_id=receiver, user_id=d.user.id, delegation_token=token,
        execution_identity=ExecutionIdentityRef(identity_type="delegated_user", identity_id=d.user.id),
        session_context=SessionContext(session_id=str(session), metadata={"execution_principal": principal.to_evidence(),
            "runtime_task_id": run.hex}))
    rendered = await load_delegated_artifact_context(invocation, messages)
    if source == "human_grant":
        assert "AAAA" not in rendered and "artifact_source_access_denied" in rendered
        async with owner_sessionmaker() as db:
            grant = KnowledgeGrant(tenant_id=d.tenant, scope_id=publisher, resource_type="document", resource_id=doc.id,
                document_id=doc.id, grantee_type="agent", grantee_id=receiver, requester_user_id=d.user.id,
                purpose="a2a_delegation", session_id=str(session), delegation_id=token.delegation_id, permission="read",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
            db.add(grant)
            await db.commit()
            grant_id = grant.id
        rendered = await load_delegated_artifact_context(invocation, messages)
        assert "AAAA" in rendered, rendered
        async with owner_sessionmaker() as db:
            (await db.get(KnowledgeGrant, human_grant.id)).revoked_at = datetime.now(timezone.utc)
            await db.commit()
        # Native grants stand on their own; an unrelated browser grant is not required.
        rendered = await load_delegated_artifact_context(invocation, messages)
    assert "AAAA" in rendered and "unavailable" not in rendered, rendered
    from fastapi import HTTPException
    from app.core.resource_authority import authorize_resource_action, workspace_resource_id
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    reader = AgentRuntimePrincipal(receiver, d.user.id, session_id=str(session), runtime_task_id=run.hex,
        delegation_id=token.delegation_id, purpose="a2a_delegation")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        user = await db.get(User, d.user.id)
        for kind, resource in [("chat_artifact", d.first.id),
            ("workspace_file", workspace_resource_id(d.agent, "workspace/report.txt"))]:
            with pytest.raises(HTTPException) as denied:
                await authorize_resource_action(db, user, agent_id=d.agent, resource_kind=kind, resource_id=resource,
                    action="write", owner_user_id=d.user.id, runtime_principal=reader)
            assert denied.value.status_code == 403
        with pytest.raises(HTTPException) as denied:
            await authorize_resource_action(db, user, agent_id=d.agent, resource_kind="chat_artifact", resource_id=d.first.id,
                action="read", owner_user_id=d.user.id, runtime_principal=replace(reader, purpose="interactive_session", delegation_id=None))
        assert denied.value.status_code == 403
    if source == "original":
        invocation.delegation_token = replace(token, expires_at=0)
        expired = await load_delegated_artifact_context(invocation, messages)
        assert "AAAA" not in expired and "artifact_source_principal_unavailable" in expired
        invocation.delegation_token = token
    assert "content" not in messages[0]["a2a_artifact_inputs"][0]
    spec["recipient_session_id"] = str(uuid4())
    denied = await load_delegated_artifact_context(invocation, messages)
    assert "AAAA" not in denied and "artifact_input_contract_invalid" in denied
    spec["recipient_session_id"] = str(session)
    async with owner_sessionmaker() as db:
        if source == "human_grant":
            (await db.get(KnowledgeGrant, grant_id)).revoked_at = datetime.now(timezone.utc)
        else:
            (await db.get(ChatSession, session)).user_id = publisher
        await db.commit()
    revoked = await load_delegated_artifact_context(invocation, messages)
    assert "AAAA" not in revoked and "artifact_source_access_denied" in revoked
