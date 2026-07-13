"""§9 P5 red tests: leaf-level journal + advisory-lock quota on REAL PG.

THE P5 contract (v1 decision 6): 8-leaf fanout with 7 done resumes exactly
1 — never the whole step. WorkflowLeafCall rows carry
input_hash/idempotency_key/status/token_usage; workflow_quotas is
pre-deducted under a Postgres advisory lock and settled with actual usage.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.workflow import WorkflowLeafCall, WorkflowQuota
from app.runtime.workflow_engine import LeafOutcome, LeafRequest
from app.services.workflow_runtime_service import WorkflowRuntimeService

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _fan_definition(budget_tokens: int = 200_000) -> dict:
    return {
        "name": "fan-8",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "default_budget": {"max_total_tokens": budget_tokens},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}",
                "max_concurrency": 4,
            },
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-leaf", slug=f"wl-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
def service(owner_sessionmaker) -> WorkflowRuntimeService:
    return WorkflowRuntimeService(session_factory=owner_sessionmaker)


async def test_eight_leaves_seven_done_resume_runs_exactly_one(service, tenant_id, owner_sessionmaker):
    targets = [f"t{i}" for i in range(8)]
    first_calls: list[str] = []

    async def leaf_fails_last(request: LeafRequest) -> LeafOutcome:
        first_calls.append(request.leaf_id)
        if request.leaf_id == "item-7":
            return LeafOutcome(ok=False, error="boom on the 8th")
        return LeafOutcome(ok=True, output={"i": request.leaf_id}, tokens_used=100)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": targets},
        leaf_executor=leaf_fails_last,
    )
    assert handle.outcome.status == "failed"
    assert len(first_calls) == 8

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        rows = (
            (await session.execute(select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == handle.run_id)))
            .scalars()
            .all()
        )
    by_leaf = {row.leaf_id: row for row in rows}
    assert len(by_leaf) == 8
    assert sum(1 for row in rows if row.status == "done") == 7
    assert by_leaf["item-7"].status == "failed"
    done_row = by_leaf["item-0"]
    assert done_row.input_hash and done_row.definition_hash and done_row.idempotency_key
    assert done_row.token_usage == {"total": 100}

    resume_calls: list[str] = []

    async def ok_leaf(request: LeafRequest) -> LeafOutcome:
        resume_calls.append(request.leaf_id)
        return LeafOutcome(ok=True, output={"i": request.leaf_id}, tokens_used=100)

    outcome = await service.resume_run(handle.run_id, tenant_id=tenant_id, leaf_executor=ok_leaf)

    assert outcome.status == "completed"
    assert resume_calls == ["item-7"], "7 done leaves must be replayed from journal, only the 8th runs"


async def test_quota_prededucted_and_settled_on_real_pg(service, tenant_id, owner_sessionmaker):
    async def leaf(request: LeafRequest) -> LeafOutcome:
        return LeafOutcome(ok=True, output={}, tokens_used=250)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(),
        args={"targets": ["a", "b", "c"]},
        leaf_executor=leaf,
    )
    assert handle.outcome.status == "completed"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        quota = (await session.execute(select(WorkflowQuota).where(WorkflowQuota.run_id == handle.run_id))).scalar_one()
    assert quota.consumed_tokens == 750, "actual usage settled back (3 leaves × 250)"


async def test_budget_exhaustion_suspends_run_definitely(service, tenant_id, owner_sessionmaker):
    """Budget covers fewer leaves than requested: the run must land in
    'suspended' (a DEFINITE state) and stop spawning."""
    from app.config import get_settings

    estimate = get_settings().WORKFLOW_LEAF_TOKEN_ESTIMATE
    calls: list[str] = []

    async def leaf(request: LeafRequest) -> LeafOutcome:
        calls.append(request.leaf_id)
        return LeafOutcome(ok=True, output={}, tokens_used=estimate)

    handle = await service.start_run(
        tenant_id=tenant_id,
        definition_data=_fan_definition(budget_tokens=estimate * 2),
        args={"targets": ["a", "b", "c", "d"]},
        leaf_executor=leaf,
    )

    assert handle.outcome.status == "suspended"
    assert "budget" in (handle.outcome.reason or "")
    assert len(calls) == 2, "no leaf may start past the exhausted budget"

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        from app.models.runtime_task import RuntimeTask

        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
    assert task.status == "suspended"
