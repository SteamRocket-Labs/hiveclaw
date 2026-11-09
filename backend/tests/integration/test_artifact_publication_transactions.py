"""File-level failures and knowledge audit locks must not strand publication."""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.database import tenant_scoped_session
from tests.services.test_session_tool_artifact_settlement import _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def publish_company_source(factory, *, tenant, user, root):
    from app.models.user import User
    from app.models.security_audit import ResourcePermission
    from app.services.company_knowledge_analysis import CompanyKnowledgeAnalysisReview
    from app.services.company_knowledge_service import (
        CompanyKnowledgeService,
        CompanyKnowledgeReviewRequest,
        CompanyEvidenceIngestRequest,
    )
    from tests.integration.test_company_knowledge_closed_loop import _contract, _principal
    from tests.integration.test_m4a_knowledge_projection_postgres import submitted_proposal

    async with factory() as db:
        actor = await db.get(User, user)
        role = actor.role
        actor.role = "org_admin"
        await db.commit()
    service = CompanyKnowledgeService(data_root=root)
    principal = _principal(tenant_id=tenant, user_id=user)
    async with factory() as db:
        contract = await service.register_source_contract(
            db,
            principal=principal,
            contract_input=_contract(),
            idempotency_key=f"contract-{uuid.uuid4()}",
            trace_id="source-contract",
        )
        now = datetime.now(timezone.utc)
        job = await service.queue_evidence_import(
            db,
            principal=principal,
            request=CompanyEvidenceIngestRequest(
                source_contract_id=contract.id,
                source_contract_version=contract.version,
                evidence_kind="document",
                source_item_id="sales",
                source_revision="one",
                title="Sales source",
                markdown="# COMPANY_SOURCE_MARKER\nApproved company sales source\n",
                typed_payload=None,
                external_artifact_ref=None,
                schema_ref="schema://company-document/v1",
                source_acl_snapshot={"all_tenant_members": True},
                proposed_namespace="company/policies",
                proposed_sensitivity="PL2_pii",
                occurred_at=None,
                effective_from=now,
                effective_until=None,
                observed_at=now,
                cursor={},
                sequence=None,
                coverage_ledger={"complete": True, "total_units": 1, "covered_units": 1, "missing_units": []},
                purpose="Prepare a knowledge-derived sales report",
                idempotency_key=f"source-{uuid.uuid4()}",
                trace_id="source-import",
            ),
        )
        await db.commit()
    processed = await service.process_import_job(tenant_id=tenant, job_id=job.id, session_factory=factory)
    assert processed.status == "completed", processed.last_error
    async with factory() as db:
        proposal = await submitted_proposal(db, (tenant, user, None, processed, service, principal, root))
        await CompanyKnowledgeAnalysisReview(data_root=root).override(
            db,
            principal=principal,
            proposal=proposal,
            reason="The complete fixture source was reviewed",
            trace_id="source-analysis",
        )
        reviewed = await service.record_review(
            db,
            principal=principal,
            proposal_id=proposal.id,
            request=CompanyKnowledgeReviewRequest(
                decision="approve",
                reviewer_role="org_admin",
                reason="Fixture approval",
                evidence_refs=(f"company-evidence://{processed.evidence_id}",),
                policy_snapshot={},
            ),
            expected_state_version=proposal.state_version,
            trace_id="source-review",
        )
        publication = await service.publish_proposal(
            db,
            principal=principal,
            proposal_id=proposal.id,
            expected_state_version=reviewed.state_version,
            valid_from=datetime.now(timezone.utc),
            valid_until=None,
            trace_id="source-publish",
        )
        (await db.get(User, user)).role = role
        db.add(
            ResourcePermission(
                tenant_id=tenant,
                principal_type="user",
                principal_id=user,
                resource_type="company_knowledge_scope",
                resource_id=tenant,
                actions=["read", "cite"],
                effect="allow",
                conditions={},
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
            )
        )
        await db.commit()
        return publication.id, publication.content_hash


