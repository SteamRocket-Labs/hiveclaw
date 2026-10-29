"""Native plan bindings retain authority, wait states and explicit recovery."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.contracts import ExecutionAuthorityUnavailable
from app.models.plan_request import AgentPlanRequest
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.runtime.session import PlanModeState
from app.services.plan_mode_native_run import (
    AUTHORING_KEY,
    AUTHORING_SCHEMA,
    creation_metadata,
    load_system_plan_mode,
    reconcile_authoring,
)


class Records:
    def __init__(self, values):
        self.values = values
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, model, identifier):
        return self.values.get((model, identifier))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture
def native():
    tenant_id, agent_id, user_id, session_id, plan_id, run_id, input_id, command_id = (uuid4() for _ in range(8))
    mode = PlanModeState(
        active=True,
        plan_id=str(plan_id),
        intent_type="in_session_execution",
        original_request="Prepare a plan and wait for confirmation.",
        plan_file_path=f"workspace/plans/{plan_id}.plan.md",
        source="system_plan_run",
        reason="system_plan_run",
    )
    marker = {"plan_id": str(plan_id), "input_id": str(input_id)}
    plan = SimpleNamespace(
        id=plan_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=str(session_id),
        requested_by_user_id=user_id,
        status="planning",
        plan_version=1,
        plan_hash=None,
        runtime_task_id=uuid4(),
        intent_type=mode.intent_type,
        original_request=mode.original_request,
        metadata_json={
            AUTHORING_KEY: {
                "schema": AUTHORING_SCHEMA,
                "run_id": str(run_id),
                "input_id": str(input_id),
                "plan_version": 1,
                "generation": 1,
                "mode": mode.to_metadata(),
                "authorization_scopes": None,
            }
        },
    )
    task = SimpleNamespace(
        id=run_id,
        tenant_id=tenant_id,
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        root_user_id=user_id,
        status="running",
        metadata_json={
            AUTHORING_KEY: marker,
            "session_v2_input_id": str(input_id),
            "session_v2_command_id": str(command_id),
        },
    )
    session = SimpleNamespace(id=session_id, tenant_id=tenant_id, agent_id=agent_id)
    row = SimpleNamespace(
        id=input_id, tenant_id=tenant_id, session_id=session_id, command_id=command_id, status="queued"
    )
    command = SimpleNamespace(
        id=command_id,
        tenant_id=tenant_id,
        session_id=session_id,
        principal_type="user",
        principal_id=user_id,
        target_json={"runtime_metadata": {AUTHORING_KEY: marker}},
    )
    db = Records(
        {
            (AgentPlanRequest, plan_id): plan,
            (RuntimeTask, run_id): task,
            (SessionTurnInput, input_id): row,
            (SessionCommand, command_id): command,
        }
    )
    return SimpleNamespace(db=db, plan=plan, task=task, session=session, row=row, command=command, mode=mode)


def test_creation_metadata_cannot_mint_a_native_binding():
    supplied = {"title": "Keep this", AUTHORING_KEY: {"run_id": str(uuid4())}}
    original = deepcopy(supplied)
    assert creation_metadata(supplied) == {"title": "Keep this"}
    assert supplied == original


async def test_native_mode_preserves_the_draft_and_its_original_source_task(native):
    source_task = native.plan.runtime_task_id
    mode = await load_system_plan_mode(native.db, task=native.task, session=native.session)
    assert mode.to_metadata() == native.mode.to_metadata()
    assert mode.plan_id == str(native.plan.id)
    assert mode.plan_file_path == f"workspace/plans/{native.plan.id}.plan.md"
    assert native.plan.runtime_task_id == source_task != native.task.id


@pytest.mark.parametrize("tamper", ["principal", "run", "input", "version", "path", "tenant", "session"])
async def test_native_mode_rejects_a_crossed_or_tampered_binding(native, tamper):
    if tamper == "principal":
        native.command.principal_id = uuid4()
    elif tamper == "run":
        native.plan.metadata_json[AUTHORING_KEY]["run_id"] = str(uuid4())
    elif tamper == "input":
        native.task.metadata_json["session_v2_input_id"] = str(uuid4())
    elif tamper == "version":
        native.plan.plan_version += 1
    elif tamper == "path":
        native.plan.metadata_json[AUTHORING_KEY]["mode"]["plan_file_path"] = "workspace/unrelated.md"
    elif tamper == "tenant":
        native.plan.tenant_id = uuid4()
    else:
        native.task.parent_session_id = str(uuid4())
    with pytest.raises(ExecutionAuthorityUnavailable) as error:
        await load_system_plan_mode(native.db, task=native.task, session=native.session)
    assert error.value.code == "system_plan_authoring_unbound"


@pytest.mark.parametrize("status", ["pending", "running", "suspended", "resumable", "needs_reconciliation"])
async def test_live_or_unknown_authoring_is_never_relabelled_as_failed(native, status):
    native.task.status = status
    assert await reconcile_authoring(native.db, native.plan) == status
    assert native.plan.status == "planning"
    assert native.plan.plan_hash is None
    assert native.plan.metadata_json[AUTHORING_KEY]["generation"] == 1


@pytest.mark.parametrize("status", ["pending", "running", "suspended", "resumable", "needs_reconciliation"])
async def test_regenerate_cannot_replay_live_or_unknown_authoring(native, monkeypatch, status):
    from app.services import plan_mode_service as service_module

    native.task.status = status
    service = service_module.PlanModeService()

    async def tenant(_identifier):
        return native.plan.tenant_id

    async def load(_db, _identifier):
        return native.plan

    monkeypatch.setattr(service_module, "resolve_tenant_for_plan", tenant)
    monkeypatch.setattr(service_module, "tenant_scoped_session", lambda *_args, **_kwargs: native.db)
    monkeypatch.setattr(service, "_load", load)
    original_binding = deepcopy(native.plan.metadata_json[AUTHORING_KEY])
    with pytest.raises(service_module.PlanConflictError) as error:
        await service.mark_plan_authoring(native.plan.id)
    assert error.value.error_code == (
        "plan_authoring_needs_reconciliation" if status == "needs_reconciliation" else "plan_authoring_in_progress"
    )
    current = native.plan.metadata_json[AUTHORING_KEY]
    assert current["input_id"] == original_binding["input_id"]
    assert current["run_id"] == original_binding["run_id"]
    assert current["generation"] == 1
    assert "launch_requested" not in current


@pytest.mark.parametrize("status", ["completed", "failed", "killed", "cancelled", "skipped"])
async def test_unsubmitted_terminal_authoring_has_explicit_generation_recovery(native, status):
    native.task.status = status
    assert await reconcile_authoring(native.db, native.plan) == status
    assert native.plan.status == "planning_failed"
    assert native.plan.plan_hash is None


async def test_ready_plan_keeps_its_confirmable_version_and_hash(native):
    native.task.status = "completed"
    native.plan.status = "awaiting_confirmation"
    native.plan.plan_hash = "canonical-plan-hash"
    await reconcile_authoring(native.db, native.plan)
    assert native.plan.status == "awaiting_confirmation"
    assert native.plan.plan_version == 1
    assert native.plan.plan_hash == "canonical-plan-hash"


def confirmed_handoff(native):
    from app.services.session_model_inputs import _hash

    native.plan.status = "confirmed"
    native.plan.plan_hash = "confirmed-plan-hash"
    native.plan.handoff_status = "queued"
    native.plan.handoff_payload = {"reason": "native_input_queued", "input_id": str(native.row.id)}
    native.row.status = "applied"
    native.row.target_run_id = native.task.id
    native.task.task_type = "web_chat_turn"
    native.command.command_kind = "queue_next_turn"
    native.command.request_json = {"content": "Execute the confirmed plan."}
    native.command.target_json = {
        "runtime_metadata": {
            "approved_plan_id": str(native.plan.id),
            "approved_plan_version": 1,
            "approved_plan_hash": native.plan.plan_hash,
        }
    }
    native.command.request_hash = _hash(
        {"command_kind": native.command.command_kind, "request": native.command.request_json}
    )
    native.command.target_hash = _hash(native.command.target_json)


@pytest.mark.parametrize("status", ["pending", "completed", "needs_reconciliation"])
async def test_handoff_reads_back_its_run_without_claiming_execution_succeeded(native, status):
    from app.services.plan_mode_session_handoff import reconcile_native_handoff

    confirmed_handoff(native)
    native.task.status = status
    assert await reconcile_native_handoff(native.db, native.plan) is True
    assert native.plan.handoff_status == "completed"
    assert native.plan.handoff_payload["runtime_task_id"] == str(native.task.id)
    assert native.plan.handoff_payload["runtime_task_status"] == status
    assert native.plan.plan_version == 1
    assert native.plan.plan_hash == "confirmed-plan-hash"
    assert await reconcile_native_handoff(native.db, native.plan) is False


@pytest.mark.parametrize("tamper", ["owner", "tenant", "hash"])
async def test_handoff_projection_rejects_an_unrelated_or_changed_receipt(native, tamper):
    from app.services.plan_mode_session_handoff import reconcile_native_handoff

    confirmed_handoff(native)
    if tamper == "owner":
        native.command.principal_id = uuid4()
    elif tamper == "tenant":
        native.task.tenant_id = uuid4()
    else:
        native.plan.plan_hash = "changed-after-confirmation"
    assert await reconcile_native_handoff(native.db, native.plan) is False
    assert native.plan.handoff_status == "queued"
    assert "runtime_task_id" not in native.plan.handoff_payload


@pytest.mark.parametrize("state", ["queued", "applied", "foreign_owner"])
async def test_native_handoff_retry_only_reconciles_existing_input(native, monkeypatch, state):
    from app.services import plan_mode_service as module

    confirmed_handoff(native)
    native.plan.plan_json = {"handoff": {"target": "continue_current_session"}}
    native.task.status = "needs_reconciliation"
    if state == "queued":
        native.row.status = "queued"
        native.row.target_run_id = None
    elif state == "foreign_owner":
        native.command.principal_id = uuid4()
    before_payload = deepcopy(native.plan.handoff_payload)
    before_metadata = deepcopy(native.plan.metadata_json)
    before_command = deepcopy(native.command.__dict__)

    async def tenant(_identifier):
        return native.plan.tenant_id

    async def load(_db, _identifier):
        return native.plan

    async def unexpected_effect(**_kwargs):
        raise AssertionError("A queued native handoff must not consume another lease or dispatch again")

    service = module.PlanModeService()
    service.register_handoff_handler("continue_current_session", unexpected_effect)
    monkeypatch.setattr(module, "resolve_tenant_for_plan", tenant)
    monkeypatch.setattr(module, "tenant_scoped_session", lambda *_args, **_kwargs: native.db)
    monkeypatch.setattr(module, "consume_plan_authorization_lease", unexpected_effect)
    monkeypatch.setattr(service, "_load", load)
    result = await service.handoff_confirmed_plan(plan_id=native.plan.id)
    assert result is native.plan
    assert native.db.commits == 1
    assert result.handoff_status == ("completed" if state == "applied" else "queued")
    assert native.task.status == "needs_reconciliation"
    assert native.plan.metadata_json == before_metadata
    assert native.command.__dict__ == before_command
    if state == "applied":
        assert result.handoff_payload["runtime_task_id"] == str(native.task.id)
        assert result.handoff_payload["runtime_task_status"] == "needs_reconciliation"
    else:
        assert result.handoff_payload == before_payload
