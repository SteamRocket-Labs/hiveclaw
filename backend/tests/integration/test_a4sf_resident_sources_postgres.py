"""Native defaults and recorded consolidation retain their source rules."""

import hashlib
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.session_v2 import SessionModelResult
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.user import User
from app.services.agent_manager import AgentManager
from app.services.content_input_receipts import ContentInputReceiptError, validate_browser_input_dependencies
from tests.integration.test_m1f_assistant_binding_postgres import (
    _actor, _agent_files, _client, _model, _user, builtin_defaults,  # noqa: F401
)
from tests.integration.test_permission_command_environment import _command, _result, _url
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.memory.test_t3_consolidation_platform_gate import (
    _accepted_review_for, _entry_patch, _write_reviewed_t2_package,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "builtin_defaults")


def _resident_dependency(agent_id, path):
    return {"source_kind": "agent_resident_memory", "source_id": f"{agent_id}\nmemory/self/self.md",
        "source_version": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("filled", [False, True])
async def test_renamed_default_assistant_keeps_its_original_resident_rule(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, filled,
):
    from app.services.employee_creation_sources import load_known_native_default_context

    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    tag = user_id.hex
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Default source test", slug=f"default-source-{tag}"))
        await db.flush()
        _user(db, id=user_id, tenant_id=tenant_id, username=f"default-{tag}", email=f"{tag}@example.test")
        model = await _model(db, tenant_id, label=tag)
        db.add(TenantSetting(tenant_id=tenant_id, key="default_model_id", value={"model_id": str(model.id)}))
        await db.commit()
    initialize = AgentManager.initialize_agent_files

    async def old_initialize(self, db, agent, *args, **kwargs):
        kwargs.pop("original_author_id", None)
        return await initialize(self, db, agent, *args, **kwargs)

    monkeypatch.setattr(AgentManager, "initialize_agent_files", old_initialize)
    async with _client(app_user_sessionmaker, {"actor": _actor(id=user_id, tenant_id=tenant_id, role="member")}) as client:
        opened = await client.post("/api/assistant/open", json={"idempotency_key": tag})
        assert opened.status_code == 200, opened.text
        agent_id = uuid.UUID(opened.json()["agent"]["id"])
    path = AgentManager()._agent_dir(agent_id) / "memory/self/self.md"
    if filled:
        path.write_text(path.read_text() + "\nUnrecorded learned content.\n")
    dependency = _resident_dependency(agent_id, path)
    for renamed in (False, True):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant_id)
            agent = await db.get(Agent, agent_id)
            if renamed:
                agent.name = "Renamed assistant"
                agent.role_description = "Edited role description"
                await db.commit()
            user = await db.get(User, user_id)
            if filled:
                with pytest.raises(ContentInputReceiptError, match="exact bootstrap witness"):
                    await validate_browser_input_dependencies(db, tenant_id, user, [dependency])
            else:
                await validate_browser_input_dependencies(db, tenant_id, user, [dependency])
            known = await load_known_native_default_context(
                db, tenant_id=tenant_id, agent_id=agent_id, ref="memory/self/self.md")
            assert known is not None, "Editable labels must not change the native-default source rule."


@pytest.mark.parametrize("history", [True, False])
@pytest.mark.parametrize("write", ["consolidated", "unrecorded", "other_writer", "changed_after_observation"])
async def test_approved_action_accepts_recorded_platform_consolidation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, history, write,
):
    from app.memory.t3_platform_gate import apply_t3_consolidation_patch, file_sha256
    from app.models.config_revision import ConfigRevision
    from app.services.agent_asset_transaction import AgentAssetTransaction
    from app.services.code_execution import service
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.session_model_inputs import _hash

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    (effect.parent / "approved-disposable.txt").unlink()
    effect.parent.rmdir()
    effect.parent.parent.rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed.agent_id)
        await AgentManager().initialize_agent_files(db, agent, original_author_id=seed.user_id if history else None)
        bootstrap = await db.scalar(select(ConfigRevision).where(
            ConfigRevision.entity_type == "agent_bootstrap", ConfigRevision.entity_id == seed.agent_id))
        assert (bootstrap is not None) is history
        await db.commit()
    root = tmp_path / str(seed.agent_id)
    (root / "workspace/approved-disposable.txt").write_text("Disposable test input.\n")
    path = root / "memory/self/self.md"
    entry = "### Learned method\n<!-- id: learned-method -->\nCheck the result after executing a command."
    if write == "unrecorded":
        path.write_text(path.read_text() + "\n" + entry + "\n")
    elif write == "other_writer":
        with AgentAssetTransaction(root, operation="unrelated_writer") as transaction:
            transaction.stage_text("memory/self/self.md", path.read_text() + "\n" + entry + "\n")
            transaction.commit()
    else:
        _write_reviewed_t2_package(tmp_path, seed.agent_id, session_id=str(seed.session_id))
        ref = f"t2://session/{seed.session_id}/segment/seg-1"
        result = await apply_t3_consolidation_patch(agent_id=seed.agent_id, data_root=tmp_path, job_id="learned-method",
            tenant_id=seed.tenant_id,
            revised_patch_md=_entry_patch("memory/self/self.md", file_sha256(path), "learned-method", entry, refs=[ref]),
            review_md=_accepted_review_for(ref))
        assert result.status == "committed", result.issues
        assert entry in path.read_text()
    dependency = _resident_dependency(seed.agent_id, path)
    if write == "changed_after_observation":
        path.write_text(path.read_text() + "\nChanged after the model observed it.\n")
    async with owner_sessionmaker() as db:
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.run_id == seed.run_id))
        snapshot = dict(model.model_request_snapshot_json)
        snapshot["input_bindings"] = {"schema": "hive.model_input_bindings.v1", "dependencies": [dependency],
            "dependency_coverage": {"complete": True}}
        model.model_request_snapshot_json = snapshot
        model.model_request_hash = _hash(snapshot)
        await db.commit()

    async def operation(*_args, **_kwargs):
        with effect.open("a") as out:
            out.write("approved effect\n")
        return CodeExecutionResult(stdout="approved effect", exit_code=0)

    monkeypatch.setattr(service, "_execute_selected_command", operation)
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = await client.post(_url(seed), json={"action": "allow_once"})
        replay = await client.post(_url(seed), json={"action": "allow_once"})
        assert first.status_code == replay.status_code == 200, (first.text, replay.text)
        assert first.json()["run_id"] == str(seed.run_id)
        assert first.json()["result_event_id"] == replay.json()["result_event_id"]
    invocation, task, content = await _result(app_user_sessionmaker, seed)
    assert task.status == "resumable"
    # No-history bots retain the owner's observation-time rule; recorded history
    # additionally requires the bootstrap or a real consolidation write.
    allowed = write == "consolidated" or (not history and write != "changed_after_observation")
    if allowed:
        assert effect.exists(), content
        assert effect.read_text().splitlines() == ["approved effect"]
        assert invocation.effect_state == "effect_committed"
    else:
        assert not effect.exists(), content
        assert invocation.effect_state == "prepared_not_started"
        assert "approved_tool_execution_failed" in content
