"""Approval rechecks observed context without inventing missing creation history."""
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _event_count, _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("context_ref,scenario", [
    (ref, scenario)
    for ref in ("identity", "soul", "memory/self/self.md")
    for scenario in ("unchanged", "changed", "invalid_bootstrap", "inactive_bootstrap", "invalid_hr", "hr_draft")
    if ref != "soul" or scenario in ("unchanged", "changed")
])
async def test_approved_command_uses_current_employee_identity_without_hr_creation_history(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, context_ref, scenario,
):
    from app.config import get_settings
    from app.models.config_revision import ConfigRevision
    from app.models.hr_creation import HrCreationDraft
    from app.services import agent_tools, runtime_task_worker
    from app.services import agent_context
    from app.services.agent_context import agent_identity_content
    from app.services.approval_ticket import hash_tool_input
    from app.services.config_versioning import canonical_content_hash
    from app.services.session_model_inputs import _hash

    effects = tmp_path / "approved-effects.txt"
    seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="run_command",
        tool_arguments={"command": "rm workspace/approved-disposable.txt"})
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed.agent_id)
        root = tmp_path / str(agent.id)
        source_path = root / ("soul.md" if context_ref == "soul" else context_ref)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        body = "Observed employee context.\n"
        source_path.write_text(body)
        monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)
        monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path)
        kind = {"identity": "agent_identity", "soul": "agent_context_resource",
                "memory/self/self.md": "agent_resident_memory"}[context_ref]
        version = canonical_content_hash(agent_identity_content(agent.name, agent.role_description))
        if context_ref == "soul":
            version = (await agent_context.load_agent_context_resource(
                agent_id=agent.id, tenant_id=seed.tenant_id, resource_ref="soul")).sha256
        elif context_ref != "identity":
            version = hashlib.sha256(body.encode()).hexdigest()
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
        snapshot = dict(model.model_request_snapshot_json)
        snapshot["input_bindings"] = {
            "schema": "hive.model_input_bindings.v1",
            "dependencies": [{"source_kind": kind, "source_id": f"{agent.id}\n{context_ref}",
                "source_version": version}],
            "dependency_coverage": {"complete": True},
        }
        model.model_request_snapshot_json = snapshot
        model.model_request_hash = _hash(snapshot)
        if scenario == "changed":
            if context_ref == "identity":
                agent.role_description = "Changed after the model observed the employee."
            else:
                source_path.write_text("Changed after observation.\n")
        elif scenario in {"invalid_bootstrap", "inactive_bootstrap", "invalid_hr"}:
            db.add(ConfigRevision(tenant_id=seed.tenant_id, entity_id=agent.id,
                entity_type="agent_hr_bootstrap" if scenario == "invalid_hr" else "agent_bootstrap",
                version=1, is_active=scenario != "inactive_bootstrap", content={"schema": "invalid"},
                content_hash="0" * 64, change_source="user", changed_by_user_id=seed.user_id))
        elif scenario == "hr_draft":
            db.add(HrCreationDraft(tenant_id=seed.tenant_id, hr_agent_id=agent.id,
                session_id=seed.session_id, requested_by_user_id=seed.user_id,
                created_agent_id=agent.id, status="completed", blueprint_hash="0" * 64))
        await db.commit()

    async def execute(tool_name, arguments, **kwargs):
        await kwargs["pre_effect_callback"]({"arguments": dict(arguments)})
        with effects.open("a") as out:
            out.write("approved effect\n")
        kwargs["trace_metadata_sink"].update({
            "tool_decision": {"schema": "hive.tool_decision.v1", "decision_id": "approved-current-identity",
                "outcome": "allow", "input_hash": hash_tool_input(tool_name, arguments),
                "policy_snapshot_hash": "a" * 64},
            "tool_execution_frame": {"status": "completed", "output_hash": "b" * 64},
        })
        return "Approved command completed."

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr(agent_tools, "execute_session_permission_tool", execute)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", no_wakeup)
    path = f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve"
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = await client.post(path, json={"action": "allow_once"})
        assert first.status_code == 200, first.text
        replay = await client.post(path, json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert first.json()["run_id"] == str(seed.run_id)
        assert first.json()["run_status"] == "resumable"
        assert first.json()["result_event_id"] == replay.json()["result_event_id"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        task = await db.get(RuntimeTask, seed.run_id)
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        assert task.status == "resumable"
        assert await _event_count(db, invocation_id=invocation.id, item_kind="tool_result") == 1
        if scenario != "unchanged":
            assert not effects.exists()
            assert invocation.effect_state == "prepared_not_started"
            assert json.loads(event.metadata_json["v2_payload"]["content"])["reason_code"] == "approved_tool_execution_failed"
        else:
            assert effects.exists(), event.metadata_json["v2_payload"]["content"]
            assert effects.read_text().splitlines() == ["approved effect"]
            assert invocation.effect_state == "effect_committed"
            assert event.metadata_json["v2_payload"]["content"] == "Approved command completed."


@pytest.mark.parametrize("context_ref", ["identity", "soul", "memory/self/self.md"])
async def test_unrecorded_context_still_requires_current_reader_access(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, context_ref,
):
    from app.config import get_settings
    from app.models.user import User
    from app.services import agent_context
    from app.services.agent_context import agent_identity_content
    from app.services.config_versioning import canonical_content_hash
    from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies

    seed = await _seed_waiting_batch(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", tmp_path)
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", tmp_path)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed.agent_id)
        version = canonical_content_hash(agent_identity_content(agent.name, agent.role_description))
        kind = "agent_identity"
        if context_ref != "identity":
            path = tmp_path / str(agent.id) / ("soul.md" if context_ref == "soul" else context_ref)
            path.parent.mkdir(parents=True)
            path.write_text("Observed context.")
            version = hashlib.sha256(path.read_bytes()).hexdigest()
            kind = "agent_context_resource" if context_ref == "soul" else "agent_resident_memory"
        dependencies = [{"source_kind": kind, "source_id": f"{agent.id}\n{context_ref}", "source_version": version}]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        user = await db.get(User, seed.user_id)
        await validate_browser_input_dependencies(db, seed.tenant_id, user, dependencies)
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed.agent_id)
        successor_id = uuid.uuid4()
        db.add(User(id=successor_id, tenant_id=seed.tenant_id, username=f"successor-{successor_id.hex}",
            email=f"{successor_id.hex}@permission-runtime.test", password_hash="x", display_name="Successor"))
        await db.flush()
        agent.owner_user_id = successor_id
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        user = await db.get(User, seed.user_id)
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_browser_input_dependencies(db, seed.tenant_id, user, dependencies)
        assert error.value.code == "content_source_access_denied"
