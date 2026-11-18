"""A private member's next turn reads the current resident memory and Skill catalog."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.events import close_redis
from app.models.chat_session import ChatSession
from app.models.config_revision import ConfigRevision
from app.models.knowledge import KnowledgeDocument, KnowledgeGrant
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest
from app.runtime import invoker
from app.services import web_chat_runtime as runtime
from app.services.config_versioning import canonical_content_hash
from app.services.explicit_memory_authority import AUTHORITY_SCHEMA
from app.services.learned_skill_sources import CANDIDATE_ENTITY, CANDIDATE_SCHEMA, _candidate_entity
from app.services.personal_knowledge_service import PersonalKnowledgeService
from tests.integration.test_a2fp1_private_turn_budget_postgres import (
    ANSWER,
    app_user_engine,  # noqa: F401
    assert_committed_reply,
)
from tests.integration.test_a2p_member_request_cost_postgres import member_request  # noqa: F401
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("source", ["resident_memory", "platform_skill", "scoped_skill"])
async def test_private_turn_uses_the_next_committed_prompt_source(
    member_request, owner_sessionmaker, monkeypatch, tmp_path, source,  # noqa: F811
):
    from app.services import llm_client
    from tests.integration.test_a2fp1_private_turn_budget_postgres import LocalAuxTransport

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    root = tmp_path / str(seed.agent_id)
    before, after = "The reference color is coral.", "The reference color is amber."
    catalogs = []
    if source == "resident_memory":
        relative = "memory/self/self.md"
        template = "# Self\n\n{}\n"
    elif source == "platform_skill":
        relative = "skills/colors/SKILL.md"
        template = "---\nname: colors\ndescription: {}\n---\nFollow the color convention.\n"
    else:
        relative = "workspace/project/skills/colors/SKILL.md"
        template = "---\nname: colors\ndescription: {}\n---\nFollow the color convention.\n"
        async with owner_sessionmaker() as db:
            db.add(WorkspaceResourceManifest(tenant_id=seed.tenant_id, agent_id=seed.agent_id, path=relative,
                owner_user_id=seed.user_id, root_session_id=seed.session_id, authority_state="owned"))
            await db.commit()
        build_catalog = invoker._build_native_skill_catalog

        async def record_catalog(*args, **kwargs):
            catalog = await build_catalog(*args, **kwargs)
            catalogs.append(catalog)
            return catalog

        monkeypatch.setattr(invoker, "_build_native_skill_catalog", record_catalog)

    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    published_skill = tmp_path / "published-SKILL.md"
    if source == "platform_skill":
        from app.services import skill_source_receipts

        bundled_hashes = skill_source_receipts._bundled_platform_skill_hashes

        def hashes(*, source_path=None):
            bundled = bundled_hashes(source_path=source_path)
            if source_path in {None, relative}:
                bundled[relative] = hashlib.sha256(published_skill.read_bytes()).hexdigest()
            return bundled

        monkeypatch.setattr(skill_source_receipts, "_bundled_platform_skill_hashes", hashes)
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport([]))
    try:
        for current, previous in ((before, after), (after, before)):
            written = template.format(current)
            if source == "platform_skill":
                published_skill.write_text(written, encoding="utf-8")
            path.write_text(written, encoding="utf-8")
            provider = ScriptedLLMClient([{"content": ANSWER, "stream_text": ANSWER}])
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/runs",
                json={"content": "hello", "input_id": str(uuid.uuid4())})
            assert response.status_code == 201, response.text
            result = response.json()
            run_id = uuid.UUID(result["run"]["run_id"])
            await claim(seed, run_id, owner_sessionmaker)
            assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="rl0c")
            await runtime._TASKS[run_id.hex]
            await assert_committed_reply(owner_sessionmaker, run_id, uuid.UUID(result["session"]["id"]), seed)
            request, = provider.requests
            text = "\n".join(message["content"] for message in request["messages"] if isinstance(message["content"], str))
            if source == "scoped_skill":
                # The uninstalled workspace skill reaches the catalog but has no versioned source
                # receipt to disclose through the provider boundary.
                catalog, = catalogs
                assert current in catalog and previous not in catalog
                catalogs.clear()
            else:
                assert current in text
            assert previous not in text
    finally:
        await close_redis()


async def test_private_turn_omits_candidate_after_source_grant_revocation(
    member_request, owner_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    from app.services import llm_client
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
    from tests.integration.test_a2fp1_private_turn_budget_postgres import LocalAuxTransport

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    outbox = RuntimeTerminalBoundaryOutboxService(session_factory=factory)
    processor = WebTerminalBoundaryProcessor(session_factory=factory, data_root=tmp_path)
    session_id, grant_id = uuid.uuid4(), uuid.uuid4()
    candidate_id = "distillation-reference"
    knowledge = PersonalKnowledgeService(data_root=tmp_path, extractor=None)
    async with owner_sessionmaker() as db:
        source_owner = await db.scalar(select(User.id).where(User.tenant_id == seed.tenant_id, User.id != seed.user_id))
        imported = await knowledge.ingest_markdown(
            db, tenant_id=seed.tenant_id, owner_user_id=source_owner, title="Shared color reference",
            markdown="# Color reference\nUse a coral reference card.\n", source_kind="paste", sensitivity="PL1_public",
        )
        source = await db.get(KnowledgeDocument, imported.document_id)
        content = {
            "schema": CANDIDATE_SCHEMA, "origin_kind": "distillation",
            "agent_id": str(seed.agent_id), "candidate_id": candidate_id,
            "authority": {
                "schema": AUTHORITY_SCHEMA, "tenant_id": str(seed.tenant_id), "agent_id": str(seed.agent_id),
                "audience_kind": "private", "source_session_id": str(seed.session_id), "owner_user_id": str(seed.user_id),
            },
            "source_principal": {
                "runtime_task_id": str(seed.run_id), "session_id": str(seed.session_id),
                "agent_id": str(seed.agent_id), "requester_user_id": str(seed.user_id),
            },
            "dependencies": [{
                "source_kind": "knowledge_document", "source_id": str(source.id),
                "source_version": source.canonical_md_sha256,
            }],
        }
        db.add_all([
            ChatSession(id=session_id, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                user_id=seed.user_id, source_channel="web", audience_kind="private"),
            KnowledgeGrant(id=grant_id, tenant_id=seed.tenant_id, scope_id=source_owner,
                resource_type="document", resource_id=source.id, document_id=source.id, permission="read",
                grantee_type="agent", grantee_id=seed.agent_id, requester_user_id=seed.user_id,
                session_id=str(session_id), purpose="interactive_session", created_by_user_id=source_owner,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1)),
            ConfigRevision(tenant_id=seed.tenant_id, entity_type=CANDIDATE_ENTITY, version=1,
                entity_id=_candidate_entity(seed.agent_id, candidate_id), content=content,
                content_hash=canonical_content_hash(content), change_source="evolve",
                changed_by_user_id=seed.user_id, changed_by_agent_id=seed.agent_id),
        ])
        await db.commit()

    root = tmp_path / str(seed.agent_id)
    marker = "Use the retained coral card."
    candidate = root / "evolution" / "skill_candidates" / candidate_id / "coral-card.md"
    candidate.parent.mkdir(parents=True)
    candidate.write_text(f"---\nname: Coral card\ndescription: {marker}\n---\nCompare the reference cards.\n", encoding="utf-8")
    # Discover the instruction through skills/ while authorizing its resolved candidate path.
    (root / "skills" / candidate.name).symlink_to(candidate)

    catalogs = []
    build_catalog = invoker._build_native_skill_catalog

    async def record_catalog(request, *args, **kwargs):
        assert request.private_web_turn is not None
        catalog = await build_catalog(request, *args, **kwargs)
        catalogs.append(catalog)
        return catalog

    monkeypatch.setattr(invoker, "_build_native_skill_catalog", record_catalog)
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport([]))
    try:
        for revoked in (False, True):
            if revoked:
                async with owner_sessionmaker() as db:
                    assert await knowledge.delete_personal_grant(
                        db, tenant_id=seed.tenant_id, owner_user_id=source_owner,
                        current_user_id=source_owner, grant_id=grant_id,
                    )
                    await db.commit()

            provider = ScriptedLLMClient([{"content": ANSWER, "stream_text": ANSWER}])
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
            response = await client.post(f"/api/agents/{seed.agent_id}/sessions/{session_id}/runs",
                json={"content": "hello", "input_id": str(uuid.uuid4())})
            assert response.status_code == 201, response.text
            run_id = uuid.UUID(response.json()["run_id"])
            await claim(seed, run_id, owner_sessionmaker)
            assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="rl0c")
            await runtime._TASKS[run_id.hex]
            await assert_committed_reply(owner_sessionmaker, run_id, session_id, seed)
            catalog, = catalogs
            assert (marker in catalog) is (not revoked)
            catalogs.clear()
            boundary = await outbox.drain_once(
                tenant_id=seed.tenant_id, worker_id="candidate-boundary", canonical_validator=processor.validate,
                process_callback=processor, task_types=("web_chat_turn",),
            )
            assert boundary["delivered"] == 1, boundary
    finally:
        await close_redis()