async def stage_report(db, *, tenant, agent, user, session, root, path, dependencies=()):
    from app.services.content_input_receipts import record_workspace_receipt
    from app.services.workspace_resource_authority import register_workspace_path

    content = "# Report\nCOMPANY_SOURCE_MARKER\n"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    await register_workspace_path(
        db,
        tenant_id=tenant,
        agent_id=agent,
        path=path,
        owner_user_id=user,
        root_session_id=session,
        source="fixture",
        content_hash=digest,
    )
    return record_workspace_receipt(db, tenant, agent, path, digest, None, additional_dependencies=dependencies)


async def publish_report(db, n, index, *, paths=None):
    from app.models.audit import ChatMessage
    from app.services.chat_artifact_delivery import create_chat_artifacts_for_message

    anchor = ChatMessage(
        id=uuid.uuid4(),
        tenant_id=n.tenant,
        agent_id=n.agent,
        user_id=n.users[index],
        conversation_id=str(n.sessions[index]),
        role="assistant",
        content="Prepared reports",
    )
    db.add(anchor)
    await db.flush()
    errors = []
    parts = await create_chat_artifacts_for_message(
        db=db,
        tenant_id=n.tenant,
        agent_id=n.agent,
        session_id=n.sessions[index],
        message_id=anchor.id,
        runtime_task_id=None,
        paths=paths or [n.paths[index]],
        workspace_root=n.root,
        publication_errors=errors,
    )
    return parts, errors


