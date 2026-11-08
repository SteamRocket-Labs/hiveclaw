"""Withholding optional evidence does not prove missing session permissions."""

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.mark.parametrize("source", ["permission", "unbound_source", "revoked_file"])
async def test_withheld_permission_blocks_outputs_without_hiding_next_round_inputs(monkeypatch, source):
    from app.runtime.context_candidates import ContextMessageOrigin
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.llm_client import LLMMessage
    from app.services.session_model_round import _validate_or_withhold_model_sources
    from app.services.session_model_inputs import _hash, _model_dependencies

    revoked = source == "revoked_file"
    dependency = {"source_kind": "workspace_artifact", "source_id": str(uuid4()), "source_version": "a" * 64}
    dependencies = [dependency] if revoked else []
    message = LLMMessage(role="system", content="WITHHELD_CONTEXT", source_ref=f"session-kernel-context:{uuid4()}",
        context_origin=ContextMessageOrigin.capture(kind="dynamic_prompt", role="system", content="WITHHELD_CONTEXT",
            sections=[{"name": "approval_policy" if source == "permission" else "memory_context"}],
            sources=dependencies, unbound=() if revoked else ("context_sources_unbound",)))
    messages = [message, LLMMessage(role="user", content="Continue the authorized task.")]
    bindings = {"schema": "hive.model_input_bindings.v1", "status": "resolved" if revoked else "incomplete",
        "dependencies": dependencies.copy(), "dependency_coverage": {"complete": revoked}, "messages": [
            {"message_index": 0, "source_ref": message.source_ref, "status": "bound", "dependencies": dependencies,
                "dependency_status": "resolved" if revoked else "incomplete"},
            {"message_index": 1, "status": "bound", "dependencies": [], "dependency_status": "resolved"}]}
    wire = {"messages": [{"role": item.role, "content": item.content} for item in messages]}
    db = NS(get=AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.team_task_context.validate_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.shared_group_authority.validate_shared_group_model_inputs",
        AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.explicit_memory_authority.validate_memory_model_sources", AsyncMock())
    monkeypatch.setattr("app.services.trigger_read_sources.validate_native_trigger_model_sources",
        AsyncMock(side_effect=ContentInputReceiptError("source_revoked", "Source revoked.", 403) if revoked else None))

    await _validate_or_withhold_model_sources(db, tenant_id=uuid4(), agent_id=uuid4(), session_id=uuid4(),
        run_id=uuid4(), input_bindings=bindings, messages=messages, wire_request=wire)

    assert bindings["dependency_coverage"]["complete"] is True
    assert bindings["messages"][0]["status"] == "withheld"
    assert bindings["messages"][0]["code"] == (
        "source_revoked" if revoked else "session_permission_frame_unbound" if source == "permission"
        else "model_input_source_unbound")
    assert bindings["dependencies"] == []
    assert "WITHHELD_CONTEXT" not in str(wire)
    assert "暂时无法核实" in wire["messages"][0]["content"]
    assert wire["messages"][1] == {"role": "user", "content": "Continue the authorized task."}
    snapshot = {"input_bindings": bindings}
    model = NS(state="round_committed", model_request_snapshot_json=snapshot, model_request_hash=_hash(snapshot))
    assert _model_dependencies(model) == ([], True)
    from app.services.session_model_inputs import model_output_dependencies

    assert model_output_dependencies(model) == ([], source != "permission")


async def test_read_artifact_publication_without_model_returns_no_artifacts():
    from app.services.chat_artifact_delivery import materialize_declared_read_artifacts

    db = NS(get=AsyncMock(return_value=None))

    assert await materialize_declared_read_artifacts(db, task=NS(), model_result_id=uuid4(),
        paths=["workspace/report.md"]) == []
