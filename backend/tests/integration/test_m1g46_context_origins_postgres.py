"""Context construction is canonical evidence, separate from leaf-source authority."""

from copy import deepcopy
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.kernel.engine import _context_message, _mid_run_items_to_user_messages
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult
from app.services.llm_client import LLMMessage
from app.services.session_model_inputs import _hash, load_context_message_origin
from app.services.session_model_round import prepare_model_request
from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_resident_memory_sources_bind_the_selected_raw_bytes(tmp_path):
    import hashlib
    from app.memory.profile_plane import load_resident_memory

    agent_id = uuid.uuid4()
    target = tmp_path / str(agent_id) / "memory/self/self.md"
    target.parent.mkdir(parents=True)
    body = b"# Self\r\n\r\nSELECTED_RESIDENT_MEMORY\r\n"
    target.write_bytes(body)
    resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path)
    assert "SELECTED_RESIDENT_MEMORY" in resident.text and "\r" not in resident.text
    assert resident.source_refs == ({"source_kind": "agent_resident_memory",
        "source_id": f"{agent_id}\nmemory/self/self.md", "source_version": hashlib.sha256(body).hexdigest()},)
    target.write_text("REPLACED_AFTER_SELECTION")
    assert resident.source_refs[0]["source_version"] == hashlib.sha256(body).hexdigest()


def test_compiler_source_state_is_not_hydrated_from_client_metadata():
    from app.runtime.context import RuntimeAssemblyState

    state = RuntimeAssemblyState.from_metadata({"compiled_context_sources": {"forged": [{"source_id": "CLAIM"}]},
        "compiled_context_unbound": {"forged": []}})
    assert state.compiled_context_sources == {}
    sources = [{"source_kind": "agent_context_resource", "source_id": "fixture\nchannels", "source_version": "version"}]
    state.record_context_sources(agent_id="fixture", content="Actual prefix", sources=sources)
    assert state.context_sources_for(agent_id="fixture", content="Actual prefix") == tuple(sources)
    assert state.context_sources_for(agent_id="other", content="Actual prefix") == ()
    assert "compiled_context_sources" not in state.to_metadata()
    assert state.context_unbound_for(agent_id="fixture", content="Actual prefix")
    missing = ["actual_source_unavailable"]
    state.record_context_sources(agent_id="fixture", content="Actual prefix", sources=sources, unbound=missing)
    missing.clear()
    assert state.context_unbound_for(agent_id="fixture", content="Actual prefix") == ("actual_source_unavailable",)
    assert "compiled_context_unbound" not in state.to_metadata()


async def _prepare(db, scope, context):
    tenant, agent, session, run, bound = scope
    request = await prepare_model_request(db, tenant_id=tenant, agent_id=agent, session_id=session,
        run_id=run, turn_id=f"turn-{run.hex}", round_index=1,
        messages=[context, *_mid_run_items_to_user_messages(bound)], tools=[], provider="openai", model="synthetic-no-provider")
    row = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
    await db.commit()
    return row.model_request_snapshot_json


async def test_actual_compiler_origin_is_recoverable_but_does_not_assert_upstream_authority(owner_sessionmaker, app_user_sessionmaker):
    scope = await _bound_input(owner_sessionmaker)
    tenant, agent, session, _run, _bound = scope
    sections = [{"name": "fixture-context", "source_ref": "runtime.prompt_builder", "selected": True}]
    context = _context_message(role="system", content="Exact compiler context", kind="frozen_prompt", sections=sections)
    sections[0]["name"] = "MUTATED_AFTER_CAPTURE"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        snapshot = await _prepare(db, scope, context)
        bindings = snapshot["input_bindings"]
        assert bindings["status"] == "resolved" and bindings["messages"][0]["status"] == "bound"
        assert bindings["dependency_coverage"]["complete"] is False
        assert bindings["source_authority"] == "not_evaluated"
        source_id = uuid.UUID(bindings["messages"][0]["source_ref"].split(":", 1)[1])
        origin = await load_context_message_origin(db, tenant_id=tenant, agent_id=agent, session_id=session, source_id=source_id)
        assert origin["message"] == {"role": "system", "content": "Exact compiler context"}
        assert origin["sections"][0]["name"] == "fixture-context"
        assert "context_origin" not in snapshot["wire_request"]["messages"][0]
        assert "source_ref" not in context.to_openai_format() and "context_origin" not in context.to_openai_format()


