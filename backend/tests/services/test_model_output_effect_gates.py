"""Permission withholding refuses effects while ordinary source withholding stays local."""

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import pytest

from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_model_inputs import _hash, _model_dependencies
from app.services.session_tool_runtime import _sha256


@pytest.fixture(params=["session_permission_frame_unbound", "source_revoked"])
def model(request):
    snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [],
        "dependency_coverage": {"complete": True}, "messages": [{"status": "withheld", "code": request.param}]}}
    result = NS(id=uuid4(), tenant_id=uuid4(), session_id=uuid4(), run_id=uuid4(), provider_request_id="provider-1",
        state="round_committed", model_request_snapshot_json=snapshot, model_request_hash=_hash(snapshot))
    result.seal_json = {"result_id": str(result.id), "run_id": str(result.run_id),
        "provider_request_id": result.provider_request_id, "semantic_content": "Done.", "content_hash": _sha256("Done.")}
    assert _model_dependencies(result) == ([], True)
    return result


def denied(model):
    return model.model_request_snapshot_json["input_bindings"]["messages"][0]["code"] == "session_permission_frame_unbound"


@pytest.mark.parametrize("source", ["session_terminal_outcome", "original_work_permission", "original_work_permission_response"])
async def test_group_delivery_checks_output_permission(model, monkeypatch, source):
    from app.models.runtime_task import RuntimeTask
    from app.services import shared_group_authority as groups
    from app.services.channel_delivery_outbox import ChannelDeliveryRetryableError

    session = NS(id=uuid4(), tenant_id=model.tenant_id, agent_id=uuid4(), group_subject_id="oc-test")
    config, user = NS(id=uuid4()), NS(id=uuid4())
    item = NS(tenant_id=session.tenant_id, agent_id=session.agent_id, runtime_task_id=model.run_id,
        user_id=user.id, channel_config_id=config.id, terminal_status="completed", session_id=session.id,
        delivery_target={"channel": "feishu", "receive_id_type": "chat_id", "receive_id": "oc-test", "chat_id": "oc-test"},
        metadata={"source": source, "model_result_id": str(model.id), "original_notice_id": str(uuid4())})
    task = NS(id=model.run_id, tenant_id=session.tenant_id, task_type="business_task", parent_agent_id=session.agent_id,
        child_agent_id=session.agent_id, parent_session_id=str(session.id), child_session_id=str(model.session_id), root_user_id=user.id)
    original = NS(tenant_id=item.tenant_id, runtime_task_id=task.id, agent_id=item.agent_id, session_id=session.id,
        metadata_json={"source": "original_work_permission", "model_result_id": str(model.id)})
    db = NS(scalar=AsyncMock(return_value=model), get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs:
        task if kind is RuntimeTask else original))
    monkeypatch.setattr(groups, "load_current_group_recipient_users", AsyncMock(return_value=(config, [user])))
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    if denied(model):
        with pytest.raises(ChannelDeliveryRetryableError, match="group_output_sources_unavailable"):
            await groups.validate_shared_group_delivery(db, item=item, session=session)
    else:
        assert await groups.validate_shared_group_delivery(db, item=item, session=session) is None


