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


def assert_source_withheld(models, *, content, code):
    """Check later requests after an individual source loses read authority."""
    assert models and content
    entries = []
    for model in models:
        snapshot = model.model_request_snapshot_json
        assert snapshot["input_bindings"]["source_authority"] == "not_evaluated"
        assert content not in json.dumps(snapshot["wire_request"], ensure_ascii=False)
        entries.extend(snapshot["input_bindings"]["messages"])
    withheld = [entry for entry in entries if entry.get("status") == "withheld" and entry.get("code") == code]
    assert withheld, entries
    assert all(entry["dependencies"] == [] and entry["dependency_status"] == "resolved" for entry in withheld)


@contextmanager
def legacy_model_requests(monkeypatch):
    """Seed old persisted requests; restore the current boundary before reading them."""
    async def retain_legacy_request(*_args, **_kwargs):
        pass

    with monkeypatch.context() as legacy:
        legacy.setattr("app.services.session_model_round._validate_or_withhold_model_sources", retain_legacy_request)
        yield
