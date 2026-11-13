"""Package Skill updates keep existing delegation results readable."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.config_revision import ConfigRevision
from app.models.runtime_task import RuntimeTask
from app.models.skill import Skill, SkillFile
from app.models.session_v2 import SessionModelResult
from tests.integration.source_withholding_helpers import legacy_model_requests
from tests.integration.test_failed_delegation_continuation import _call, _execute, _failed_child
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

SKILL_PATH = "skills/mcp-installer/SKILL.md"
RESULT = "Three sales points: quality, service, value."


async def _push_default(owner, content):
    from app.services.skill_seeder import push_default_skills_to_existing_agents

    async with owner() as db:
        skill = Skill(name="MCP Tool Installer", folder_name="mcp-installer", is_builtin=True, is_default=True)
        skill.files = [SkillFile(path="SKILL.md", content=content.decode())]
        db.add(skill)
        await db.commit()
        skill_id = skill.id
    try:
        await push_default_skills_to_existing_agents()
    finally:
        async with owner() as db:
            await db.delete(await db.get(Skill, skill_id))
            await db.commit()


def _deploy_bundle(monkeypatch, tmp_path):
    """Load v2 from an actual replacement application template, not the registry."""
    from app.services import skill_seeder

    definitions = deepcopy(skill_seeder.builtin_skill_definitions(pack_dirs=()))
    definition = next(row for row in definitions if row["folder_name"] == "mcp-installer")
    original = definition["files"][0]["content"]
    updated = original + "\nApplication package v2 guidance.\n"
    package = tmp_path / "package"
    template = package / "agent_template" / SKILL_PATH
    template.parent.mkdir(parents=True)
    template.write_text(updated, encoding="utf-8")
    definition["files"] = []
    monkeypatch.setattr(skill_seeder, "BUILTIN_SKILLS", definitions)
    monkeypatch.setattr(skill_seeder, "__file__", str(package / "app/services/skill_seeder.py"))
    assert next(row for row in skill_seeder.builtin_skill_definitions(pack_dirs=())
        if row["folder_name"] == "mcp-installer")["files"][0]["content"] == updated
    return original.encode(), updated.encode()


@pytest.mark.parametrize("update_at", ["before_parent", "historical_before_parent", "before_read"])
@pytest.mark.usefixtures("auxiliary_models")
async def test_updated_package_keeps_old_parent_result_readable(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, update_at,
):
    import app.runtime.invoker as invoker
    from app.kernel.engine import _dynamic_suffix_notice
    from app.runtime.context import ensure_runtime_assembly_state
    from app.runtime.invoker import AgentInvocationRequest, _build_native_skill_catalog
    from app.runtime.prompt_builder import build_dynamic_prompt_suffix
    from app.runtime.session import SessionContext
    from app.services import historical_model_inputs
    from app.services.session_model_inputs import capture_context_message_origins
    from app.services.session_model_round import prepare_model_request
    from tests.services.test_session_semantic_history import _seal_round

    evidence = {"recoveries": []}
    native_context = invoker.build_agent_context
    native_runtime_context = invoker.build_agent_runtime_context
    recover = historical_model_inputs.recover_context_dependencies

    async def observe_recovery(*args, **kwargs):
        dependencies, complete = await recover(*args, **kwargs)
        if complete:
            evidence["recoveries"].extend(row for row in dependencies
                if row["source_kind"] == "agent_platform_skill_file")
            if any(row == evidence.get("old_leaf") for row in dependencies):
                print("R20A_HISTORICAL_RECOVERY", evidence["old_leaf"])
        return dependencies, complete

    monkeypatch.setattr(historical_model_inputs, "recover_context_dependencies", observe_recovery)

    async def retain_v1_witness(seed):
        monkeypatch.setattr(invoker, "build_agent_context", native_context)
        monkeypatch.setattr(invoker, "build_agent_runtime_context", native_runtime_context)
        request = AgentInvocationRequest(model=None, messages=[], agent_name="Package update parent",
            role_description="", agent_id=seed["agent_id"], tenant_id=seed["tenant_id"], user_id=seed["user_id"],
            session_context=SessionContext(session_id=str(seed["session_id"])))
        catalog = await _build_native_skill_catalog(request, seed["agent_id"], session_id=str(seed["session_id"]))
        state = ensure_runtime_assembly_state(request.session_context)
        sources = state.context_sources_for(agent_id=seed["agent_id"], content=catalog)
        old_leaf = next(row for row in sources if row["source_id"].split("\n")[1] == SKILL_PATH)
        ledger = []
        text = build_dynamic_prompt_suffix(skill_catalog=catalog, context_section_ledger=ledger)
        witness = _dynamic_suffix_notice(text, sections=ledger, sources=sources, unbound=())
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            await capture_context_message_origins(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                session_id=seed["session_id"], run_id=seed["run_id"], result_id=seed["run_id"], messages=[witness])
            bootstrap = await db.scalar(select(ConfigRevision).where(ConfigRevision.entity_id == seed["agent_id"],
                ConfigRevision.entity_type == "agent_bootstrap", ConfigRevision.version == 1))
            evidence.update(bootstrap_id=bootstrap.id, bootstrap_content=deepcopy(bootstrap.content),
                bootstrap_hash=bootstrap.content_hash, old_leaf=old_leaf)
            if update_at != "before_read":
                # Retain an actual old assistant answer in this session. A fresh
                # post-deploy round inherits its sources through semantic history.
                current_run = await db.get(RuntimeTask, seed["run_id"])
                current_status = current_run.status
                current_run.status = "completed"
                await db.flush()
                prior_run = uuid4()
                previous = RuntimeTask(id=prior_run, task_type="web_chat_turn", status="running",
                    tenant_id=seed["tenant_id"], parent_agent_id=seed["agent_id"], child_agent_id=seed["agent_id"],
                    parent_session_id=str(seed["session_id"]), child_session_id=str(seed["session_id"]),
                    root_session_id=str(seed["session_id"]), root_user_id=seed["user_id"], root_runtime_task_id=prior_run,
                    metadata_json={})
                db.add(previous)
                await db.flush()
                prior_notice = witness
                if update_at == "historical_before_parent":
                    prior_notice = _dynamic_suffix_notice(text, sections=ledger, sources=(),
                        unbound=(f"skill_file_unbound:{SKILL_PATH}",))

                async def retain_round():
                    request_id = await prepare_model_request(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                        session_id=seed["session_id"], run_id=prior_run, turn_id=f"turn-{prior_run.hex}", round_index=1,
                        messages=[prior_notice], tools=[], provider="openai", model="synthetic-no-provider")
                    await _seal_round(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                        session_id=seed["session_id"], run_id=prior_run, turn_id=f"turn-{prior_run.hex}", round_index=1,
                        provider_request_id=request_id, content="The original application Skill is available.")

                if update_at == "historical_before_parent":
                    # Freeze only the pre-withholding persisted historical input.
                    # Every post-update round and source validator remains native.
                    with legacy_model_requests(monkeypatch):
                        await retain_round()
                else:
                    await retain_round()
                previous.status, previous.completed_at = "completed", datetime.now(timezone.utc)
                await db.flush()
                current_run.status = current_status
            await db.commit()
        if update_at != "before_read":
            await deploy(seed)

    async def deploy(seed):
        original, updated = _deploy_bundle(monkeypatch, tmp_path)
        assert evidence["old_leaf"]["source_version"] == hashlib.sha256(original).hexdigest()
        await _push_default(owner_sessionmaker, updated)
        assert (seed["agent_root"] / SKILL_PATH).read_bytes() == updated
        evidence["updated_digest"] = hashlib.sha256(updated).hexdigest()

    if update_at == "before_read":
        seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
            child_success=True, before_parent=retain_v1_witness, before_notification=deploy)
    else:
        seed, _ = await _failed_child(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
            child_success=True, before_parent=retain_v1_witness)
    reader = ScriptedLLMClient([
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "package-result")],
            "finish_reason": "tool_calls"},
        {"content": "The colleague result remains readable after the package update."},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: reader)
    await _execute(owner_sessionmaker, seed["return_id"], "r20a-reader")
    read = (await _tool_results(owner_sessionmaker, seed["return_id"]))["package-result"]
    page = json.loads(read["content"])
    print("R20A_RESULT_READ", json.dumps(page, ensure_ascii=False))
    print("R20A_RECOVERED_OLD_LEAF", evidence["old_leaf"] in evidence["recoveries"])
    assert (evidence["old_leaf"] in evidence["recoveries"]) == (update_at == "historical_before_parent")
    assert page["status"] == "ok", page
    assert page["complete"] is True and RESULT in page["content"]
    assert page["dependency_coverage"]["complete"] is True
    assert any(RESULT in str(message.get("content")) for message in reader.requests[1]["messages"]
        if message.get("tool_call_id") == "package-result")
    async with owner_sessionmaker() as db:
        bootstrap = await db.get(ConfigRevision, evidence["bootstrap_id"])
        assert (bootstrap.content, bootstrap.content_hash) == (evidence["bootstrap_content"], evidence["bootstrap_hash"])
        rounds = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"])))
        assert rounds
        for model in rounds:
            inputs = model.model_request_snapshot_json["input_bindings"]
            assert inputs["dependency_coverage"]["complete"] is True
            expected = evidence["old_leaf"] if update_at == "before_read" else {
                **evidence["old_leaf"], "source_version": evidence["updated_digest"]}
            assert expected in inputs["dependencies"]
        # The current compiler must retain and bind v2 in the actual next round,
        # rather than hiding an unbound Skill catalog from the model request.
        current_rounds = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == seed["return_id"])))
        assert current_rounds
        for model in current_rounds:
            inputs = model.model_request_snapshot_json["input_bindings"]
            assert inputs["dependency_coverage"]["complete"] is True
            assert {**evidence["old_leaf"], "source_version": evidence["updated_digest"]} in inputs["dependencies"]
            assert SKILL_PATH in json.dumps(model.model_request_snapshot_json["wire_request"])


async def test_every_bundled_path_binds_current_bytes_and_reads_old_dependency(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.config import get_settings
    from app.models.user import User
    from app.services import skill_seeder, skill_source_receipts as sources
    from app.services.config_versioning import canonical_content_hash
    from app.services.skill_installation import collect_skill_package_files
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    backend = Path(sources.__file__).resolve().parents[2]
    current = {f"skills/{skill['folder_name']}/{file['path']}": file["content"].encode()
        for skill in skill_seeder.builtin_skill_definitions(pack_dirs=(backend / "packs",)) if skill.get("is_default")
        for file in skill["files"]}
    for folder in (backend / "hr_agent_template/skills").iterdir():
        if folder.is_dir() and (folder / "SKILL.md").is_file():
            current.update({f"skills/{folder.name}/{file['path']}": file["content"].encode()
                for file in collect_skill_package_files(folder)})
    bundled = sources._bundled_platform_skill_hashes()
    assert {path: hashlib.sha256(raw).hexdigest() for path, raw in current.items()} == bundled
    old = {path: hashlib.sha256(b"Previous application package\n" + raw).hexdigest() for path, raw in current.items()}
    for path, raw in current.items():
        file = seed["agent_root"] / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"Previous application package\n" + raw)
    assert sources.original_platform_skill_hashes(seed["agent_root"], bundled=old) == old
    content = {"schema": "hive.agent_platform_skills.v1", "files": old}
    revision_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(ConfigRevision(id=revision_id, entity_type="agent_platform_skill_bootstrap", entity_id=seed["agent_id"],
            tenant_id=seed["tenant_id"], version=1, content=content, content_hash=canonical_content_hash(content),
            change_source="platform_init"))
        await db.commit()
    for path, raw in current.items():
        (seed["agent_root"] / path).write_bytes(raw)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        inventory = await sources._platform_skill_sources(db, seed["tenant_id"], seed["agent_id"])
        assert inventory == {path: (str(revision_id), digest) for path, digest in bundled.items()}
        capture = sources.SkillInputCapture(seed["agent_id"], platform_files=inventory)
        actor = await db.get(User, seed["user_id"])
        for path, digest in bundled.items():
            capture.observe_file(path, digest)
            await sources.validate_skill_file_dependency(db, seed["tenant_id"], {
                "source_kind": "agent_platform_skill_file", "source_id": f"{seed['agent_id']}\n{path}\n{revision_id}",
                "source_version": old[path]}, user=actor, runtime_request=None)
        assert capture.snapshot()["unbound"] == []
        assert len(capture.snapshot()["sources"]) == len(bundled)
        print("R20A_ALL_BUNDLED_PATHS", len(bundled))
        retained = await db.get(ConfigRevision, revision_id)
        assert retained.content == content
        assert retained.content_hash == canonical_content_hash(content)


@pytest.mark.parametrize("writer", ["member", "registry", "custom_template", "registry_install"])
async def test_non_package_skill_bytes_remain_refused(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, writer,
):
    from app.config import get_settings
    from app.models.user import User
    from app.services import skill_seeder, skill_source_receipts as sources
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import ContentInputReceiptError
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr("app.services.agent_manager.settings", settings)
    monkeypatch.setattr(skill_seeder, "async_session", owner_sessionmaker)
    original, updated = _deploy_bundle(monkeypatch, tmp_path)
    original_digest = hashlib.sha256(original).hexdigest()
    revision_id = uuid4()
    content = {"schema": "hive.agent_platform_skills.v1", "files": {SKILL_PATH: original_digest}}
    async with owner_sessionmaker() as db:
        db.add(ConfigRevision(id=revision_id, entity_type="agent_platform_skill_bootstrap", entity_id=seed["agent_id"],
            tenant_id=seed["tenant_id"], version=1, content=content, content_hash=canonical_content_hash(content),
            change_source="platform_init"))
        await db.commit()
    arbitrary = updated + f"\nNon-package {writer} edit.\n".encode()
    path = seed["agent_root"] / SKILL_PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    if writer == "registry_install":
        from app.models.agent import Agent
        from app.services.ai_asset_adapters import project_skill
        from app.services.ai_assets import register_projection
        from app.services.config_versioning import save_revision

        await _push_default(owner_sessionmaker, updated)
        # The pre-install pass below must accept the old dependency against
        # the current package bytes, not against the untouched v1 file.
        assert path.read_bytes() == updated
        dependency = {"source_kind": "agent_platform_skill_file",
            "source_id": f"{seed['agent_id']}\n{SKILL_PATH}\n{revision_id}", "source_version": original_digest}
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            actor = await db.get(User, seed["user_id"])
            await sources.validate_skill_file_dependency(db, seed["tenant_id"], dependency,
                user=actor, runtime_request=None)
            skill = Skill(name="Installed MCP Package", folder_name="mcp-installer", tenant_id=seed["tenant_id"])
            skill.files = [SkillFile(path="SKILL.md", content=updated.decode())]
            db.add(skill)
            await db.flush()
            projection = project_skill(skill, owner_user_id=actor.id)
            await register_projection(db, projection, change_source="create", actor_user_id=actor.id)
            await save_revision(db, entity_type="registry_skill_original", entity_id=skill.id,
                tenant_id=seed["tenant_id"], content=projection.content, change_source="create",
                changed_by_user_id=actor.id)
            installed = await sources.install_registry_skill_with_sources(db, workspace=seed["agent_root"],
                agent=await db.get(Agent, seed["agent_id"]), skill=skill, actor=actor)
            assert installed["source_receipt"]["status"] == "captured"
            installation = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == seed["tenant_id"],
                ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.entity_id == sources._installation_id(
                    seed["agent_id"], "mcp-installer"), ConfigRevision.is_active.is_(True)))
            assert installation.content["origins"]
            assert installation.content["files"] == {SKILL_PATH: hashlib.sha256(updated).hexdigest()}
            await db.commit()
        assert path.read_bytes() == updated
        assert sources.original_platform_skill_hashes(seed["agent_root"]) == {
            SKILL_PATH: hashlib.sha256(updated).hexdigest()}
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            with pytest.raises(ContentInputReceiptError) as refused:
                await sources.validate_skill_file_dependency(db, seed["tenant_id"], dependency,
                    user=await db.get(User, seed["user_id"]), runtime_request=None)
            assert refused.value.code == "content_source_changed"
            assert SKILL_PATH not in await sources._platform_skill_sources(db, seed["tenant_id"], seed["agent_id"])
        return
    if writer == "registry":
        await _push_default(owner_sessionmaker, arbitrary)
    elif writer == "custom_template":
        from app.services.agent_manager import AgentManager

        custom = tmp_path / "custom-template"
        template = custom / SKILL_PATH
        template.parent.mkdir(parents=True)
        template.write_bytes(arbitrary)
        monkeypatch.setattr(settings, "AGENT_TEMPLATE_DIR", str(custom))
        # Copy from the configured custom template, keeping the existing
        # v1 source receipt to exercise the same historical byte check.
        import shutil
        shutil.copyfile(AgentManager()._template_dir() / SKILL_PATH, path)
    else:
        path.write_bytes(arbitrary)
    assert path.read_bytes() == arbitrary
    assert sources.original_platform_skill_hashes(seed["agent_root"]) == {}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        actor = await db.get(User, seed["user_id"])
        with pytest.raises(ContentInputReceiptError) as refused:
            await sources.validate_skill_file_dependency(db, seed["tenant_id"], {
                "source_kind": "agent_platform_skill_file", "source_id": f"{seed['agent_id']}\n{SKILL_PATH}\n{revision_id}",
                "source_version": original_digest}, user=actor, runtime_request=None)
        assert refused.value.code == "content_source_changed"
        assert await sources._platform_skill_sources(db, seed["tenant_id"], seed["agent_id"]) == {}
