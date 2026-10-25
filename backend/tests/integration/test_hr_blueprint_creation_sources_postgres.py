"""Confirmed blueprints retain their actual model sources without granting authority."""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult
from app.services import session_model_round
from app.services.delegation_input_origin import load_employee_creation_dependencies
from app.services.hr_creation_service import (
    HrCreationConflict,
    confirm_hr_creation_draft_record,
    has_creation_authorization,
)
from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task
from app.tools.handlers.hr import preview_agent_blueprint
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _preview(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, *, unbound=(), canonical=True):
    import app.database as database
    from app.kernel.engine import _context_message, _mid_run_items_to_user_messages
    from app.services.web_chat_runtime import _persist_tool_call

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Preview a private report employee for confirmation.")
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.name, agent.agent_class = "__system_hr__", "internal_system"
        await db.commit()
    arguments = {
        "name": "Report employee",
        "role_description": "Prepare reports from authorized sources.",
        "primary_users": ["The requesting owner"],
        "boundaries": "Draft only; do not send externally.",
        "core_outputs": ["Report"],
        "permission_scope": "self",
        "first_task_autostart": False,
        "focus_content": "Prepare a report when the owner supplies the authorized sources.",
    }
    call_id = "blueprint-preview-call"
    scope = {key: seed[key] for key in ("tenant_id", "agent_id", "session_id", "run_id", "turn_id")}
    if canonical:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert (
                await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"))
            ).one() == (
                False,
                False,
            )
            inputs = await session_model_round.bind_round_inputs(db, **scope, round_index=1)
            provider_id = await session_model_round.prepare_model_request(
                db,
                **scope,
                round_index=1,
                messages=[
                    _context_message(
                        role="system",
                        kind="platform_guidance",
                        content="Preview the requested employee.",
                        unbound=unbound,
                    ),
                    *_mid_run_items_to_user_messages(inputs),
                ],
                tools=[],
                provider="openai",
                model="fake-4.1",
            )
            await session_model_round.seal_model_response(
                db,
                **scope,
                round_index=1,
                provider_request_id=provider_id,
                response={
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "preview_agent_blueprint",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
            )
            await session_model_round.commit_sealed_model_round(
                db, **scope, round_index=1, provider_request_id=provider_id
            )
            await db.commit()
        event = {
            "name": "preview_agent_blueprint",
            "args": arguments,
            "tool_call_id": call_id,
            "provider_request_id": provider_id,
            "runtime_task_id": str(seed["run_id"]),
        }
        for status in ("running", "effect_started"):
            await _persist_tool_call(
                agent_id=seed["agent_id"],
                user_id=seed["user_id"],
                session_id=str(seed["session_id"]),
                data={**event, "status": status},
            )
    context = ToolExecutionContext(
        agent_id=seed["agent_id"],
        user_id=seed["user_id"],
        tenant_id=str(seed["tenant_id"]),
        session_id=str(seed["session_id"]),
        workspace=seed["agent_root"] / "workspace",
        runtime_task_id=str(seed["run_id"]),
        tool_call_id=call_id,
    )
    result = json.loads(
        await preview_agent_blueprint(ToolExecutionRequest("preview_agent_blueprint", arguments, context))
    )
    assert result.get("blueprint_id"), result
    return seed, uuid.UUID(result["blueprint_id"])


async def _confirm(db, seed, draft):
    confirm_hr_creation_draft_record(
        draft,
        confirming_user_id=seed["user_id"],
        blueprint_version=draft.blueprint_version,
        blueprint_hash=draft.blueprint_hash,
    )
    task = build_hr_provisioning_runtime_task(draft)
    db.add(task)
    await db.flush()
    draft.provisioning_task_id = task.id
    await db.flush()


@pytest.mark.parametrize("unbound", [(), ("original_preview_input_unavailable",)])
async def test_preview_sources_require_real_confirmation_and_preserve_coverage(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    unbound,
):
    seed, draft_id = await _preview(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, unbound=unbound)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        draft = await db.get(HrCreationDraft, draft_id)
        pointer = draft.preview_json.get("creation_origin")
        assert pointer and set(pointer) == {"source_id", "source_hash"}
        proof = (await db.get(InvocationSpan, uuid.UUID(pointer["source_id"]))).metadata_json["instruction_origin"]
        assert proof["schema"] == "hive.employee_blueprint_origin.v1"
        assert proof["blueprint_hash"] == draft.blueprint_hash
        assert proof["blueprint_version"] == draft.blueprint_version
        assert not has_creation_authorization(draft)
        assert "creation_authorization" not in draft.preview_json
        assert await load_employee_creation_dependencies(db, draft=draft) is None
        with pytest.raises(ValueError, match="authenticated confirmation"):
            build_hr_provisioning_runtime_task(draft)
        with pytest.raises(HrCreationConflict) as wrong_requester:
            confirm_hr_creation_draft_record(
                draft,
                confirming_user_id=uuid.uuid4(),
                blueprint_version=draft.blueprint_version,
                blueprint_hash=draft.blueprint_hash,
            )
        assert wrong_requester.value.code == "requester_mismatch"
        await _confirm(db, seed, draft)
        dependencies = await load_employee_creation_dependencies(db, draft=draft)
        assert dependencies is not None and dependencies[1] is (not unbound)
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed["run_id"]))
        assert dependencies[0] == model.model_request_snapshot_json["input_bindings"]["dependencies"]
        assert dependencies[0]
        with pytest.raises(IntegrityError, match="confirmed HR blueprint is immutable"):
            async with db.begin_nested():
                draft.blueprint_hash = "0" * 64
                await db.flush()
        await db.refresh(draft)
        source = await db.get(InvocationSpan, uuid.UUID(pointer["source_id"]))
        original_metadata = source.metadata_json
        source.metadata_json = {"instruction_origin": {**proof, "blueprint_hash": "0" * 64}}
        await db.flush()
        assert await load_employee_creation_dependencies(db, draft=draft) is None
        source.metadata_json = original_metadata
        await db.flush()
        assert await load_employee_creation_dependencies(db, draft=draft) == dependencies