@pytest.mark.parametrize("gate", ["frame", "sources", "read"])
async def test_delegated_permission_keeps_read_separate_from_authority(model, monkeypatch, gate):
    from app.core.execution_context import ExecutionPrincipal
    from app.runtime.ccplus_contracts import build_permission_profile, permission_profile_snapshot, permission_profile_snapshot_hash
    from app.services import delegation_permission_runtime as delegation

    parent, child, user = uuid4(), uuid4(), uuid4()
    session = NS(id=model.session_id, runtime_task_id=model.run_id, user_id=user)
    profile = build_permission_profile({})
    original = {"permission_profile": permission_profile_snapshot(profile),
        "execution_identity": {"identity_type": "delegated_user", "identity_id": str(user), "label": "Owner"},
        "execution_principal": ExecutionPrincipal(tenant_id=model.tenant_id, source_agent_id=parent, requester_user_id=user).to_evidence(),
        "session_id": str(session.id), "parent_session_id": str(uuid4()), "runtime_task_id": str(model.run_id)}
    task = NS(id=model.run_id, tenant_id=model.tenant_id, task_type="delegation", parent_agent_id=parent, child_agent_id=child,
        child_session_id=str(session.id), root_user_id=user, metadata_json={"authority_snapshot": original,
            "authority_policy_hash": permission_profile_snapshot_hash(original["permission_profile"])})
    invocation = NS(id=uuid4(), run_id=model.run_id, tool_name="write_file", provider_tool_use_id="call-1",
        effective_arguments_json={"path": "workspace/report.md", "content": "Done."}, permission_authority_snapshot_hash="a" * 64)
    authority = NS(tenant_id=model.tenant_id, agent_id=child, principal_id=user)
    db = NS(get=AsyncMock(return_value=task))
    monkeypatch.setattr("app.services.shared_group_authority._permission_execution_matches", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.delegation_context_sources._facts", AsyncMock(return_value=NS()))
    monkeypatch.setattr("app.services.capability_gate._resolve_capability", lambda _name: "workspace.write")
    monkeypatch.setattr(delegation, "_validate_input_dependencies", AsyncMock())

    async def invoke():
        if gate == "frame":
            return await delegation.delegated_permission_execution(db, authority=authority, session=session,
                invocation=invocation, profile=profile, control_id=uuid4(), model=model)
        frame = NS(principal=ExecutionPrincipal.from_evidence(original["execution_principal"]), execution_identity=None,
            delegation_token=None, budget_run_id=None, permission_profile=profile)
        return await delegation.validate_permission_input_sources(db, authority=authority, session=session,
            invocation=invocation, model=model, execution={"authority_frame": frame} if gate == "sources" else {})

    if denied(model) and gate != "read":
        with pytest.raises(ContentInputReceiptError) as error:
            await invoke()
        assert error.value.code == "delegation_permission_sources_unavailable"
    else:
        result = await invoke()
        if gate == "frame":
            assert result["authority_frame"].delegation_token is result["delegation_token"]
            assert result["delegation_token"].child_agent_id == child
        else:
            assert result is None


async def test_compute_refuses_permission_withholding(model, monkeypatch):
    from app.services.code_execution import opensandbox_provider as provider
    from app.services.execution_environments import EnvironmentError
    from app.services.session_tool_runtime import _sha256

    request = NS(tool_name="execute_code", arguments={"language": "python", "code": "print(1)"}, context=NS(
        workspace_authority_scope=NS(execution_inputs=NS(snapshot=lambda: {"observed": True, "unbound": [], "sources": []}))))
    invocation = NS(session_id=model.session_id, run_id=model.run_id, provider_request_id=model.provider_request_id,
        effective_args_hash=None, args_hash=_sha256({"tool_name": request.tool_name, "arguments": request.arguments}))
    db = NS(scalars=AsyncMock(return_value=NS(one_or_none=lambda: invocation)), scalar=AsyncMock(return_value=model))

    @asynccontextmanager
    async def transaction(*_args, **_kwargs):
        yield db

    monkeypatch.setattr(provider, "tenant_scoped_session", transaction)
    monkeypatch.setattr("app.services.content_input_receipts.validate_runtime_input_dependencies", AsyncMock())
    scope = NS(tenant_id=model.tenant_id, session_id=str(model.session_id), runtime_task_id=model.run_id,
        invocation_key="call-1", runtime_request=request, _authority=AsyncMock())
    if denied(model):
        with pytest.raises(EnvironmentError) as error:
            await provider._dependencies(scope, [])
        assert error.value.code == "compute_model_inputs_unavailable"
    else:
        assert await provider._dependencies(scope, []) == []


async def test_learned_model_certification_refuses_permission_withholding(model, monkeypatch):
    from app.models.session_v2 import SessionModelResult
    from app.services import learned_skill_sources as skills

    session = NS(tenant_id=model.tenant_id)
    db = NS(get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs: model if kind is SessionModelResult else session))
    monkeypatch.setattr("app.services.content_input_receipts._authorize_session_input_source", AsyncMock())
    monkeypatch.setattr(skills, "_validate_input_dependencies", AsyncMock())
    if denied(model):
        with pytest.raises(ContentInputReceiptError) as error:
            await skills.validate_model_source(db, model.tenant_id, skills.model_source(model))
        assert error.value.code == "learned_model_source_unavailable"
    else:
        assert await skills.validate_model_source(db, model.tenant_id, skills.model_source(model)) == (model, session)


async def test_skill_recovery_certification_refuses_permission_withholding(model):
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.skill_candidate_operations import validate_recovery

    agent, user, operation = uuid4(), uuid4(), uuid4()
    invocation = NS(id=uuid4(), tenant_id=model.tenant_id, session_id=model.session_id, run_id=model.run_id,
        provider_request_id=model.provider_request_id, tool_name="reconcile_skill_candidate", effect_state="effect_committed",
        effective_arguments_json={"action": "retry", "operation_id": str(operation)}, effective_args_hash="args", authority_snapshot_hash="authority")
    task = NS(tenant_id=model.tenant_id, root_user_id=user, parent_agent_id=agent, child_agent_id=agent,
        parent_session_id=str(model.session_id), child_session_id=None)
    row = NS(entity_id=operation, content={"authority": {"owner_user_id": str(user)}, "recovery": {
        "invocation_id": str(invocation.id), "model_result_id": str(model.id), "args_hash": "args",
        "authority_hash": "authority", "model_request_hash": model.model_request_hash, "dependencies": []}})
    db = NS(get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs:
        invocation if kind is SessionToolInvocation else model if kind is SessionModelResult else task))
    if denied(model):
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_recovery(db, row, tenant_id=model.tenant_id, agent_id=agent)
        assert error.value.code == "learned_skill_recovery_unverified"
    else:
        assert await validate_recovery(db, row, tenant_id=model.tenant_id, agent_id=agent) is None


