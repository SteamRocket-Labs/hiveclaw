"""Output gates retain permission refusal without withholding safe round inputs."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4, uuid5

import pytest

from app.services.content_input_receipts import ContentInputReceiptError
from app.services.session_model_inputs import _hash, _model_dependencies


@pytest.fixture
def model():
    snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [],
        "dependency_coverage": {"complete": True}, "messages": [
            {"status": "withheld", "code": "session_permission_frame_unbound"}]}}
    result = NS(id=uuid4(), tenant_id=uuid4(), session_id=uuid4(), run_id=uuid4(), state="round_committed",
        model_request_snapshot_json=snapshot, model_request_hash=_hash(snapshot), seal_json={"semantic_content": "Done."})
    assert _model_dependencies(result) == ([], True)
    return result


@pytest.mark.parametrize("case", ["permission", "ordinary", "not_withheld", "incomplete", "tampered", "unsealed", "missing"])
def test_output_check_preserves_input_dependencies(model, case):
    from app.services.session_model_inputs import model_output_dependencies

    bindings = model.model_request_snapshot_json["input_bindings"]
    dependency = {"source_kind": "chat_input", "source_id": str(uuid4()), "source_version": "v1"}
    bindings["dependencies"] = [dependency]
    if case == "ordinary":
        bindings["messages"][0]["code"] = "source_revoked"
    elif case == "not_withheld":
        bindings["messages"][0]["status"] = "bound"
    elif case == "incomplete":
        bindings["dependency_coverage"]["complete"] = False
    elif case == "unsealed":
        model.state = "prepared"
    model.model_request_hash = _hash(model.model_request_snapshot_json)
    if case == "tampered":
        bindings["messages"] = []
    if case == "missing":
        model = None
    dependencies, complete = _model_dependencies(model)
    assert model_output_dependencies(model) == (dependencies, complete and case != "permission")
    if case in {"permission", "ordinary", "not_withheld"}:
        assert dependencies == [dependency]
        assert complete is True


async def test_read_artifact_gate_refuses_withheld_permission(model, monkeypatch):
    from app.models.invocation_span import InvocationSpan
    from app.models.session_v2 import SessionModelResult
    from app.services.chat_artifact_delivery import materialize_declared_read_artifacts

    invocation_id, receipt_id = uuid4(), uuid4()
    dependencies = [{"source_kind": "session_tool_invocation", "source_id": str(invocation_id), "source_version": "read"},
        {"source_kind": "workspace_artifact", "source_id": str(receipt_id), "source_version": "a" * 64}]
    model.model_request_snapshot_json["input_bindings"]["dependencies"] = dependencies
    model.model_request_hash = _hash(model.model_request_snapshot_json)
    invocation = NS(id=invocation_id, effective_arguments_json={"path": "workspace/report.md"},
        tool_name="read_file", provider_tool_use_id="read-1")
    receipt = NS(id=receipt_id, content_sha256="a" * 64)
    span = NS(metadata_json={"workspace_input_evidence": {"content_input_receipt_id": str(receipt_id)}})
    db = NS(get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs:
        model if kind is SessionModelResult else span if kind is InvocationSpan else receipt),
        scalars=AsyncMock(return_value=[invocation]))
    monkeypatch.setattr("app.services.session_model_inputs._origin_dependencies", AsyncMock(return_value=(dependencies, True)))
    monkeypatch.setattr("app.services.web_chat_runtime._ensure_tool_result_artifact_message", AsyncMock())
    monkeypatch.setattr("app.services.chat_artifact_delivery.create_chat_artifacts_for_message",
        AsyncMock(return_value=[{"type": "artifact", "artifact_id": str(uuid4()), "path": "workspace/report.md"}]))
    task = NS(id=model.run_id, tenant_id=model.tenant_id, parent_session_id=str(model.session_id),
        parent_agent_id=uuid4(), root_user_id=uuid4(), root_external_principal_id=None)
    assert await materialize_declared_read_artifacts(db, task=task, model_result_id=model.id,
        paths=["workspace/report.md"]) == []


async def test_native_channel_result_gate_refuses_withheld_permission(model, monkeypatch):
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    session = NS(id=model.session_id, tenant_id=model.tenant_id, agent_id=uuid4(), audience_kind="private")
    item = NS(tenant_id=model.tenant_id, session_id=session.id, agent_id=session.agent_id, runtime_task_id=model.run_id,
        text="Done.", terminal_status="completed", metadata={"source": "session_terminal_outcome", "outcome_id": str(uuid4())})
    db = NS(get=AsyncMock(return_value=session), execute=AsyncMock(return_value=NS(first=lambda: (NS(), model))))
    service = NS(_private_delivery_reader=AsyncMock(return_value=NS(id=uuid4())))
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    with pytest.raises(ContentInputReceiptError) as error:
        await ChannelDeliveryOutboxService._authorize_native_result_text(service, db, item)
    assert error.value.code == "native_result_sources_unavailable"


async def test_business_channel_result_gate_refuses_withheld_permission(model, monkeypatch):
    from app.models.runtime_task import RuntimeTask
    from app.services.channel_delivery_outbox import ChannelDeliveryOutboxService

    agent, parent_session, user = uuid4(), uuid4(), uuid4()
    run = NS(id=model.run_id, tenant_id=model.tenant_id, task_type="business_task", parent_agent_id=agent,
        parent_session_id=str(parent_session), root_user_id=user, status="completed", child_session_id=str(model.session_id),
        metadata_json={"outcome": {"result": "Done.", "summary": "Done.", "response_complete_payload": {}}})
    session = NS(id=parent_session, tenant_id=model.tenant_id, agent_id=agent, audience_kind="private")
    item = NS(tenant_id=model.tenant_id, runtime_task_id=run.id, session_id=parent_session, agent_id=agent,
        user_id=user, terminal_status="completed", text="Done.", metadata={})
    db = NS(get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs: run if kind is RuntimeTask else session),
        scalar=AsyncMock(return_value=model))
    service = NS(_private_delivery_reader=AsyncMock(return_value=NS(id=user)))
    monkeypatch.setattr("app.services.direct_invocation_terminal_boundary_processor._latest_run_event",
        AsyncMock(return_value=NS(id=uuid4(), event_type="result_commit.round_committed")))
    monkeypatch.setattr("app.services.direct_invocation_terminal_boundary_processor._validate_response_payload", AsyncMock())
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    with pytest.raises(ContentInputReceiptError) as error:
        await ChannelDeliveryOutboxService._authorize_business_result_text(service, db, item)
    assert error.value.code == "business_result_sources_unavailable"


async def test_original_progress_gate_refuses_withheld_permission(model, monkeypatch):
    from app.services.original_work_progress import read_original_work_progress

    user = NS(id=uuid4())
    run = NS(tenant_id=model.tenant_id, task_type="delegation", status="running", root_user_id=user.id, parent_agent_id=uuid4(),
        child_agent_id=uuid4(), child_session_id=str(model.session_id))
    session = NS(id=uuid4(), user_id=user.id)
    monkeypatch.setattr("app.services.original_work_permission_notices._original_work",
        AsyncMock(return_value=(session, None, None, None)))
    monkeypatch.setattr("app.core.permissions.authorize_session_action", AsyncMock())
    monkeypatch.setattr("app.services.original_work_progress._committed_progress",
        AsyncMock(return_value=(model, NS(id=uuid4()), "Done.")))
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    with pytest.raises(ContentInputReceiptError) as error:
        await read_original_work_progress(NS(), run=run, user=user)
    assert error.value.code == "progress_sources_unverified"


async def test_business_question_gate_refuses_withheld_permission(model, monkeypatch):
    from app.services.business_task_question_delivery import authorize_business_question_notice

    business = NS(id=uuid4(), agent_id=uuid4(), created_by=uuid4(), root_session_id=uuid4(), authority_state="owned")
    item = NS(delivery_kind="interactive_prompt", artifact_ids=[], text="Question?", metadata={
        "question_item_id": str(uuid4()), "business_task_id": str(business.id), "model_result_id": str(model.id)})
    monkeypatch.setattr("app.services.business_task_question_delivery._waiting_question",
        AsyncMock(return_value=(business, NS(), model)))
    monkeypatch.setattr("app.services.business_task_question_delivery._question_text", lambda *_args: "Question?")
    monkeypatch.setattr("app.core.resource_authority.authorize_resource_action", AsyncMock())
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    with pytest.raises(ContentInputReceiptError) as error:
        await authorize_business_question_notice(NS(), item=item, run=NS(tenant_id=model.tenant_id),
            session=NS(id=uuid4(), audience_kind="private"), user=NS(id=uuid4()))
    assert error.value.code == "business_question_sources_unavailable"


async def test_memory_write_gate_refuses_withheld_permission(model, monkeypatch):
    from app.services.explicit_memory_authority import _writer_input

    request = NS(tool_name="save_memory", arguments={"content": "Remember."}, context=NS(
        tenant_id=str(model.tenant_id), runtime_task_id=str(model.run_id), tool_call_id="call-1", turn_id="turn-1"))
    invocation = NS(effective_args_hash=None, args_hash=_hash({"tool_name": request.tool_name, "arguments": request.arguments}))
    db = NS(execute=AsyncMock(return_value=NS(all=lambda: [(invocation, model)])))
    monkeypatch.setattr("app.services.explicit_memory_authority._actor",
        AsyncMock(return_value=(NS(), NS(), NS(id=model.session_id))))
    monkeypatch.setattr("app.services.explicit_memory_authority.validate_runtime_input_dependencies", AsyncMock())
    with pytest.raises(ContentInputReceiptError) as error:
        await _writer_input(db, request)
    assert error.value.code == "explicit_memory_input_unverified"


async def test_delegated_instruction_gate_refuses_withheld_permission(model, monkeypatch):
    from app.services import delegation_input_origin as delegation

    parent_agent, child_agent, owner, invocation_id = (uuid4() for _ in range(4))
    proof = {"schema": "hive.delegation_instruction_origin.v1", "content": "Do the task.",
        "parent_invocation_id": str(invocation_id), "parent_model_result_id": str(model.id),
        "parent_model_request_hash": model.model_request_hash, "recipient_agent_id": str(child_agent),
        "requester_user_id": str(owner), "call_id": "call-1"}
    digest = _hash(proof)
    source_id = uuid5(invocation_id, f"delegation-instruction:{digest}")
    pointer = {"source_id": str(source_id), "source_hash": digest}
    row = NS(tenant_id=model.tenant_id, agent_id=parent_agent, session_id=str(model.session_id), runtime_task_id=model.run_id,
        span_type="delegation_input", status="captured", user_id=owner, name="model_authored_instruction",
        span_id=f"delegation-instruction:{source_id}", metadata_json={"instruction_origin": proof})
    request = NS(conversation_messages=[{"a2a_instruction_origin": pointer}], parent_agent_id=str(parent_agent),
        parent_session_id=str(model.session_id), owner_id=owner, target=NS(id=child_agent))
    monkeypatch.setattr(delegation, "_parent_call", AsyncMock(return_value=(NS(id=invocation_id), model, {"message": "Do the task."})))
    origin = await delegation.load_delegation_instruction_origin(NS(get=AsyncMock(return_value=row)),
        request=request, tenant_id=model.tenant_id, content="Do the task.")
    assert origin is not None
    assert origin.unbound == ("delegation_parent_inputs_unbound",)


async def test_employee_instruction_gate_refuses_withheld_permission(model, monkeypatch):
    from app.models.invocation_span import InvocationSpan
    from app.services import delegation_input_origin as delegation

    invocation_id, draft_id, owner, agent = (uuid4() for _ in range(4))
    original = {"first_task": "Do the task."}
    proof = {"schema": "hive.employee_blueprint_origin.v1", "draft_id": str(draft_id), "requester_user_id": str(owner),
        "blueprint_version": 1, "blueprint_hash": "blueprint", "tool_arguments_hash": _hash(original),
        "parent_invocation_id": str(invocation_id), "parent_model_result_id": str(model.id),
        "parent_model_request_hash": model.model_request_hash, "call_id": "call-1"}
    digest = _hash(proof)
    source_id = uuid5(invocation_id, f"delegation-instruction:{digest}")
    row = NS(tenant_id=model.tenant_id, agent_id=agent, session_id=str(model.session_id), user_id=owner,
        span_type="delegation_input", status="captured", runtime_task_id=model.run_id, metadata_json={"instruction_origin": proof})
    draft = NS(id=draft_id, tenant_id=model.tenant_id, hr_agent_id=agent, session_id=model.session_id,
        requested_by_user_id=owner, provisioning_task_id=uuid4(), blueprint_version=1, blueprint_hash="blueprint",
        preview_json={"creation_origin": {"source_id": str(source_id), "source_hash": digest}})
    db = NS(get=AsyncMock(side_effect=lambda kind, *_args, **_kwargs: row if kind is InvocationSpan else NS()))
    monkeypatch.setattr("app.services.hr_creation_service.direct_creation_authorization", lambda _draft: None)
    monkeypatch.setattr("app.services.hr_provisioning_runtime._runtime_authority_issues", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(delegation, "_parent_call", AsyncMock(return_value=(NS(id=invocation_id), model, original)))
    assert await delegation.load_employee_creation_dependencies(db, draft=draft) == ([], False)


async def test_native_task_artifact_gate_refuses_withheld_permission(model, monkeypatch):
    from app.services.business_task_artifacts import capture_business_task_artifacts

    owner, agent, root = uuid4(), uuid4(), uuid4()
    run = NS(id=model.run_id, tenant_id=model.tenant_id, parent_agent_id=agent, root_user_id=owner, status="completed",
        metadata_json={"outcome": {"result": "Done.", "response_complete_payload": {}}})
    monkeypatch.setattr("app.services.web_chat_runtime._terminal_artifact_candidate_paths", lambda _text: ["workspace/report.md"])
    monkeypatch.setattr("app.services.business_task_artifacts._execution_session", AsyncMock(return_value=(NS(id=model.session_id), root)))
    monkeypatch.setattr("app.services.direct_invocation_terminal_boundary_processor._latest_run_event",
        AsyncMock(return_value=NS(id=uuid4(), event_type="result_commit.round_committed")))
    monkeypatch.setattr("app.services.direct_invocation_terminal_boundary_processor._validate_response_payload", AsyncMock())
    monkeypatch.setattr("app.services.chat_artifact_delivery._native_artifact_for_final_message",
        AsyncMock(return_value=(True, NS(id=uuid4(), owner_user_id=owner, root_session_id=root))))
    await capture_business_task_artifacts(NS(scalar=AsyncMock(return_value=model)), run)
    assert run.metadata_json["business_artifact_ids"] == []
    assert run.metadata_json["business_artifact_delivery"]["files"] == [
        {"path": "workspace/report.md", "reason_code": "business_artifact_sources_unavailable"}]


@pytest.mark.parametrize("permission_withheld", [True, False])
async def test_native_write_publication_preserves_working_file_and_tool_result(
    model, monkeypatch, tmp_path, permission_withheld,
):
    from app.models.session_v2 import SessionToolInvocation
    from app.services import session_tool_runtime as runtime

    if not permission_withheld:
        model.model_request_snapshot_json["input_bindings"]["messages"][0]["code"] = "source_revoked"
        model.model_request_hash = _hash(model.model_request_snapshot_json)
    assert _model_dependencies(model) == ([], True)
    model.turn_id, model.round_id = "turn-1", "round-1"
    agent, invocation_id = uuid4(), uuid4()
    target = tmp_path / "workspace/report.md"
    target.parent.mkdir()
    target.write_text("Working file remains available.")
    invocation = SessionToolInvocation(id=invocation_id, tenant_id=model.tenant_id, session_id=model.session_id,
        run_id=model.run_id, invocation_item_id=uuid4(), provider_request_id="provider-1", provider_tool_use_id="write-1",
        tool_name="write_file", provider_arguments_json={"path": "workspace/report.md", "content": target.read_text()},
        effect_state="effect_started", version=1)
    db = NS(scalar=AsyncMock(return_value=invocation), get=AsyncMock(return_value=NS(
        tenant_id=model.tenant_id, agent_id=agent, user_id=uuid4(), external_principal_id=None)))
    monkeypatch.setattr(runtime.chat_transcript, "lock_transcript_session", AsyncMock())
    monkeypatch.setattr(runtime, "_result_for_invocation", AsyncMock(return_value=model))
    monkeypatch.setattr(runtime, "_settlement_from_evidence", lambda *_args, **_kwargs:
        ("completed", "success", False, {"outcome": "allow", "decision_id": "decision-1"}))
    recorded = []

    async def append_events(_db, **kwargs):
        recorded.extend(kwargs["drafts"])
        return [NS(id=uuid4())]

    monkeypatch.setattr(runtime, "append_session_events", append_events)
    monkeypatch.setattr("app.services.content_input_receipts.load_workspace_execution_evidence",
        AsyncMock(return_value=("workspace-output:proof", {"workspace/report.md": "a" * 64})))
    monkeypatch.setattr("app.services.web_chat_runtime._ensure_tool_result_artifact_message", AsyncMock())
    artifact = {"type": "artifact", "artifact_id": str(uuid4()), "path": "workspace/report.md"}
    monkeypatch.setattr("app.services.chat_artifact_delivery.create_chat_artifacts_for_message",
        AsyncMock(return_value=[artifact]))
    await runtime.complete_tool_invocation(db, tenant_id=model.tenant_id, agent_id=agent, session_id=model.session_id,
        invocation_id=invocation_id, provider_result_content="Wrote workspace/report.md", execution_evidence={})

    result = recorded[-1].payload
    assert result["parts"] == ([] if permission_withheld else [artifact])
    assert result["content"] == "Wrote workspace/report.md"
    assert invocation.effect_state == "effect_committed"
    assert target.read_text() == "Working file remains available."
    if permission_withheld:
        assert result["metadata"]["artifact_delivery"]["files"][0]["reason_code"] == "workspace_execution_unverified"
