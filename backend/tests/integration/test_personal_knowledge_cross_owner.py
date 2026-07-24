"""Real-PostgreSQL authority matrix for Personal Knowledge search/read."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import Base
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment
from app.models.tenant import Tenant
from app.models.user import User
from app.services.personal_knowledge_access import AgentRuntimePrincipal, HumanBrowserPrincipal
from app.services.personal_knowledge_service import (
    PersonalKnowledgeService,
    build_personal_knowledge_document_list_statement,
)
from app.services.personal_knowledge_proposals import PersonalKnowledgeProposalService


def _sha() -> str:
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


@pytest.fixture
async def complete_schema(owner_engine):
    """Ensure every ORM table exists (parity with main.py's lifespan
    ``create_all``), mirroring ``test_runtime_bootstrap_rls.py``. This test
    reads only via the owner session, so no non-owner GRANT is needed."""
    async with owner_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_two_owners(owner_sessionmaker) -> dict[str, uuid.UUID]:
    """One tenant, two owners, one agent each, and a person-scope document owned
    by owner A. Staged flushes satisfy the FK chain (no ORM relationships between
    these tables, so the unit-of-work cannot order the inserts on its own)."""
    ids = {key: uuid.uuid4() for key in ("tenant", "ownerA", "ownerB", "agentA", "agentB", "docA", "docAPl3")}
    suffix = uuid.uuid4().hex[:8]
    async with owner_sessionmaker() as session:
        session.add(Tenant(id=ids["tenant"], name="T", slug=f"t-{suffix}"))
        await session.flush()
        for key, tag in (("ownerA", "a"), ("ownerB", "b")):
            session.add(
                User(
                    id=ids[key],
                    username=f"u-{tag}-{suffix}",
                    email=f"u-{tag}-{suffix}@example.test",
                    password_hash="x",
                    display_name=tag.upper(),
                    tenant_id=ids["tenant"],
                )
            )
        await session.flush()
        session.add(Agent(id=ids["agentA"], name="AA", creator_id=ids["ownerA"], tenant_id=ids["tenant"]))
        session.add(Agent(id=ids["agentB"], name="AB", creator_id=ids["ownerB"], tenant_id=ids["tenant"]))
        await session.flush()
        session.add(
            KnowledgeDocument(
                id=ids["docA"],
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                owner_user_id=ids["ownerA"],
                source_kind="paste",
                source_sha256=_sha(),
                title="ownerA-doc",
                canonical_md_path=f"persons/{ids['ownerA']}/kb/x.md",
                status="ready",
                sensitivity="PL1_public",
                agent_searchable=True,
                created_by_user_id=ids["ownerA"],
            )
        )
        await session.flush()
        session.add(
            KnowledgeDocument(
                id=ids["docAPl3"],
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                owner_user_id=ids["ownerA"],
                source_kind="paste",
                source_sha256=_sha(),
                title="ownerA-sensitive-doc",
                canonical_md_path=f"persons/{ids['ownerA']}/kb/sensitive.md",
                status="degraded",
                sensitivity="PL3_sensitive",
                agent_searchable=True,
                created_by_user_id=ids["ownerA"],
            )
        )
        await session.flush()
        session.add(
            KnowledgeSegment(
                id=uuid.uuid4(),
                tenant_id=ids["tenant"],
                document_id=ids["docA"],
                scope_type="person",
                scope_id=ids["ownerA"],
                position=0,
                segment_hash=_sha(),
                content="hello owner A",
                token_count=3,
            )
        )
        session.add(
            KnowledgeSegment(
                id=uuid.uuid4(),
                tenant_id=ids["tenant"],
                document_id=ids["docAPl3"],
                scope_type="person",
                scope_id=ids["ownerA"],
                position=0,
                segment_hash=_sha(),
                content="sensitive owner A",
                token_count=3,
            )
        )
        await session.commit()
    return ids


async def _visible_doc_ids(
    owner_sessionmaker,
    *,
    tenant_id,
    owner_user_id,
    agent_id,
    requester_user_id,
    session_id: str | None = None,
    purpose: str = "interactive_session",
    autonomous: bool = False,
    action: str = "search",
) -> list[uuid.UUID]:
    async with owner_sessionmaker() as session:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=AgentRuntimePrincipal(
                agent_id=agent_id,
                requester_user_id=requester_user_id,
                session_id=session_id,
                purpose=purpose,
                autonomous=autonomous,
            ),
            action=action,
            limit=25,
        )
        return [row[0].id for row in (await session.execute(statement)).all()]


async def _human_visible_doc_ids(owner_sessionmaker, *, tenant_id, owner_user_id, user_id) -> list[uuid.UUID]:
    async with owner_sessionmaker() as session:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=HumanBrowserPrincipal(user_id=user_id),
            limit=25,
        )
        return [row[0].id for row in (await session.execute(statement)).all()]


async def test_interactive_owner_uses_owned_agent_for_pl1_through_pl3_without_grant(
    complete_schema,
    owner_sessionmaker,
):
    ids = await _seed_two_owners(owner_sessionmaker)
    visible = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerA"],
        session_id="owner-interactive-session",
    )
    assert set(visible) == {ids["docA"], ids["docAPl3"]}


async def test_autonomous_owner_agent_requires_explicit_scoped_grant(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    visible = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerA"],
        purpose="autonomous_agent",
        autonomous=True,
    )
    assert visible == []


async def test_cross_owner_agent_cannot_read_kb(complete_schema, owner_sessionmaker):
    """Security boundary: an agent belonging to a different owner sees nothing in
    owner A's scope — the grant-free allow branch must not leak across owners."""
    ids = await _seed_two_owners(owner_sessionmaker)
    visible = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentB"],
        requester_user_id=ids["ownerB"],
        session_id="cross-owner-session",
    )
    assert visible == []