async def test_approved_workspace_result_certification_refuses_permission_withholding(model, monkeypatch):
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.runtime_task import RuntimeTask
    from app.services import content_input_receipts as receipts

    agent, user, approval_id = uuid4(), uuid4(), uuid4()
    invocation = NS(id=uuid4(), tenant_id=model.tenant_id, session_id=model.session_id, run_id=model.run_id,
        provider_request_id=model.provider_request_id)
    original = NS(tenant_id=model.tenant_id, parent_agent_id=agent, parent_session_id=str(model.session_id))
    job = NS(status="completed", task_type="approval_execution", tenant_id=model.tenant_id,
        parent_agent_id=agent, parent_session_id=original.parent_session_id, root_user_id=user, metadata_json={"approval_id": str(approval_id)})
    path, digest, ref = "workspace/report.md", "a" * 64, "workspace-output:proof"
    receipt_id = uuid5(invocation.id, f"workspace-content:{path}")
    receipt = NS(id=receipt_id, tenant_id=model.tenant_id, carrier_kind="workspace_artifact",
        carrier_ref=receipts.workspace_artifact_ref(agent, path), content_sha256=digest, dependencies_json=[])
    span = NS(user_id=user, metadata_json={"workspace_execution_evidence": {},
        "native_content_receipts": {"status": "captured", "receipts": {path: str(receipt_id)}}})
    approval = NS(id=approval_id, agent_id=agent, tenant_id=model.tenant_id, requested_by=user, tool_name="office_document_create",
        status="approved", execution_status="succeeded", consumed_at="consumed", execution_task_id=uuid4(),
        execution_envelope={"session_id": str(model.session_id)}, execution_result="Done.", normalized_arguments={},
        execution_receipt={"runtime_evidence": {"workspace_execution_evidence": {"status": "captured", "receipt_ref": ref}}})
    db = NS(scalar=AsyncMock(return_value=model), get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs:
        NS(id=model.session_id) if kind is ChatSession else job if kind is RuntimeTask else span if kind is InvocationSpan else receipt))
    monkeypatch.setattr("app.services.session_approval.group_approval_origin", AsyncMock(return_value=(original, invocation, NS())))
    monkeypatch.setattr(receipts, "load_workspace_execution_evidence", AsyncMock(return_value=(ref, {path: digest})))
    monkeypatch.setattr(receipts, "_workspace_execution_input_sources", lambda _proof: [])
    assert await receipts.load_approved_workspace_result(db, approval=approval) == (None if denied(model) else ([], {receipt_id}))


@pytest.mark.parametrize("group", [False, True])
async def test_artifact_delivery_rechecks_selection_permission(model, monkeypatch, group):
    from app.models.organization_identity import ContentInputReceipt
    from app.models.chat_session import ChatSession
    from app.services import content_input_receipts as receipts
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    agent, receipt_id = uuid4(), uuid4()
    artifact = NS(id=uuid4(), tenant_id=model.tenant_id, agent_id=agent, session_id=model.session_id,
        runtime_task_id=model.run_id, path="workspace/report.md", snapshot_json={
            "content_input_receipt_id": str(receipt_id), "content_hash": "a" * 64, "selection_model_result_id": str(model.id)})
    receipt = NS(tenant_id=model.tenant_id, carrier_kind="workspace_artifact", content_sha256="a" * 64,
        carrier_ref=receipts.workspace_artifact_ref(agent, artifact.path), id=receipt_id)
    session = NS(id=model.session_id, tenant_id=model.tenant_id, agent_id=agent, audience_kind="group")
    db = NS(get_transaction=lambda: None, scalar=AsyncMock(return_value=artifact), get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs:
        receipt if kind is ContentInputReceipt else session if kind is ChatSession else model))
    # This unit test isolates selection permission; PostgreSQL graph tests
    # cover the immutable catalog query and its real row projections.
    monkeypatch.setattr(receipts, "_preload_execution_sources", AsyncMock())
    monkeypatch.setattr(receipts, "_validate_workspace_receipt_inputs", AsyncMock())
    monkeypatch.setattr(receipts, "_validate_input_dependencies", AsyncMock())
    monkeypatch.setattr("app.services.shared_group_authority.load_current_group_recipient_users",
        AsyncMock(return_value=(NS(), [NS(id=uuid4())])))
    item = NS(tenant_id=model.tenant_id, agent_id=agent, session_id=session.id, runtime_task_id=model.run_id,
        artifact_ids=[artifact.id], metadata={})

    async def invoke():
        if group:
            return await ChannelDeliveryOutboxService._authorize_artifact_part(NS(), db, item, artifact.id)
        return await receipts.validate_artifact_content_receipt(db, model.tenant_id, NS(id=uuid4()), artifact)

    if denied(model):
        with pytest.raises(ContentInputReceiptError) as error:
            await invoke()
        assert error.value.code == "artifact_selection_unavailable"
    else:
        assert await invoke() is (artifact if group else None)
