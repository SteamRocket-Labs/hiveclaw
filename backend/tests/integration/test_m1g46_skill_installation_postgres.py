"""Registry installation keeps original sources and rejects provenance resets."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def test_platform_witness_does_not_bless_custom_template_bytes(tmp_path):
    from pathlib import Path
    from app.services.skill_source_receipts import original_platform_skill_hashes

    path = "skills/mcp-installer/SKILL.md"
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    raw = (Path(__file__).resolve().parents[2] / "agent_template" / path).read_bytes()
    target.write_bytes(raw)
    assert original_platform_skill_hashes(tmp_path) == {path: hashlib.sha256(raw).hexdigest()}
    target.write_bytes(raw + b"\nCUSTOM_TEMPLATE_CONTENT")
    assert original_platform_skill_hashes(tmp_path) == {}


def test_selected_reads_keep_local_denial_and_unreceipted_copy_gaps():
    from types import SimpleNamespace
    from app.services.skill_source_receipts import SkillInputCapture

    agent, revision = uuid.uuid4(), uuid.uuid4()
    path, digest = "skills/source-probe/SKILL.md", hashlib.sha256(b"SELECTED_SOURCE").hexdigest()
    capture = SkillInputCapture(agent, files={path: (str(revision), digest)}, unverified={"skills/other/SKILL.md"})
    capture.observe_file(path, digest)
    assert capture.snapshot()["unbound"] == []
    assert capture.snapshot()["sources"][0]["source_id"] == f"{agent}\n{path}\n{revision}"
    capture.observe_file(path, hashlib.sha256(b"CHANGED_AFTER_SNAPSHOT").hexdigest())
    capture.observe_workspace({"unreceipted.txt": {"path": "workspace/unreceipted.txt", "sha256": digest}},
        SimpleNamespace(input_read=lambda _path: None))
    assert capture.snapshot()["unbound"] == [f"skill_file_unbound:{path}", "workspace_file_unbound:workspace/unreceipted.txt"]
    assert len(capture.snapshot()["sources"]) == 1


async def test_skill_capture_does_not_rebind_a_different_workspace_root(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services.skill_source_receipts import prepare_skill_input_capture
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    agent, user = uuid.uuid4(), uuid.uuid4()
    scope = WorkspaceAuthorityScope(agent_id=agent, user_id=user, root_session_id=None, allowed_paths=frozenset(),
        operator_view=False, authority_source="controlled-fixture")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "canonical"))
    request = ToolExecutionRequest(tool_name="load_skill", arguments={"name": "source-probe"}, context=ToolExecutionContext(
        agent_id=agent, user_id=user, tenant_id=str(uuid.uuid4()), workspace=tmp_path / "other", workspace_authority_scope=scope))
    await prepare_skill_input_capture(request)
    assert request.context.workspace_authority_scope.skill_inputs.snapshot()["unbound"] == ["skill_workspace_source_unverified"]
    assert scope.skill_inputs is None


def test_catalog_observer_keeps_selected_raw_bytes_and_ignores_hidden_entries(tmp_path):
    from app.skills.parser import SkillParser
    from app.skills.registry import SkillRegistry

    path = tmp_path / "SKILL.md"
    raw = b"---\r\nname: Visible\r\ndescription: SELECTED_DESCRIPTION\r\n---\r\nBody\r\n"
    path.write_bytes(raw)
    parser = SkillParser()
    selected = parser.parse_file(path, relative_path="skills/visible/SKILL.md")
    path.write_text("REPLACED_AFTER_PARSE")
    hidden = parser.parse_content("---\nname: Hidden\nhidden: true\n---\nPRIVATE_BODY", path=path,
        relative_path="skills/hidden/SKILL.md")
    registry = SkillRegistry()
    registry.register_many([selected, hidden])
    observed = []
    catalog = registry.render_catalog(source_observer=observed.append)
    assert "SELECTED_DESCRIPTION" in catalog and "PRIVATE_BODY" not in catalog
    assert observed == [selected] and selected.source_sha256 == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("role", ["member", "org_admin", "platform_admin"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
async def test_native_catalog_withholds_foreign_workspace_alias_metadata(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, role, alias,
):
    from app.models.user import User
    from app.runtime.invoker import AgentInvocationRequest, _build_native_skill_catalog
    from app.services import agent_context
    from app.services.workspace_resource_authority import register_workspace_path

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    await _import_original_skill(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    path = "workspace/private-skill/SKILL.md"
    target = seed["agent_root"] / path
    target.parent.mkdir(parents=True)
    target.write_text("---\nname: PRIVATE_SKILL_NAME\ndescription: PRIVATE_SKILL_DESCRIPTION\n---\nPrivate\n")
    linked = seed["agent_root"] / "skills/private-alias"
    if alias == "symlink":
        linked.symlink_to(target.parent, target_is_directory=True)
    else:
        linked.mkdir()
        (linked / "SKILL.md").hardlink_to(target)
    foreign = uuid.uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(User, seed["user_id"])).role = role
        db.add(User(id=foreign, tenant_id=seed["tenant_id"], username=f"private-{foreign.hex}",
            email=f"{foreign.hex}@fixture.test", password_hash="synthetic", display_name="Other owner"))
        await db.flush()
        await register_workspace_path(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], path=path,
            owner_user_id=foreign, root_session_id=None, source="controlled-fixture")
        await db.commit()
    request = AgentInvocationRequest(model=None, messages=[], agent_name="Catalog probe", role_description="",
        agent_id=seed["agent_id"], tenant_id=seed["tenant_id"], user_id=seed["user_id"])
    catalog = await _build_native_skill_catalog(request, seed["agent_id"], session_id=str(seed["session_id"]))
    assert "Controlled source probe." in catalog
    assert "PRIVATE_SKILL_NAME" not in catalog and "PRIVATE_SKILL_DESCRIPTION" not in catalog
    if alias == "symlink":
        from app.core.resource_authority import workspace_resource_id
        from app.models.security_audit import ResourcePermission

        async with owner_sessionmaker() as db:
            db.add(ResourcePermission(tenant_id=seed["tenant_id"], principal_type="user", principal_id=seed["user_id"],
                resource_type="workspace_file", resource_id=workspace_resource_id(seed["agent_id"], path), actions=["read"], conditions={}))
            await db.commit()
        granted = await _build_native_skill_catalog(request, seed["agent_id"], session_id=str(seed["session_id"]))
        assert "PRIVATE_SKILL_DESCRIPTION" in granted and "Controlled source probe." in granted


async def _import_original_skill(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, *, expected_written=2):
    import app.database as database
    from app.api import files, skills
    from app.config import get_settings
    from app.database import pin_rls_tenant_context
    from app.models.capability_policy import CapabilityPolicy
    from app.models.user import User

    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(files, "settings", get_settings())
    publisher_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        publisher = User(id=publisher_id, tenant_id=seed["tenant_id"], role="platform_admin", is_active=True,
            username=f"skill-author-{publisher_id.hex}", email=f"{publisher_id.hex}@fixture.test",
            display_name="Original skill author", password_hash="synthetic-only")
        db.add(publisher)
        for capability in ("agent.skill.read", "agent.skill.execute", "workspace.file.read"):
            if not await db.scalar(select(CapabilityPolicy).where(CapabilityPolicy.tenant_id == seed["tenant_id"],
                CapabilityPolicy.capability == capability)):
                db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    created = await skills.create_skill(skills.SkillCreateIn(name="Source Probe", folder_name="source-probe",
        description="Controlled source probe.", files=[skills.SkillFileIn(path="SKILL.md",
            content="---\nname: Source Probe\ndescription: Controlled source probe.\n---\nRun scripts/probe.py.\n"),
            skills.SkillFileIn(path="scripts/probe.py", content="from pathlib import Path\n"
                "Path('native-report.md').write_text('NATIVE_SKILL_REPORT')\nprint('NATIVE_SKILL_EXECUTED')\n")]),
        current_user=publisher)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        owner = await db.get(User, seed["user_id"])
        imported = await files.import_skill_to_agent(seed["agent_id"], files.ImportSkillBody(skill_id=created["id"]),
            current_user=owner, db=db)
        await db.commit()
    assert imported["files_written"] == expected_written
    return uuid.UUID(created["id"]), publisher, imported


@pytest.mark.parametrize("change", ["registry_revoked", "local_bytes", "unverified_predecessor", "rehashed_origin", "legacy_registry", "revoked_then_reimport"])
async def test_installed_skill_retains_current_source_authority(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, change,
):
    from app.api import files, skills
    from app.database import pin_rls_tenant_context
    from app.models.config_revision import ConfigRevision
    from app.models.skill import Skill, SkillFile
    from app.models.user import User
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies
    from sqlalchemy.exc import DBAPIError

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    skill_id, publisher, imported = await _import_original_skill(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
    assert imported["source_receipt"]["status"] == "captured"
    path = "skills/source-probe/scripts/probe.py"
    target = seed["agent_root"] / path
    refs = [{"source_kind": "agent_skill_file", "source_id": f"{seed['agent_id']}\n{path}\n{imported['source_receipt']['ref'].split(':', 1)[1]}",
        "source_version": hashlib.sha256(target.read_bytes()).hexdigest()}]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        owner = await db.get(User, seed["user_id"])
        await validate_browser_input_dependencies(db, seed["tenant_id"], owner, refs)
        if change == "registry_revoked":
            await skills.delete_skill(str(skill_id), current_user=publisher)
        elif change == "revoked_then_reimport":
            await skills.delete_skill(str(skill_id), current_user=publisher)
            _new_skill, _new_publisher, reimported = await _import_original_skill(
                seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, expected_written=0)
            assert reimported["source_receipt"]["source_status"] == "unverified"
        elif change in {"local_bytes", "unverified_predecessor"}:
            target.write_text("print('UNVERIFIED_REPLACEMENT')\n")
            if change == "unverified_predecessor":
                await files.import_skill_to_agent(seed["agent_id"], files.ImportSkillBody(skill_id=str(skill_id)),
                    current_user=owner, db=db)
                await db.commit()
        elif change == "legacy_registry":
            legacy = Skill(tenant_id=seed["tenant_id"], name="Legacy source", folder_name="source-probe")
            db.add(legacy)
            await db.flush()
            for file in (target, target.parent.parent / "SKILL.md"):
                db.add(SkillFile(skill_id=legacy.id, path=file.relative_to(target.parent.parent).as_posix(), content=file.read_text()))
            await db.flush()
            unverified = await files.import_skill_to_agent(seed["agent_id"], files.ImportSkillBody(skill_id=str(legacy.id)),
                current_user=owner, db=db)
            assert unverified["source_receipt"]["source_status"] == "unverified"
            await db.commit()
        else:
            with pytest.raises(DBAPIError, match="config revision snapshots are immutable"):
                async with owner_sessionmaker() as writer:
                    original = await writer.scalar(select(ConfigRevision).where(ConfigRevision.entity_type == "registry_skill_original",
                        ConfigRevision.entity_id == skill_id))
                    original.content = {**original.content, "input_origin": {"kind": "model_derived"}}
                    original.content_hash = canonical_content_hash(original.content)
                    await writer.commit()
            await validate_browser_input_dependencies(db, seed["tenant_id"], owner, refs)
            return
        for version in (refs[0]["source_version"], hashlib.sha256(target.read_bytes()).hexdigest()):
            refs[0]["source_version"] = version
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, seed["tenant_id"], owner, refs)
