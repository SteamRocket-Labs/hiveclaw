"""Cross-owner behavioral guardrail for B4 (owner-agent KB read).

The SQL-structure test
``test_owner_agent_search_statement_uses_agent_owner_chain_without_trusting_agent_id``
only pins that the access predicate *emits* the agent owner-chain + tenant
clauses. This file proves the predicate *behaves*, against real PostgreSQL:

* an agent that belongs to the scope owner reads the owner's KB through an
  explicit ``AgentRuntimePrincipal`` with NO explicit grant — spec D3
  "owner's agents can search the full library";
* an agent that belongs to a DIFFERENT owner sees nothing in owner A's scope —
  the security boundary of B4's grant-free allow branch (no cross-owner leak).

Ownership resolves through ``coalesce(owner_user_id, sponsor_user_id,
creator_id)`` in the predicate, matching how the tool handler derives the owner
scope; the agents below set only ``creator_id`` (the model event fills sponsor).

Real PostgreSQL is required — a monkeypatched session cannot execute the
``EXISTS (SELECT ... FROM agents ...)`` sub-select. The whole directory skips
when Docker is unavailable (see ``tests/integration/conftest.py``).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database import Base
from app.models.agent import Agent
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment
from app.models.tenant import Tenant
from app.models.user import User
from app.services.personal_knowledge_access import AgentRuntimePrincipal, HumanBrowserPrincipal
from app.services.personal_knowledge_service import build_personal_knowledge_document_list_statement


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
    ids = {key: uuid.uuid4() for key in ("tenant", "ownerA", "ownerB", "agentA", "agentB", "docA")}
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
        await session.commit()
    return ids


async def _visible_doc_ids(owner_sessionmaker, *, tenant_id, owner_user_id, agent_id) -> list[uuid.UUID]:
    async with owner_sessionmaker() as session:
        statement = build_personal_knowledge_document_list_statement(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            principal=AgentRuntimePrincipal(agent_id=agent_id, requester_user_id=None),
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


async def test_owner_agent_reads_owner_kb_without_grant(complete_schema, owner_sessionmaker):
    """spec D3: the owner's own agent sees the owner's KB in the autonomous path
    (no interactive user, no explicit grant)."""
    ids = await _seed_two_owners(owner_sessionmaker)
    visible = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentA"],
    )
    assert ids["docA"] in visible


async def test_cross_owner_agent_cannot_read_kb(complete_schema, owner_sessionmaker):
    """Security boundary: an agent belonging to a different owner sees nothing in
    owner A's scope — the grant-free allow branch must not leak across owners."""
    ids = await _seed_two_owners(owner_sessionmaker)
    visible = await _visible_doc_ids(
        owner_sessionmaker,
        tenant_id=ids["tenant"],
        owner_user_id=ids["ownerA"],
        agent_id=ids["agentB"],
    )
    assert visible == []


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
    assert after == [ids["docA"]]


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
