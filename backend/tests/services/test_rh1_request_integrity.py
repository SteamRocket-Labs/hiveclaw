from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.session_v2 import SessionModelResult
from app.services.content_input_receipts import ContentInputReceiptError, _model_request_digest
from app.services.delegation_permission_runtime import validate_permission_input_sources
from app.services.session_model_inputs import _hash, _model_dependencies, model_output_dependencies
from app.tools.runtime import ToolExecutionRequest


@pytest.fixture
def sealed_model():
    snapshot = {
        "input_bindings": {
            "schema": "hive.model_input_bindings.v1",
            "messages": [],
            "dependencies": [],
            "dependency_coverage": {"complete": True},
        },
        "wire_request": {"messages": []},
    }
    return SessionModelResult(state="sealed", model_request_hash=_hash(snapshot), model_request_snapshot_json=snapshot)


@pytest.mark.parametrize("dependencies", [_model_dependencies, model_output_dependencies])
def test_loaded_sealed_request_must_match_its_hash(sealed_model, dependencies):
    assert dependencies(sealed_model) == ([], True)
    sealed_model.model_request_hash = "0" * 64
    assert dependencies(sealed_model) == ([], False)


def test_receipt_digest_checks_a_loaded_sealed_request(sealed_model):
    original_digest = sealed_model.model_request_hash
    sealed_model.model_request_hash = "0" * 64
    assert _model_request_digest(sealed_model) == original_digest


@pytest.mark.asyncio
async def test_delegation_permission_refuses_a_mismatched_sealed_request(sealed_model):
    context = SimpleNamespace(tenant_id=uuid4(), runtime_task_id=uuid4(), authority_frame_required=True)
    request = ToolExecutionRequest("send_email", {}, context)
    await validate_permission_input_sources(None, session=None, model=sealed_model, runtime_request=request)

    sealed_model.model_request_hash = "0" * 64
    with pytest.raises(ContentInputReceiptError) as refusal:
        await validate_permission_input_sources(None, session=None, model=sealed_model, runtime_request=request)
    assert refusal.value.code == "delegation_permission_sources_unavailable"