async def test_failed_authorization_statement_preserves_other_file_card(owner_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.core import permissions
    from app.models.agent import Agent
    from app.models.chat_artifact import ChatArtifact
    from app.models.user import User

    tenant, user, agent, session = await _seed(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    root = tmp_path / str(agent)
    foreign_user, foreign_agent = uuid.uuid4(), uuid.uuid4()
    paths = ["workspace/denied.md", "workspace/ready.md"]
    async with owner_sessionmaker() as db:
        (await db.get(User, user)).role = "member"
        db.add(
            User(
                id=foreign_user,
                tenant_id=tenant,
                username=foreign_user.hex,
                email=f"{foreign_user}@fixture.test",
                password_hash="fixture",
                display_name="Other owner",
                role="member",
            )
        )
        await db.flush()
        db.add(Agent(id=foreign_agent, tenant_id=tenant, creator_id=foreign_user, name="Foreign source"))
        await db.flush()
        foreign = await stage_report(
            db,
            tenant=tenant,
            agent=foreign_agent,
            user=foreign_user,
            session=None,
            root=tmp_path / str(foreign_agent),
            path="workspace/source.md",
        )
        dependency = {
            "source_kind": "workspace_artifact",
            "source_id": str(foreign.id),
            "source_version": foreign.content_sha256,
        }
        for path in paths:
            await stage_report(
                db,
                tenant=tenant,
                agent=agent,
                user=user,
                session=session,
                root=root,
                path=path,
                dependencies=[dependency] if path == paths[0] else [],
            )
        await db.commit()
    original = permissions.check_permission
    injected = False

    async def fail_one_statement(db, **kwargs):
        nonlocal injected
        if kwargs.get("resource_id") == foreign_agent and not injected:
            injected = True
            await db.execute(text("SELECT 1 / 0"))
        return await original(db, **kwargs)

    monkeypatch.setattr(permissions, "check_permission", fail_one_statement)
    n = SimpleNamespace(tenant=tenant, agent=agent, users=[user], sessions=[session], paths=paths, root=root)
    async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as db:
        parts, errors = await publish_report(db, n, 0, paths=paths)
        assert injected
        assert [part["path"] for part in parts] == [paths[1]]
        assert [(error["path"], error["status"]) for error in errors] == [(paths[0], "denied")]
        assert await db.scalar(text("SELECT 42")) == 42
        await db.commit()
    async with owner_sessionmaker() as db:
        assert list(await db.scalars(select(ChatArtifact.path).where(ChatArtifact.agent_id == agent))) == [paths[1]]


@pytest.fixture
async def knowledge_reports(owner_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.models.agent import AgentPermission
    from app.models.chat_session import ChatSession
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
    from tests.integration.test_company_knowledge_direct_import import _member_principal

    tenant, user, agent, session = await _seed(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    publication, version = await publish_company_source(owner_sessionmaker, tenant=tenant, user=user, root=tmp_path)
    second_user, second_session = uuid.uuid4(), uuid.uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(User, user)).role = "member"
        db.add(
            User(
                id=second_user,
                tenant_id=tenant,
                username=second_user.hex,
                email=f"{second_user}@fixture.test",
                password_hash="fixture",
                display_name="Second member",
                role="member",
            )
        )
        await db.flush()
        db.add(ChatSession(id=second_session, tenant_id=tenant, agent_id=agent, user_id=second_user))
        db.add(
            AgentPermission(tenant_id=tenant, agent_id=agent, scope_type="company", scope_id=tenant, access_level="use")
        )
        db.add(
            ResourcePermission(
                tenant_id=tenant,
                principal_type="user",
                principal_id=second_user,
                resource_type="company_knowledge_scope",
                resource_id=tenant,
                actions=["read", "cite"],
                effect="allow",
                conditions={},
                sensitivity_ceiling="PL3_sensitive",
                purposes=["interactive_session"],
            )
        )
        await db.commit()
    n = SimpleNamespace(
        tenant=tenant,
        agent=agent,
        users=[user, second_user],
        sessions=[session, second_session],
        root=tmp_path / str(agent),
        paths=["workspace/first.md", "workspace/second.md"],
        publication=publication,
    )
    for index, reader in enumerate(n.users):
        async with tenant_scoped_session(tenant, session_factory=owner_sessionmaker) as db:
            result = await CompanyKnowledgeGateway().read(
                db,
                principal=_member_principal(tenant_id=tenant, user_id=reader),
                request=CompanyKnowledgeReadRequest(
                    document_id=None,
                    publication_id=publication,
                    segment_ids=(),
                    max_chars=None,
                    trace_id=f"member-read-{index}",
                ),
            )
            assert result.status == "ok", result.as_dict()
            assert "COMPANY_SOURCE_MARKER" in result.as_json()
            await stage_report(
                db,
                tenant=tenant,
                agent=agent,
                user=reader,
                session=n.sessions[index],
                root=n.root,
                path=n.paths[index],
                dependencies=[
                    {
                        "source_kind": "company_knowledge_publication",
                        "source_id": str(publication),
                        "source_version": version,
                    }
                ],
            )
            await db.commit()
    return n


async def test_same_agent_knowledge_publications_do_not_deadlock(knowledge_reports, owner_sessionmaker, tmp_path):
    from app.models.chat_artifact import ChatArtifact
    from app.models.audit import ChatMessage
    from app.models.company_knowledge import CompanyKnowledgeEvent
    from app.services.chat_artifact_delivery import _lock_artifact_publication, create_chat_artifacts_for_message
    from app.services.chat_transcript import lock_transcript_session
    from app.services.company_knowledge_evidence import verify_company_knowledge_event_chain

    n = knowledge_reports
    async with owner_sessionmaker() as db:
        before = list(
            await db.scalars(select(CompanyKnowledgeEvent).where(CompanyKnowledgeEvent.tenant_id == n.tenant))
        )
        previous_sequence = max(row.stream_sequence for row in before)
    first_locked, second_inserted = asyncio.Event(), asyncio.Event()
    pids = {}

    async def publish(index):
        async with tenant_scoped_session(n.tenant, session_factory=owner_sessionmaker) as db:
            pids[index] = await db.scalar(text("SELECT pg_backend_pid()"))
            await lock_transcript_session(db, session_id=n.sessions[index])
            if index:
                await first_locked.wait()
            anchor = ChatMessage(
                id=uuid.uuid4(),
                tenant_id=n.tenant,
                agent_id=n.agent,
                user_id=n.users[index],
                conversation_id=str(n.sessions[index]),
                role="assistant",
                content="Knowledge report",
            )
            db.add(anchor)
            await db.flush()
            if index == 0:
                await _lock_artifact_publication(db, n.agent)
                first_locked.set()
                await second_inserted.wait()
            else:
                second_inserted.set()
            errors = []
            parts = await create_chat_artifacts_for_message(
                db=db,
                tenant_id=n.tenant,
                agent_id=n.agent,
                session_id=n.sessions[index],
                message_id=anchor.id,
                runtime_task_id=None,
                paths=[n.paths[index]],
                workspace_root=n.root,
                publication_errors=errors,
            )
            assert not errors and [part["path"] for part in parts] == [n.paths[index]], errors
            await db.commit()
            return parts

    async def observe():
        await second_inserted.wait()
        await asyncio.sleep(0.15)
        async with owner_sessionmaker() as db:
            rows = (
                (
                    await db.execute(
                        text("""SELECT pid,state,wait_event,query,pg_blocking_pids(pid) AS blockers
                FROM pg_stat_activity WHERE pid IN (:first,:second) ORDER BY pid"""),
                        {"first": pids[0], "second": pids[1]},
                    )
                )
                .mappings()
                .all()
            )
            locks = (
                (
                    await db.execute(
                        text("""SELECT pid,locktype,mode,granted,relation::regclass::text AS relation,
                transactionid::text FROM pg_locks WHERE pid IN (:first,:second)
                AND (NOT granted OR locktype IN ('tuple','transactionid','advisory')) ORDER BY pid,locktype"""),
                        {"first": pids[0], "second": pids[1]},
                    )
                )
                .mappings()
                .all()
            )
        result = {"activity": [dict(row) for row in rows], "locks": [dict(row) for row in locks]}
        (tmp_path / "knowledge-locks.json").write_text(json.dumps(result, default=str, indent=2))
        print("R6B_KNOWLEDGE_LOCKS", json.dumps(result, default=str), flush=True)

    observer = asyncio.create_task(observe())
    results = await asyncio.wait_for(asyncio.gather(publish(0), publish(1), return_exceptions=True), timeout=8)
    await observer
    print(
        "R6B_KNOWLEDGE_RESULTS",
        [str(result) if isinstance(result, BaseException) else "completed" for result in results],
    )
    assert all(not isinstance(result, BaseException) for result in results), [str(result) for result in results]
    async with owner_sessionmaker() as db:
        assert set(await db.scalars(select(ChatArtifact.path).where(ChatArtifact.agent_id == n.agent))) == set(n.paths)
        events = list(
            await db.scalars(
                select(CompanyKnowledgeEvent)
                .where(CompanyKnowledgeEvent.tenant_id == n.tenant)
                .order_by(CompanyKnowledgeEvent.stream_sequence)
            )
        )
        assert verify_company_knowledge_event_chain(events)["valid"]
        assert {
            row.accountable_user_id
            for row in events
            if row.stream_sequence > previous_sequence and row.resource_id == n.publication
        } == set(n.users)


@pytest.mark.parametrize("stream", ["company", "retained_original"])
async def test_audit_writers_with_tenant_fk_locks_keep_one_chain(knowledge_reports, owner_sessionmaker, stream):
    from app.models.audit import ChatMessage
    from app.models.company_knowledge import CompanyKnowledgeEvent
    from app.models.security_audit import SecurityAuditEvent
    from app.models.user import User
    from app.services.company_knowledge_gateway import CompanyKnowledgeGateway, CompanyKnowledgeReadRequest
    from app.services.company_knowledge_evidence import verify_company_knowledge_event_chain
    from app.services.original_source_lifecycle import audit_retained_original_inspection
    from tests.integration.test_company_knowledge_direct_import import _member_principal

    n = knowledge_reports
    async with owner_sessionmaker() as db:
        company_before = list(
            await db.scalars(select(CompanyKnowledgeEvent).where(CompanyKnowledgeEvent.tenant_id == n.tenant))
        )
        if stream == "retained_original":
            for user in n.users:
                (await db.get(User, user)).role = "org_admin"
            await db.commit()
        previous = await db.scalar(
            select(SecurityAuditEvent)
            .where(SecurityAuditEvent.tenant_id == n.tenant)
            .order_by(SecurityAuditEvent.sequence_num.desc())
            .limit(1)
        )
        previous_sequence, previous_hash = (previous.sequence_num, previous.event_hash) if previous else (0, "genesis")
    inserted = 0
    both_inserted = asyncio.Event()

    async def writer(index):
        nonlocal inserted
        async with tenant_scoped_session(n.tenant, session_factory=owner_sessionmaker) as db:
            db.add(
                ChatMessage(
                    tenant_id=n.tenant,
                    agent_id=n.agent,
                    user_id=n.users[index],
                    conversation_id=str(n.sessions[index]),
                    role="assistant",
                    content="Audit concurrency anchor",
                )
            )
            await db.flush()
            inserted += 1
            if inserted == 2:
                both_inserted.set()
            await both_inserted.wait()
            if stream == "company":
                result = await CompanyKnowledgeGateway().read(
                    db,
                    principal=_member_principal(tenant_id=n.tenant, user_id=n.users[index]),
                    request=CompanyKnowledgeReadRequest(
                        document_id=None,
                        publication_id=n.publication,
                        segment_ids=(),
                        max_chars=None,
                        trace_id=f"concurrent-read-{index}",
                    ),
                )
                assert result.status == "ok", result.as_dict()
            else:
                ref = SimpleNamespace(
                    id=uuid.uuid4(),
                    tenant_id=n.tenant,
                    metadata_json={
                        "owner_user_id": str(n.users[index]),
                        "withdrawal": {
                            "schema": "hive.original_withdrawal.v1",
                            "actor_user_id": str(n.users[index]),
                            "withdrawn_at": datetime.now(timezone.utc).isoformat(),
                        },
                    },
                )
                await audit_retained_original_inspection(
                    db, user=await db.get(User, n.users[index]), ref=ref, runtime_principal=None
                )
            await db.commit()

    results = await asyncio.wait_for(asyncio.gather(writer(0), writer(1), return_exceptions=True), timeout=8)
    assert all(not isinstance(result, BaseException) for result in results), [str(result) for result in results]
    async with owner_sessionmaker() as db:
        if stream == "company":
            events = list(
                await db.scalars(
                    select(CompanyKnowledgeEvent)
                    .where(CompanyKnowledgeEvent.tenant_id == n.tenant)
                    .order_by(CompanyKnowledgeEvent.stream_sequence)
                )
            )
            assert verify_company_knowledge_event_chain(events)["valid"]
            new_events = [
                row for row in events if row.stream_sequence > max(item.stream_sequence for item in company_before)
            ]
            assert {row.accountable_user_id for row in new_events} == set(n.users)
        else:
            events = list(
                await db.scalars(
                    select(SecurityAuditEvent)
                    .where(
                        SecurityAuditEvent.tenant_id == n.tenant, SecurityAuditEvent.sequence_num > previous_sequence
                    )
                    .order_by(SecurityAuditEvent.sequence_num)
                )
            )
            assert len(events) == 2
            for item in events:
                assert item.event_type == "storage.original_retained_inspection_authorized"
                assert item.prev_hash == previous_hash
                previous_hash = item.event_hash


async def test_knowledge_derived_web_report_completes(owner_sessionmaker, monkeypatch, tmp_path):
    from tests.integration.test_workspace_artifact_transaction import (
        test_web_csv_report_delivers_without_blocking_its_settlement,
    )

    await test_web_csv_report_delivers_without_blocking_its_settlement(
        owner_sessionmaker, monkeypatch, tmp_path, retained_reports=0, company_knowledge=True
    )


async def test_connection_observer_rejects_second_validation_connection(owner_sessionmaker, monkeypatch, tmp_path):
    from app.services import content_input_receipts
    from tests.integration.test_workspace_artifact_transaction import (
        test_web_csv_report_delivers_without_blocking_its_settlement,
    )

    original = content_input_receipts.validate_published_workspace_receipt

    async def second_connection(_db, tenant, user, receipt, digest):
        async with tenant_scoped_session(tenant) as source_db:
            return await original(source_db, tenant, user, receipt, digest)

    monkeypatch.setattr(content_input_receipts, "validate_published_workspace_receipt", second_connection)
    with pytest.raises(AssertionError, match="publication used more than one connection"):
        await test_web_csv_report_delivers_without_blocking_its_settlement(
            owner_sessionmaker, monkeypatch, tmp_path, retained_reports=0
        )