async def test_owner_agent_relation_never_replaces_current_requester_grant(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    visible = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerB"],
        session_id="shared-agent-session",
    )
    assert visible == []


async def test_cross_principal_agent_grant_binds_requester_session_purpose_ceiling_action_and_revoke(
    complete_schema,
    owner_sessionmaker,
):
    ids = await _seed_two_owners(owner_sessionmaker)
    session_id = "shared-agent-session"
    purpose = "interactive_session"
    grant_id = uuid.uuid4()
    async with owner_sessionmaker() as session:
        session.add(
            KnowledgeGrant(
                id=grant_id,
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                resource_type="scope",
                resource_id=ids["ownerA"],
                grantee_type="agent",
                grantee_id=ids["agentA"],
                permission="search",
                requester_user_id=ids["ownerB"],
                session_id=session_id,
                purpose=purpose,
                sensitivity_ceiling="PL1_public",
                binding_key="test-cross-principal",
                created_by_user_id=ids["ownerA"],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await session.commit()

    allowed = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerB"],
        session_id=session_id,
        purpose=purpose,
        action="search",
    )
    assert allowed == [ids["docA"]]

    for wrong_session, wrong_purpose in (("wrong", purpose), (session_id, "a2a_delegation")):
        assert (
            await _visible_doc_ids(
                owner_sessionmaker,
                tenant_id=ids["tenant"],
                owner_user_id=ids["ownerA"],
                agent_id=ids["agentA"],
                requester_user_id=ids["ownerB"],
                session_id=wrong_session,
                purpose=wrong_purpose,
                action="search",
            )
            == []
        )

    assert (
        await _visible_doc_ids(
            owner_sessionmaker,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            agent_id=ids["agentA"],
            requester_user_id=ids["ownerB"],
            session_id=session_id,
            purpose=purpose,
            action="read",
        )
        == []
    )

    async with owner_sessionmaker() as session:
        grant = await session.get(KnowledgeGrant, grant_id)
        grant.permission = "read"
        grant.sensitivity_ceiling = "PL3_sensitive"
        await session.commit()

    read_allowed = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerB"],
        session_id=session_id,
        purpose=purpose,
        action="read",
    )
    assert set(read_allowed) == {ids["docA"], ids["docAPl3"]}

    async with owner_sessionmaker() as session:
        grant = await session.get(KnowledgeGrant, grant_id)
        grant.revoked_at = datetime.now(timezone.utc)
        grant.revoked_by_user_id = ids["ownerA"]
        await session.commit()

    revoked = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerB"],
        session_id=session_id,
        purpose=purpose,
        action="read",
    )
    assert revoked == []


async def test_human_browser_requires_explicit_live_user_grant(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)

    before = await _human_visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        user_id=ids["ownerB"],
    )
    assert before == []

    async with owner_sessionmaker() as session:
        session.add(
            KnowledgeGrant(
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                resource_type="scope",
                resource_id=ids["ownerA"],
                grantee_type="user",
                grantee_id=ids["ownerB"],
                permission="read",
                sensitivity_ceiling="PL3_sensitive",
                binding_key="human-live",
                created_by_user_id=ids["ownerA"],
            )
        )
        await session.commit()

    after = await _human_visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        user_id=ids["ownerB"],
    )
    assert set(after) == {ids["docA"], ids["docAPl3"]}


