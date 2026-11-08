"""Private conversations cannot become shared resident memory or approved input."""

import hashlib
import inspect
import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent, AgentPermission
from app.models.chat_session import ChatSession
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies
from tests.integration.test_permission_command_environment import _command, _result, _url
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _commit(**kwargs):
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch

    result = apply_t3_consolidation_patch(**kwargs)
    return await result if inspect.isawaitable(result) else result


def _member(db, tenant_id, user_id):
    db.add(User(id=user_id, tenant_id=tenant_id, username=f"audience-{user_id.hex}",
        email=f"{user_id.hex}@example.test", password_hash="x", display_name="Audience member", role="member"))


@pytest.mark.parametrize("audience", [
    "private_owner", "owner_grant", "company", "department", "other_user", "other_member",
    "group", "channel_group", "shared_after_package", "shared_after_staging", "private_then_shared",
])
async def test_conversation_consolidation_requires_a_private_owner_audience(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, audience,
):
    from app.config import get_settings
    from app.memory.profile_plane import load_resident_memory
    from app.memory.t0.ledger import append_t0_session_event
    from app.memory.t2.segment_package import build_t2_segment_package
    from app.memory.t3_consolidation import stage_pending_t3_consolidation_job
    from app.memory.t3_platform_gate import file_sha256
    from tests.memory.test_t2_segment_package_builder import _summary_xml, _labels_xml, _approved_review_xml
    from tests.memory.test_t3_consolidation_platform_gate import _accepted_review_for, _entry_patch

    seed = await _seed_waiting_batch(owner_sessionmaker)
    other = uuid.uuid4()
    source_member = other if audience in {"company", "other_member"} else seed.user_id
    private_marker = "PRIVATE_MEMBER_METHOD_MOONSTONE"
    async with owner_sessionmaker() as db:
        _member(db, seed.tenant_id, other)
        await db.flush()
        session = await db.get(ChatSession, seed.session_id)
        session.user_id = source_member
        session.audience_kind = "group" if audience == "group" else "private"
        if audience == "group":
            session.group_installation_ref = "fixture-installation"
            session.group_subject_id = "fixture-group"
        if audience == "channel_group":
            session.source_channel = "feishu"
            session.delivery_target_json = {"chat_type": "group", "chat_id": "fixture-group"}
        if audience in {"company", "department", "other_user", "owner_grant"}:
            scope = {"other_user": "user", "owner_grant": "user"}.get(audience, audience)
            db.add(AgentPermission(tenant_id=seed.tenant_id, agent_id=seed.agent_id, scope_type=scope,
                scope_id=seed.user_id if audience == "owner_grant" else other if scope != "company" else None,
                access_level="manage" if audience == "other_user" else "use"))
        await db.commit()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    event = append_t0_session_event(agent_id=seed.agent_id, session_id=seed.session_id, tenant_id=seed.tenant_id,
        actor_id=source_member, event_type="user_message", role="user", content=private_marker,
        source="web", metadata={"audience_kind": "private"}, data_root=tmp_path)

    async def summary(bundle):
        assert private_marker in str(bundle["t0_events"])
        return _summary_xml(bundle).replace("测试事实。", private_marker)

    async def labels(bundle, _summary):
        return _labels_xml(bundle)

    async def review(bundle, _summary, _labels):
        return _approved_review_xml(package_id=bundle["package_id"], ref=bundle["source_refs"][0]["uri"])

    t2 = await build_t2_segment_package(agent_id=seed.agent_id, tenant_id=seed.tenant_id,
        session_id=seed.session_id, t0_segment_id=event.segment_id, data_root=tmp_path,
        summary_agent=summary, learning_brain=labels, memory_gate=review)
    assert t2.status == "committed", t2.issues

    async def share():
        async with owner_sessionmaker() as db:
            db.add(AgentPermission(tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                scope_type="company", access_level="use"))
            await db.commit()

    if audience == "shared_after_package":
        await share()
    staged = stage_pending_t3_consolidation_job(agent_id=seed.agent_id, data_root=tmp_path)
    assert staged.status == "staged", staged.issues
    bundle = json.loads((staged.job_dir / "source_bundle.json").read_text())
    assert private_marker in str(bundle["source_packages"])
    if audience == "shared_after_staging":
        await share()
    ref = f"t2://session/{seed.session_id}/segment/{event.segment_id}"
    target = tmp_path / str(seed.agent_id) / "memory/self/self.md"
    entry = f"### Learned private method\n<!-- id: private-method -->\n{private_marker}"
    patch_md = _entry_patch("memory/self/self.md", file_sha256(target), "private-method", entry, refs=[ref])
    review_md = _accepted_review_for(ref)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        result = await _commit(agent_id=seed.agent_id, data_root=tmp_path, job_id=staged.job_id,
            revised_patch_md=patch_md, review_md=review_md)
    allowed = audience in {"private_owner", "owner_grant", "private_then_shared"}
    private_marker_in_shared_self = target.exists() and private_marker in target.read_text()
    resident = load_resident_memory(agent_id=seed.agent_id, data_root=tmp_path, include_overlay=False)
    assert private_marker_in_shared_self is allowed, result
    assert (private_marker in resident.text) is allowed
    assert result.status == ("committed" if allowed else "held"), result.issues
    # T2 remains available for authorized session recall and is not absorbed on denial.
    manifest = json.loads((t2.package_dir / "manifest.json").read_text())
    if not allowed:
        assert "t3_absorbed_by" not in manifest
        assert manifest["package_status"] == "reviewed"
    if audience == "private_then_shared":
        await share()
        before = target.read_bytes()
        replay = await _commit(agent_id=seed.agent_id, data_root=tmp_path, job_id=staged.job_id,
            revised_patch_md=patch_md, review_md=review_md)
        assert replay.status == "committed", replay.issues
        assert target.read_bytes() == before
        assert private_marker in load_resident_memory(
            agent_id=seed.agent_id, data_root=tmp_path, include_overlay=False).text


