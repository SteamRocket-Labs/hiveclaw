from __future__ import annotations

import asyncio
import uuid

import pytest

from app.config import get_settings
from app.runtime.workflow_admission import AdmissionLimits, admit_workflow
from app.runtime.workflow_compiler import compile_workflow
from app.services.deep_research.schemas import ResearchRequest
from app.services.deep_research.workflow_definition import (
    DEEP_RESEARCH_WORKFLOW_NAME,
    build_deep_research_workflow_definition,
    deep_research_workflow_args,
)


def test_deep_research_v1_definition_compiles_to_plan_fanout_synthesize_critic() -> None:
    definition = build_deep_research_workflow_definition()

    compiled = compile_workflow(
        definition,
        known_leaves={
            "deep_research_planner",
            "deep_research_explorer",
            "deep_research_synthesizer",
            "deep_research_critic",
        },
    )

    assert definition["name"] == DEEP_RESEARCH_WORKFLOW_NAME
    assert [step.id for step in compiled.definition.steps] == ["plan", "explore", "synthesize", "critic"]
    assert compiled.leaf_names == {
        "deep_research_planner",
        "deep_research_explorer",
        "deep_research_synthesizer",
        "deep_research_critic",
    }


def test_deep_research_v1_args_are_admitted_and_fallback_topics_are_bounded() -> None:
    request = ResearchRequest.from_arguments(
        {
            "question": "Map the current enterprise AI workflow market",
            "depth": "full",
            "scope": "US SaaS",
            "output_format": "markdown",
            "plan_confirmed": True,
        }
    )
    args = deep_research_workflow_args(request)
    compiled = compile_workflow(build_deep_research_workflow_definition())

    admission = admit_workflow(compiled, args=args, limits=AdmissionLimits.from_settings(get_settings()))

    assert args["worker_topics"] == ["Map the current enterprise AI workflow market"]
    assert admission.budget_tokens > 0


# ── real-PG registration contract (§9 P14) ────────────────────────


@pytest.fixture()
async def pg_tenant_id(owner_sessionmaker):
    from app.database import tenant_scoped_session
    from app.models.tenant import Tenant

    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="dr-wf", slug=f"dr-{tid.hex[:10]}"))
    return tid


@pytest.mark.usefixtures("migrated_pg_url")
async def test_ensure_definition_is_idempotent_on_real_pg(pg_tenant_id, owner_sessionmaker):
    """First call registers + activates deep_research.v1; the second call must
    return the SAME record instead of stacking a new version."""
    from app.services.deep_research.workflow_definition import ensure_deep_research_workflow_definition

    first = await ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker)
    second = await ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker)

    assert first.status == "active"
    assert second.id == first.id
    assert second.definition_version == first.definition_version


@pytest.mark.usefixtures("migrated_pg_url")
async def test_concurrent_ensure_does_not_stack_duplicate_versions(pg_tenant_id, owner_sessionmaker):
    """Two concurrent ensures race create_draft (same-name auto-next-version
    never hits the unique constraint) — without serialisation they register
    v1 AND v2 both active. The advisory lock must collapse that to one."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.workflow import WorkflowDefinitionRecord
    from app.services.deep_research.workflow_definition import (
        DEEP_RESEARCH_WORKFLOW_NAME,
        ensure_deep_research_workflow_definition,
    )

    records = await asyncio.gather(
        ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker),
        ensure_deep_research_workflow_definition(tenant_id=pg_tenant_id, session_factory=owner_sessionmaker),
    )
    assert records[0].id == records[1].id

    async with tenant_scoped_session(str(pg_tenant_id), session_factory=owner_sessionmaker) as session:
        rows = (
            (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(
                        WorkflowDefinitionRecord.tenant_id == pg_tenant_id,
                        WorkflowDefinitionRecord.name == DEEP_RESEARCH_WORKFLOW_NAME,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, (
        f"expected exactly one registered record, got versions {[r.definition_version for r in rows]}"
    )


@pytest.mark.usefixtures("migrated_pg_url")
async def test_ensure_definition_never_reuses_other_tenant_record_under_owner_dsn(
    pg_tenant_id,
    owner_sessionmaker,
):
    """The ensure query must be tenant-explicit, not RLS-only. Owner/superuser
    test DSNs can see every tenant; without a tenant predicate tenant B would
    incorrectly reuse tenant A's active deep_research.v1 record."""
    from sqlalchemy import select

    from app.database import tenant_scoped_session
    from app.models.tenant import Tenant
    from app.models.workflow import WorkflowDefinitionRecord
    from app.services.deep_research.workflow_definition import ensure_deep_research_workflow_definition

    other_tenant = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=other_tenant, name="dr-wf-other", slug=f"dr-{other_tenant.hex[:10]}"))

    first = await ensure_deep_research_workflow_definition(
        tenant_id=pg_tenant_id,
        session_factory=owner_sessionmaker,
    )
    second = await ensure_deep_research_workflow_definition(
        tenant_id=other_tenant,
        session_factory=owner_sessionmaker,
    )

    assert first.tenant_id == pg_tenant_id
    assert second.tenant_id == other_tenant
    assert second.id != first.id

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = (
            (
                await session.execute(
                    select(WorkflowDefinitionRecord).where(WorkflowDefinitionRecord.name == DEEP_RESEARCH_WORKFLOW_NAME)
                )
            )
            .scalars()
            .all()
        )
    assert {row.tenant_id for row in rows} >= {pg_tenant_id, other_tenant}