async def test_known_platform_guidance_can_have_complete_inventory_without_granting_authority(owner_sessionmaker, app_user_sessionmaker):
    scope = await _bound_input(owner_sessionmaker)
    context = _context_message(role="system", content="Use the authorized tools.", kind="platform_guidance", unbound=())
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, scope[0])
        snapshot = await _prepare(db, scope, context)
        bindings = snapshot["input_bindings"]
        assert bindings["dependency_coverage"]["complete"] is True
        assert bindings["source_authority"] == "not_evaluated"
        assert [ref["source_kind"] for ref in bindings["dependencies"]] == ["session_command"]
        origin = await load_context_message_origin(db, tenant_id=scope[0], agent_id=scope[1],
            source_id=uuid.UUID(bindings["messages"][0]["source_ref"].split(":", 1)[1]))
        assert origin["dependency_coverage"] == {"complete": True, "unbound": []}


@pytest.mark.parametrize("change", ["dictionary", "changed_bytes"])
async def test_untrusted_or_stale_context_tags_never_become_canonical_origins(owner_sessionmaker, app_user_sessionmaker, change):
    scope = await _bound_input(owner_sessionmaker)
    context = _context_message(role="system", content="Original compiler bytes", kind="frozen_prompt")
    if change == "dictionary":
        context = LLMMessage(role="system", content=context.content, context_origin={
            "kind": "frozen_prompt", "message_sha256": context.context_origin.message_sha256, "sections": []})
    else:
        context.content = "Changed after compiler capture"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, scope[0])
        snapshot = await _prepare(db, scope, context)
        assert snapshot["input_bindings"]["messages"][0]["status"] == "unresolved"
        assert snapshot["input_bindings"]["dependency_coverage"]["complete"] is False
        assert (await db.scalars(select(InvocationSpan).where(InvocationSpan.span_type == "kernel_context"))).all() == []


async def test_context_origins_enforce_session_scope_and_detect_rewritten_records_live(owner_sessionmaker, app_user_sessionmaker):
    scope = await _bound_input(owner_sessionmaker)
    tenant, agent, session, _run, _bound = scope
    context = _context_message(role="user", content="Runtime notice", kind="dynamic_prompt")
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        snapshot = await _prepare(db, scope, context)
        source_id = uuid.UUID(snapshot["input_bindings"]["messages"][0]["source_ref"].split(":", 1)[1])
        assert await load_context_message_origin(db, tenant_id=tenant, agent_id=agent, session_id=uuid.uuid4(), source_id=source_id) is None
        retained = await db.get(InvocationSpan, source_id)
        original = deepcopy(retained.metadata_json["context_origin"])
        for change in ("message", "coverage"):
            proof = deepcopy(original)
            if change == "message":
                proof["message"]["content"] = "FORGED_CANONICAL_CONTEXT"
            else:
                proof["dependency_coverage"] = {"complete": True}
            proof["source_hash"] = _hash({key: value for key, value in proof.items() if key != "source_hash"})
            async with owner_sessionmaker() as writer:
                (await writer.get(InvocationSpan, source_id)).metadata_json = {"context_origin": proof}
                await writer.commit()
            assert await load_context_message_origin(db, tenant_id=tenant, agent_id=agent, session_id=session, source_id=source_id) is None
        assert retained.id == source_id


