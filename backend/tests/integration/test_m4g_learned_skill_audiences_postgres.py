"""Learned group methods retain actual Knowledge permissions and recipients."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory, group_call, finish_group_work  # noqa: F401
from tests.integration.test_m4g_learned_skill_sources_postgres import controlled_provider_boundary  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def group_method(group_memory, app_user_sessionmaker, owner_sessionmaker, monkeypatch):  # noqa: F811
    from app.models.capability_policy import CapabilityPolicy
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant, KnowledgeSegment
    from app.services.skill_distiller import DistilledSkillDraft, run_skill_distillation_cycle
    from tests.services.test_skill_distiller import _passing_artifact_gate, _approving_referee_review

    g = group_memory
    n = g.n
    marker = "GROUP_KB_METHOD_MARKER"
    body = f"Use the primary document and retain the comparison: {marker}."
    source = n.root.parent / "group-method-source.md"
    source.write_text(body)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    document_id, peer_grant_id, peer_runtime_grant_id = uuid4(), uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        for name in ("agent.skill.read", "agent.skill.write", "agent.knowledge.read"):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=name, allowed=True, requires_approval=False))
        db.add(KnowledgeDocument(id=document_id, tenant_id=n.tenant, scope_type="person", scope_id=n.user,
            owner_user_id=n.user, source_kind="text", source_sha256=digest, title="Evidence comparison method",
            status="ready", canonical_md_path=source.name, canonical_md_sha256=digest))
        await db.flush()
        db.add(KnowledgeSegment(tenant_id=n.tenant, document_id=document_id, scope_type="person", scope_id=n.user,
            position=0, segment_hash=digest, content=body))
        db.add(KnowledgeGrant(id=peer_grant_id, tenant_id=n.tenant, scope_id=n.user, document_id=document_id,
            resource_type="document", resource_id=document_id, grantee_type="user", grantee_id=n.foreign, permission="read"))
        db.add(KnowledgeGrant(id=peer_runtime_grant_id, tenant_id=n.tenant, scope_id=n.user, document_id=document_id,
            resource_type="document", resource_id=document_id, grantee_type="agent", grantee_id=n.agent,
            requester_user_id=n.foreign, purpose="interactive_session", session_id=str(g.session), permission="read",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10)))
        await db.commit()
    read, _invocation = await group_call(g, app_user_sessionmaker, "read_personal_kb", {"document_id": str(document_id)},
        command="Read the authorized evidence comparison method for this group.")
    assert json.loads(read)["status"] == "ok" and marker in read, read
    args = {"name": "Group Evidence Review", "description": "Compare group work with authorized primary evidence.",
        "folder_name": "group-evidence-review", "instructions": body}
    submitted, _invocation = await group_call(g, app_user_sessionmaker, "save_skill", args,
        command="Learn this method for future group work.", include_history=True)
    assert "active_skill_created: false" in submitted, submitted
    prompts = []
    async def draft(**kwargs):
        prompts.append(repr(kwargs))
        return DistilledSkillDraft(decision="promote", confidence=0.95, name=args["name"], description=args["description"],
            instructions_markdown=body, declared_tools=(), declared_packs=(), reason="Controlled group method review.",
            skill_markdown=f"---\nname: {args['name']}\ndescription: {args['description']}\n---\n\n{body}\n")
    async def no_extra_sessions(**_kwargs):
        return []
    monkeypatch.setattr("app.services.skill_distiller._draft_skill_with_llm", draft)
    monkeypatch.setattr("app.services.skill_distiller._load_internal_session_evidence", no_extra_sessions)
    monkeypatch.setattr("app.services.skill_distiller._run_skill_artifact_gate", _passing_artifact_gate)
    monkeypatch.setattr("app.services.skill_distiller._review_skill_with_llm", _approving_referee_review)
    outcome = await run_skill_distillation_cycle(agent_id=n.agent, workspace=n.root, tenant_id=n.tenant,
        current_session_id=str(g.session), runtime_config=SimpleNamespace(skill_candidate_loop_enabled=True),
        model=SimpleNamespace(provider="synthetic", model="controlled-group-writer", api_key="synthetic", base_url="http://127.0.0.1:1"))
    assert outcome["status"] == "provisional" and prompts, outcome
    path = outcome["skill_path"]
    assert marker in (n.root / path).read_text()
    await finish_group_work(g, app_user_sessionmaker, user_id=n.user)
    return SimpleNamespace(g=g, path=path, body=(n.root / path).read_bytes(), marker=marker, document_id=document_id,
        peer_grant_id=peer_grant_id, peer_runtime_grant_id=peer_runtime_grant_id)


@pytest.mark.parametrize("withdrawal", [None, "recipient_grant", "runtime_grant", "unknown_recipient"])
async def test_learned_group_skill_uses_original_knowledge_and_current_delivery_audience(
    group_method, app_user_sessionmaker, owner_sessionmaker, withdrawal,
):
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.config_revision import ConfigRevision
    from app.models.knowledge import KnowledgeGrant
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService
    from app.services.channel_delivery_service import DeliveryResult

    m = group_method
    g, n = m.g, m.g.n
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        installation = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == n.tenant,
            ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.content["folder_name"].astext == m.path.split('/')[1]))
        assert installation.content["learning"]["authority"]["audience_kind"] == "group"
        reviewed = await db.get(ConfigRevision, UUID(installation.content["learning"]["candidate_revision_id"]))
        submitted = await db.get(ConfigRevision, UUID(reviewed.content["submitted_candidate_revision_id"]))
        assert {"source_kind": "learned_skill_candidate", "source_id": str(submitted.id),
            "source_version": submitted.content_hash} in installation.content["learning"]["review_dependencies"]
        assert any(ref["source_kind"] == "knowledge_document" and ref["source_id"] == str(m.document_id)
            for ref in submitted.content["dependencies"]), submitted.content
    if withdrawal == "runtime_grant":
        async with owner_sessionmaker() as db:
            (await db.get(KnowledgeGrant, m.peer_runtime_grant_id)).revoked_at = datetime.now(timezone.utc)
            await db.commit()
    loaded, _invocation = await group_call(g, app_user_sessionmaker, "load_skill", {"name": "Group Evidence Review"},
        user_id=n.foreign, command="Apply our group method to my next comparison.")
    if withdrawal == "runtime_grant":
        assert m.marker not in loaded, loaded
        assert (n.root / m.path).read_bytes() == m.body
        return
    from app.services.learned_skill_sources import validate_learned_installation
    from app.services.personal_knowledge_access import AgentRuntimePrincipal

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        await validate_learned_installation(db, await db.get(ConfigRevision, installation.id),
            tenant_id=n.tenant, agent_id=n.agent, request=AgentRuntimePrincipal(agent_id=n.agent,
                requester_user_id=n.foreign, session_id=str(g.session), runtime_task_id=str(g.runs[n.foreign])))
    assert m.marker in loaded, loaded
    await finish_group_work(g, app_user_sessionmaker, user_id=n.foreign)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        rows = list(await db.scalars(select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.tenant_id == n.tenant,
            ChannelDeliveryOutbox.runtime_task_id == g.runs[n.foreign])))
        assert len(rows) == 1
        identity = rows[0].id
        rows[0].available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        await db.commit()
    if withdrawal == "recipient_grant":
        async with owner_sessionmaker() as db:
            (await db.get(KnowledgeGrant, m.peer_grant_id)).revoked_at = datetime.now(timezone.utc)
            await db.commit()
    elif withdrawal == "unknown_recipient":
        g.members.add("unknown-method-recipient")
    sends = []
    async def send(**kwargs):
        sends.append(kwargs)
        return DeliveryResult(ok=True, status="delivered", channel="feishu", message="Controlled method delivery receipt")
    result = await ChannelDeliveryOutboxService(session_factory=app_user_sessionmaker,
        text_sender=send, file_sender=send).drain_once(worker_id="group-method-delivery", limit=1)
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        row = await db.get(ChannelDeliveryOutbox, identity)
        if withdrawal is None:
            assert result["delivered"] == 1 and len(sends) == 1 and row.status == "delivered", result
        elif withdrawal == "recipient_grant":
            assert not sends and result["dead_lettered"] == 1 and row.status == "dead_letter", result
        else:
            assert not sends and result["retried"] == 1 and row.status == "pending", result
    assert (n.root / m.path).read_bytes() == m.body