async def test_human_browser_rejects_expired_user_grant(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    async with owner_sessionmaker() as session:
        session.add(
            KnowledgeGrant(
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                resource_type="scope",
                resource_id=ids["ownerA"],
                grantee_type="user",
                grantee_id=ids["ownerB"],
                permission="read",
                sensitivity_ceiling="PL3_sensitive",
                binding_key="human-expired",
                created_by_user_id=ids["ownerA"],
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await session.commit()

    visible = await _human_visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        user_id=ids["ownerB"],
    )
    assert visible == []


async def test_typed_search_decision_distinguishes_denied_from_empty(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    service = PersonalKnowledgeService()
    async with owner_sessionmaker() as session:
        denied = await service.search_personal_with_authority(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            query="no matching phrase",
            principal=AgentRuntimePrincipal(
                agent_id=ids["agentA"],
                requester_user_id=ids["ownerB"],
                session_id="cross-owner-typed",
            ),
        )
        empty = await service.search_personal_with_authority(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            query="no matching phrase",
            principal=AgentRuntimePrincipal(
                agent_id=ids["agentA"],
                requester_user_id=ids["ownerA"],
                session_id="owner-empty-typed",
            ),
        )

    assert denied.status == "denied"
    assert denied.authority.allowed is False
    assert denied.authority.deny_reason_code == "explicit_grant_required"
    assert empty.status == "empty"
    assert empty.authority.allowed is True
    assert empty.authority.authority_source == "interactive_owner_agent"


async def test_pl4_read_and_search_never_return_knowledge_bytes(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    document_id = uuid.uuid4()
    secret = "PL4-DO-NOT-RETURN credential needle"
    credential_reference = "secret://tenant/provider-credential"
    async with owner_sessionmaker() as session:
        session.add(
            KnowledgeDocument(
                id=document_id,
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                owner_user_id=ids["ownerA"],
                source_kind="paste",
                source_sha256=_sha(),
                title=f"credential title {secret}",
                canonical_md_path=f"persons/{ids['ownerA']}/kb/credential.md",
                status="ready",
                sensitivity="PL4_credential",
                agent_searchable=True,
                doc_metadata_json={"credential_reference": credential_reference},
                created_by_user_id=ids["ownerA"],
            )
        )
        await session.flush()
        session.add(
            KnowledgeSegment(
                id=uuid.uuid4(),
                tenant_id=ids["tenant"],
                document_id=document_id,
                scope_type="person",
                scope_id=ids["ownerA"],
                position=0,
                segment_hash=_sha(),
                content=secret,
                token_count=8,
            )
        )
        await session.commit()

    principal = AgentRuntimePrincipal(
        agent_id=ids["agentA"],
        requester_user_id=ids["ownerA"],
        session_id="owner-pl4-session",
    )
    service = PersonalKnowledgeService()
    async with owner_sessionmaker() as session:
        read_result = await service.get_personal_document_with_authority(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            document_id=document_id,
            principal=principal,
        )
        search_result = await service.search_personal_with_authority(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            query="credential needle",
            principal=principal,
        )
        legacy_direct_read = await service.get_personal_document(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            document_id=document_id,
            principal=principal,
        )
        source_preview = await service.get_personal_document_source_preview(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            document_id=document_id,
            principal=principal,
        )

    assert read_result.status == "ok"
    assert read_result.document is None
    assert read_result.credential_reference == credential_reference
    assert read_result.authority.credential_reference_only is True
    pl4_hits = [hit for hit in search_result.hits if hit.document_id == document_id]
    assert len(pl4_hits) == 1
    assert pl4_hits[0].title == "Credential reference"
    assert pl4_hits[0].snippet == ""
    assert pl4_hits[0].heading_path == []
    assert pl4_hits[0].source_ref == credential_reference
    assert pl4_hits[0].credential_reference == credential_reference
    assert legacy_direct_read is None
    assert source_preview is None
    assert secret not in repr(read_result)
    assert secret not in repr(search_result)


async def test_owner_can_create_and_soft_revoke_a_bounded_agent_grant(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    service = PersonalKnowledgeService()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    async with owner_sessionmaker() as session:
        summary = await service.create_personal_grant(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            current_user_id=ids["ownerA"],
            resource_type="scope",
            resource_id=ids["ownerA"],
            document_id=None,
            grantee_type="agent",
            grantee_id=ids["agentA"],
            permission="read",
            requester_user_id=ids["ownerB"],
            session_id="shared-agent-bounded",
            purpose="a2a_delegation",
            delegation_id="delegation-bounded",
            sensitivity_ceiling="PL3_sensitive",
            expires_at=expires_at,
            grant_metadata={"reason": "bounded collaboration"},
        )
        assert summary is not None
        grant_id = summary.grant_id
        await session.commit()

    assert summary.active is True
    assert summary.requester_user_id == ids["ownerB"]
    assert summary.session_id == "shared-agent-bounded"
    assert summary.purpose == "a2a_delegation"
    assert summary.delegation_id == "delegation-bounded"
    assert summary.sensitivity_ceiling == "PL3_sensitive"

    async with owner_sessionmaker() as session:
        allowed = await service.search_personal_with_authority(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            query="owner",
            principal=AgentRuntimePrincipal(
                agent_id=ids["agentA"],
                requester_user_id=ids["ownerB"],
                session_id="shared-agent-bounded",
                purpose="a2a_delegation",
                delegation_id="delegation-bounded",
            ),
        )
        wrong_delegation = await service.search_personal_with_authority(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            query="owner",
            principal=AgentRuntimePrincipal(
                agent_id=ids["agentA"],
                requester_user_id=ids["ownerB"],
                session_id="shared-agent-bounded",
                purpose="a2a_delegation",
                delegation_id="delegation-wrong",
            ),
        )
        revoked = await service.delete_personal_grant(
            session,
            tenant_id=ids["tenant"],
            owner_user_id=ids["ownerA"],
            current_user_id=ids["ownerA"],
            grant_id=grant_id,
        )
        await session.commit()

    assert allowed.status == "ok"
    assert allowed.authority.grant_id == grant_id
    assert wrong_delegation.status == "denied"
    assert revoked is True
    async with owner_sessionmaker() as session:
        stored = await session.get(KnowledgeGrant, grant_id)
        assert stored is not None
        assert stored.revoked_at is not None
        assert stored.revoked_by_user_id == ids["ownerA"]
        audits = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.tenant_id == ids["tenant"],
                        AuditLog.user_id == ids["ownerA"],
                        AuditLog.action.in_(
                            (
                                "personal_kb.grant.upserted",
                                "personal_kb.grant.revoked",
                            )
                        ),
                    )
                    .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
                )
            )
            .scalars()
            .all()
        )
        assert [audit.action for audit in audits] == [
            "personal_kb.grant.upserted",
            "personal_kb.grant.revoked",
        ]
        assert [audit.details["grant_id"] for audit in audits] == [
            str(grant_id),
            str(grant_id),
        ]


async def test_revoked_grant_cannot_auto_approve_personal_kb_proposal(complete_schema, owner_sessionmaker):
    ids = await _seed_two_owners(owner_sessionmaker)
    grant_id = uuid.uuid4()
    async with owner_sessionmaker() as session:
        session.add(
            KnowledgeGrant(
                id=grant_id,
                tenant_id=ids["tenant"],
                scope_type="person",
                scope_id=ids["ownerA"],
                resource_type="scope",
                resource_id=ids["ownerA"],
                grantee_type="agent",
                grantee_id=ids["agentA"],
                permission="manage",
                requester_user_id=ids["ownerA"],
                purpose="autonomous_agent",
                sensitivity_ceiling="PL3_sensitive",
                binding_key="proposal-auto-approve",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                grant_metadata_json={"proposal_mode": "auto_approve"},
                created_by_user_id=ids["ownerA"],
            )
        )
        await session.commit()

    proposals = PersonalKnowledgeProposalService()
    async with owner_sessionmaker() as session:
        assert (
            await proposals._auto_approve_grant(
                session,
                tenant_id=ids["tenant"],
                owner_user_id=ids["ownerA"],
                agent_id=ids["agentA"],
            )
            is True
        )
        grant = await session.get(KnowledgeGrant, grant_id)
        grant.revoked_at = datetime.now(timezone.utc)
        grant.revoked_by_user_id = ids["ownerA"]
        await session.commit()

    async with owner_sessionmaker() as session:
        assert (
            await proposals._auto_approve_grant(
                session,
                tenant_id=ids["tenant"],
                owner_user_id=ids["ownerA"],
                agent_id=ids["agentA"],
            )
            is False
        )
