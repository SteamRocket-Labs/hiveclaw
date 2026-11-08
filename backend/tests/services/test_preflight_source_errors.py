"""Batch reads preserve the first source's established denial contract."""

import uuid
from types import SimpleNamespace

import pytest

from app.services import content_input_receipts as receipts
from app.services import learned_skill_sources as learned
from app.services.session_model_inputs import _hash


async def test_unknown_source_precedes_invalid_model_source():
    dependencies = [
        {"source_kind": "unknown", "source_id": "unknown", "source_version": "unknown"},
        {"source_kind": "session_model_result", "source_id": "invalid", "source_version": "invalid"},
    ]
    with pytest.raises(receipts.ContentInputReceiptError) as denied:
        await receipts._validate_input_dependency_batch(None, uuid.uuid4(), dependencies)
    assert (denied.value.code, denied.value.status_code) == ("content_source_unavailable", 503)


@pytest.mark.parametrize("audience,code,status", [
    ("private", "content_source_access_denied", 403),
    ("unsupported", "content_source_unavailable", 503),
])
async def test_actor_failure_keeps_the_source_audience_error(monkeypatch, audience, code, status):
    async def unavailable(*_args, **_kwargs):
        raise receipts.ContentInputReceiptError("content_source_authority_unavailable", "Unavailable", 503)

    monkeypatch.setattr(receipts, "_runtime_input_actor", unavailable)
    session = SimpleNamespace(id=uuid.uuid4(), audience_kind=audience)
    with pytest.raises(receipts.ContentInputReceiptError) as denied:
        await receipts._authorize_session_input_source(None, uuid.uuid4(), session,
            user=None, runtime_request=object())
    assert (denied.value.code, denied.value.status_code) == (code, status)


@pytest.mark.parametrize("later_failure", ["bad_seal", "bad_id"])
async def test_first_models_child_precedes_later_models_failure(monkeypatch, later_failure):
    tenant, session_id, run_id = (uuid.uuid4() for _ in range(3))
    session = SimpleNamespace(id=session_id, tenant_id=tenant)
    snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "messages": [],
        "dependencies": [{"source_kind": "session_command", "source_id": str(uuid.uuid4()),
            "source_version": "withheld"}], "dependency_coverage": {"complete": True}}}

    def model():
        source_id = uuid.uuid4()
        return SimpleNamespace(id=source_id, tenant_id=tenant, session_id=session_id, run_id=run_id,
            provider_request_id=str(source_id), state="round_committed", model_request_snapshot_json=snapshot,
            model_request_hash=_hash(snapshot), seal_json={"result_id": str(source_id), "run_id": str(run_id),
                "provider_request_id": str(source_id), "semantic_content": "Result", "content_hash": _hash("Result")})

    first, second = model(), model()
    second.seal_json["content_hash"] = "invalid"

    class Database:
        async def execute(self, _statement):
            return [(first, session), (second, session)]

    async def authorize(*_args, **_kwargs):
        return None

    async def child_denied(*_args, **_kwargs):
        raise receipts.ContentInputReceiptError("content_source_access_denied", "Child denied", 403)

    monkeypatch.setattr(receipts, "_authorize_session_input_source", authorize)
    monkeypatch.setattr(learned, "_validate_input_dependencies", child_denied)
    second_source = learned.model_source(second)
    if later_failure == "bad_id":
        second_source["source_id"] = "invalid"
    with pytest.raises(receipts.ContentInputReceiptError) as denied:
        await learned.validate_model_sources(Database(), tenant, [learned.model_source(first), second_source])
    assert (denied.value.code, denied.value.status_code) == ("content_source_access_denied", 403)