@pytest.mark.parametrize("creation", ["original", "model_blueprint", "legacy", "repair"])
async def test_soul_authority_requires_original_creation_and_exact_bootstrap_bytes(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, creation,
):
    from app.config import get_settings
    from app.models.agent import Agent
    from app.models.config_revision import ConfigRevision
    from app.models.user import User
    from app.services import agent_context, agent_manager
    from app.services.agent_manager import AgentManager
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies
    from tests.services.test_session_input_control_v2 import _seed_session

    tenant, user_id, agent_id, _session, _run = await _seed_session(owner_sessionmaker)
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path)
    root = tmp_path / str(agent_id)
    if creation == "repair":
        root.mkdir()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        agent = await db.get(Agent, agent_id)
        if creation == "original":
            from app.api.agents import create_agent
            from app.schemas.schemas import AgentCreate

            user = await db.get(User, user_id)
            user.role = "org_admin"
            created = await create_agent(AgentCreate(name="Original employee", personality="ORIGINAL_IDENTITY",
                permission_scope_type="user"), current_user=user, db=db)
            agent_id = created.id
            agent = await db.get(Agent, agent_id)
            root = tmp_path / str(agent_id)
        else:
            await AgentManager().initialize_agent_files(db, agent, personality="ORIGINAL_IDENTITY",
                original_author_id=user_id if creation != "legacy" else None,
                blueprint={"core_outputs": ["MODEL_DERIVED"]} if creation == "model_blueprint" else None,
                repair_existing=creation == "repair")
        await db.commit()
        user = await db.get(User, user_id)
        soul = await agent_context.load_agent_context_resource(agent_id=agent_id, tenant_id=tenant, resource_ref="soul")
        refs = [{"source_kind": "agent_context_resource", "source_id": f"{agent_id}\nsoul",
            "source_version": soul.sha256}]
        from app.memory.profile_plane import load_resident_memory

        resident = load_resident_memory(agent_id=agent_id, data_root=tmp_path)
        assert resident.source_refs and resident.source_refs[0]["source_id"] == f"{agent_id}\nmemory/self/self.md"
        from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot

        capture = SkillInputCapture(agent_id)
        await load_skill_input_snapshot(capture, tenant)
        bundled_path = "skills/mcp-installer/SKILL.md"
        bundled_bytes = (root / bundled_path).read_bytes() if (root / bundled_path).exists() else b""
        import hashlib

        capture.observe_file(bundled_path, hashlib.sha256(bundled_bytes).hexdigest())
        platform_refs = capture.snapshot()["sources"]
        if creation != "original":
            assert platform_refs == [] and capture.snapshot()["unbound"]
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, tenant, user, refs)
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, tenant, user, list(resident.source_refs))
            return
        await validate_browser_input_dependencies(db, tenant, user, refs)
        await validate_browser_input_dependencies(db, tenant, user, list(resident.source_refs))
        assert len(platform_refs) == 1 and platform_refs[0]["source_kind"] == "agent_platform_skill_file"
        await validate_browser_input_dependencies(db, tenant, user, platform_refs)
        (root / bundled_path).write_bytes(bundled_bytes + b"\nREPLACED_BUNDLE")
        with pytest.raises(ContentInputReceiptError):
            await validate_browser_input_dependencies(db, tenant, user, platform_refs)
        (root / bundled_path).write_bytes(bundled_bytes)
        self_path = root / "memory/self/self.md"
        self_path.write_text("RESTRICTED_MODEL_MEMORY")
        rewritten = load_resident_memory(agent_id=agent_id, data_root=tmp_path)
        for memory_refs in (resident.source_refs, rewritten.source_refs):
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, tenant, user, list(memory_refs))
        revision = await db.scalar(select(ConfigRevision).where(ConfigRevision.entity_type == "agent_bootstrap",
            ConfigRevision.entity_id == agent_id))
        assert revision is not None and revision.changed_by_user_id == user_id
        original = (root / "soul.md").read_text()
        (root / "soul.md").write_text(original + "\n")
        same_render = await agent_context.load_agent_context_resource(agent_id=agent_id, tenant_id=tenant, resource_ref="soul")
        assert same_render.sha256 == soul.sha256
        with pytest.raises(ContentInputReceiptError):
            await validate_browser_input_dependencies(db, tenant, user, refs)
        # Neither matching current hashes nor a subsequent repair can rebaseline unknown bytes.
        (root / "soul.md").write_text("# Rewritten\nRESTRICTED_MODEL_DERIVATIVE\n")
        current = await agent_context.load_agent_context_resource(agent_id=agent_id, tenant_id=tenant, resource_ref="soul")
        for version in (soul.sha256, current.sha256):
            refs[0]["source_version"] = version
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, tenant, user, refs)
        await AgentManager().initialize_agent_files(db, agent, original_author_id=user_id, repair_existing=True)
        assert (await db.scalars(select(ConfigRevision).where(ConfigRevision.entity_type == "agent_bootstrap",
            ConfigRevision.entity_id == agent_id))).all() == [revision]