async def test_explicit_memory_consolidation_is_unchanged_for_a_shared_bot(owner_sessionmaker, tmp_path):
    from app.memory.t3_platform_gate import file_sha256
    from tests.memory.test_t3_consolidation_platform_gate import (
        _accepted_review_for, _entry_patch, _write_explicit_overlay_entry,
    )

    seed = await _seed_waiting_batch(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        db.add(AgentPermission(tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            scope_type="company", access_level="use"))
        await db.commit()
    entry_id = _write_explicit_overlay_entry(tmp_path, seed.agent_id)
    ref = f"explicit://memory/{entry_id}"
    target = tmp_path / str(seed.agent_id) / "memory/self/self.md"
    entry = "### Explicit method\n<!-- id: explicit-method -->\nKeep the owner's explicit instruction."
    result = await _commit(agent_id=seed.agent_id, data_root=tmp_path, job_id="explicit-shared",
        revised_patch_md=_entry_patch("memory/self/self.md", file_sha256(target), "explicit-method", entry, refs=[ref]),
        review_md=_accepted_review_for(ref))
    assert result.status == "committed", result.issues
    assert entry in target.read_text()


@pytest.mark.parametrize("scenario", ["unchanged", "changed", "invalid_bootstrap", "inactive_bootstrap", "invalid_hr", "hr_draft"])
async def test_legacy_collaborator_uses_the_existing_no_history_rule(
    owner_sessionmaker, app_user_sessionmaker, scenario,
):
    from app.models.config_revision import ConfigRevision
    from app.models.hr_creation import HrCreationDraft
    from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section
    from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model

    seed = await _seed_waiting_batch(owner_sessionmaker)
    peer_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(Agent(id=peer_id, tenant_id=seed.tenant_id, creator_id=seed.user_id, owner_user_id=seed.user_id,
            name="Legacy peer", role_description="Existing same-owner collaborator", status="idle"))
        await db.commit()
        directory = await build_a2a_collaboration_read_model(db, seed.agent_id)
        assert [row["id"] for row in directory["same_owner_agents"]] == [str(peer_id)]
        observed = build_a2a_collaborators_section(directory, max_chars=None)
        dependency = {"source_kind": "agent_context_resource", "source_id": f"{seed.agent_id}\na2a-collaborators",
            "source_version": hashlib.sha256(observed.encode()).hexdigest()}
        if scenario == "changed":
            (await db.get(Agent, peer_id)).name = "Changed after observation"
        elif scenario in {"invalid_bootstrap", "inactive_bootstrap", "invalid_hr"}:
            db.add(ConfigRevision(tenant_id=seed.tenant_id, entity_id=peer_id,
                entity_type="agent_hr_bootstrap" if scenario == "invalid_hr" else "agent_bootstrap",
                version=1, is_active=scenario != "inactive_bootstrap", content={"schema": "invalid"},
                content_hash="0" * 64, change_source="user", changed_by_user_id=seed.user_id))
        elif scenario == "hr_draft":
            db.add(HrCreationDraft(tenant_id=seed.tenant_id, hr_agent_id=seed.agent_id,
                session_id=seed.session_id, requested_by_user_id=seed.user_id,
                created_agent_id=peer_id, status="completed", blueprint_hash="0" * 64))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        user = await db.get(User, seed.user_id)
        if scenario == "unchanged":
            await validate_browser_input_dependencies(db, seed.tenant_id, user, [dependency])
        else:
            with pytest.raises(ContentInputReceiptError):
                await validate_browser_input_dependencies(db, seed.tenant_id, user, [dependency])


@pytest.mark.parametrize("requester", ["successor", "former"])
async def test_real_approved_command_rechecks_transferred_no_history_bot(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, requester,
):
    from app.runtime.prompt_sections.a2a_collaborators import build_a2a_collaborators_section
    from app.services.a2a_collaboration_policy import build_a2a_collaboration_read_model
    from app.services.agent_context import agent_identity_content
    from app.services.code_execution import service
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.config_versioning import canonical_content_hash
    from app.services.session_model_inputs import _hash

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    other = uuid.uuid4()
    former_id = other if requester == "successor" else seed.user_id
    successor_id = seed.user_id if requester == "successor" else other
    peer_id = uuid.uuid4()
    path = tmp_path / str(seed.agent_id) / "memory/self/self.md"
    path.parent.mkdir(parents=True)
    path.write_text("Owner-observed legacy resident content.\n")
    async with owner_sessionmaker() as db:
        _member(db, seed.tenant_id, other)
        await db.flush()
        agent = await db.get(Agent, seed.agent_id)
        agent.creator_id = agent.owner_user_id = former_id
        db.add(Agent(id=peer_id, tenant_id=seed.tenant_id, creator_id=successor_id, owner_user_id=successor_id,
            name="Successor legacy peer", status="idle"))
        await db.commit()
        agent.owner_user_id = successor_id
        await db.commit()
        directory = await build_a2a_collaboration_read_model(db, seed.agent_id)
        observed = build_a2a_collaborators_section(directory, max_chars=None)
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
        snapshot = dict(model.model_request_snapshot_json)
        snapshot["input_bindings"] = {"schema": "hive.model_input_bindings.v1", "dependencies": [
            {"source_kind": "agent_identity", "source_id": f"{seed.agent_id}\nidentity",
                "source_version": canonical_content_hash(agent_identity_content(agent.name, agent.role_description))},
            {"source_kind": "agent_resident_memory", "source_id": f"{seed.agent_id}\nmemory/self/self.md",
                "source_version": hashlib.sha256(path.read_bytes()).hexdigest()},
            {"source_kind": "agent_context_resource", "source_id": f"{seed.agent_id}\na2a-collaborators",
                "source_version": hashlib.sha256(observed.encode()).hexdigest()},
        ], "dependency_coverage": {"complete": True}}
        model.model_request_snapshot_json, model.model_request_hash = snapshot, _hash(snapshot)
        await db.commit()

    async def operation(*_args, **_kwargs):
        with effect.open("a") as output:
            output.write("approved effect\n")
        return CodeExecutionResult(stdout="approved effect", exit_code=0)

    monkeypatch.setattr(service, "_execute_selected_command", operation)
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = await client.post(_url(seed), json={"action": "allow_once"})
        if requester == "former":
            assert first.status_code == 403, first.text
            assert not effect.exists()
            return
        assert first.status_code == 200, first.text
        replay = await client.post(_url(seed), json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert first.json()["run_id"] == replay.json()["run_id"] == str(seed.run_id)
        assert first.json()["result_event_id"] == replay.json()["result_event_id"]
    invocation, task, content = await _result(app_user_sessionmaker, seed)
    assert effect.exists(), content
    assert effect.read_text().splitlines() == ["approved effect"]
    assert invocation.effect_state == "effect_committed" and task.status == "resumable"