async def test_preview_without_canonical_model_call_cannot_sign_historical_sources(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    seed, draft_id = await _preview(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, canonical=False)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        draft = await db.get(HrCreationDraft, draft_id)
        assert "creation_origin" not in draft.preview_json
        await _confirm(db, seed, draft)
        assert has_creation_authorization(draft)
        assert await load_employee_creation_dependencies(db, draft=draft) is None


@pytest.mark.parametrize("existing", [False, True])
async def test_system_hr_captures_skill_sources_only_at_original_installation(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    existing,
):
    import hashlib

    import app.database as database
    from app.api.agents import _sync_hr_agent_workspace, get_or_create_hr_agent
    from app.config import get_settings
    from app.models.config_revision import ConfigRevision
    from app.models.user import User
    from app.services import skill_seeder
    from app.services.agent_manager import agent_manager
    from app.services.skill_source_receipts import SkillInputCapture, load_skill_input_snapshot

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    monkeypatch.setattr(skill_seeder, "async_session", owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    await skill_seeder.seed_skills()
    if existing:
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            agent = await db.get(Agent, seed["agent_id"])
            agent.name, agent.agent_class = "__system_hr__", "internal_system"
            await db.commit()
        _sync_hr_agent_workspace(seed["agent_root"], force_identity_files=True)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        user = await db.get(User, seed["user_id"])
        result = await get_or_create_hr_agent(tenant_id=seed["tenant_id"], current_user=user, db=db)
        hr_id = uuid.UUID(result["id"])
        revision = await db.scalar(
            select(ConfigRevision).where(
                ConfigRevision.entity_id == hr_id, ConfigRevision.entity_type == "agent_platform_skill_bootstrap"
            )
        )
        if existing:
            assert hr_id == seed["agent_id"]
            assert revision is None
        else:
            assert revision is not None
            assert revision.change_source == "platform_init"
            assert revision.changed_by_user_id is None and revision.changed_by_agent_id is None
    capture = SkillInputCapture(hr_id)
    await load_skill_input_snapshot(capture, seed["tenant_id"])
    if existing:
        assert capture.platform_files == {}
    else:
        workspace = agent_manager._agent_dir(hr_id)
        files = sorted((workspace / "skills").glob("*/SKILL.md"))
        assert files and any(p.parent.name == "create-employee" for p in files)
        for path in files:
            relative = path.relative_to(workspace).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            assert capture.platform_files[relative] == (str(revision.id), digest)
            capture.observe_file(relative, digest)
        assert capture.snapshot()["unbound"] == []
