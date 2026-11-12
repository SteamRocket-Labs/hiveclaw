"""Request query templates retain explicit bindings and snapshot identities."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import literal

from app.core.permissions import agent_access_from_facts
from app.services.content_input_receipts import group_source_read
from app.services.session_model_inputs import _model_snapshot_dependencies
from app.services.source_read_queries import source_reader_statement


def test_expression_templates_do_not_reuse_an_earlier_literal_binding():
    user = SimpleNamespace(id=uuid.uuid4())
    first, second = uuid.uuid4(), uuid.uuid4()
    with group_source_read(batch_inputs=True, source_catalog=True):
        before = source_reader_statement(user, literal(first))
        after = source_reader_statement(user, literal(second))
        assert before is not after
        assert first in before.compile().params.values()
        assert second in after.compile().params.values()
        assert first not in after.compile().params.values()


@pytest.mark.asyncio
async def test_missing_access_facts_raise_the_base_404():
    with pytest.raises(HTTPException) as denied:
        await agent_access_from_facts(None, SimpleNamespace(), None)
    assert denied.value.status_code == 404
    assert denied.value.detail == "Agent not found"


def test_snapshot_memo_requires_the_retained_snapshot_identity():
    snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1", "dependencies": [],
        "dependency_coverage": {"complete": True}}}
    result = SimpleNamespace(model_request_snapshot_json=snapshot, model_request_hash="incorrect stored hash")
    with group_source_read(batch_inputs=True, source_catalog=True) as scope:
        key = ("model_snapshot_dependencies", id(snapshot), result.model_request_hash)
        scope.model_inputs[key] = ({"another": "snapshot"}, ([], True))
        assert _model_snapshot_dependencies(result) == ([], False)
