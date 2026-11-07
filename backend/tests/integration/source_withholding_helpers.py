"""Assertions for current withholding and explicit pre-withholding fixtures."""

import json
from contextlib import contextmanager

from sqlalchemy import select

from app.models.session_v2 import SessionModelResult


async def assert_withheld_input(db, *, run_id, snapshot, index, content):
    bindings = snapshot["input_bindings"]
    assert bindings["dependency_coverage"]["complete"] is True
    assert bindings["source_authority"] == "not_evaluated"
    entry = bindings["messages"][index]
    assert entry["status"] == "withheld"
    assert entry["code"] == "model_input_source_unbound"
    assert entry["dependencies"] == [] and entry["dependency_status"] == "resolved"
    models = (await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id))).all()
    assert models and content
    encoded = json.dumps(content, ensure_ascii=False)[1:-1]
    for model in models:
        assert encoded not in json.dumps(model.model_request_snapshot_json["wire_request"], ensure_ascii=False)


@contextmanager
def legacy_model_requests(monkeypatch):
    """Seed old persisted requests; restore the current boundary before reading them."""
    async def retain_legacy_request(*_args, **_kwargs):
        pass

    with monkeypatch.context() as legacy:
        legacy.setattr("app.services.session_model_round._validate_or_withhold_model_sources", retain_legacy_request)
        yield
