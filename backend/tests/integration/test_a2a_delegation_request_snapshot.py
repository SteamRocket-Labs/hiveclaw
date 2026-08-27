"""Restart rebuild must reproduce the persisted A2A delegation request snapshot.

Production evidence (2026-08-27, task e8fa186d-7e9e-4c31-ac23-7d348d3e71a2):
the resume dispatch held the run with ``a2a_request_snapshot_drift`` because
the execution receipt's ``request_hash`` was computed over the
dispatch-normalized ``edit_mode`` (``create_or_update``) while
``_build_delegation_request_from_runtime_record`` rebuilt ``edit_mode=None``
from runtime-task metadata that only persisted ``edit_mode`` when target
artifacts existed.

These regressions run the real durable path — ``delegate_async`` →
``create_runtime_task_record`` → restart rebuild from
``get_runtime_task_record`` → ``dispatch_persisted_async_delegation`` —
against real PostgreSQL, so the persisted metadata itself (not a mocked
record) must rebuild byte-faithful ``_delegation_request_hash`` inputs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session as real_tenant_scoped_session
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.services import runtime_task_service
from app.services.tenant_resolver import resolve_tenant_for_agent as real_resolve_tenant_for_agent


async def _mk_tenant(db) -> uuid.UUID:
    tenant = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
    db.add(tenant)
    await db.flush()
    return tenant.id


async def _mk_user(db, tenant_id: uuid.UUID) -> uuid.UUID:
    user = User(
        username=f"u-{uuid.uuid4().hex[:10]}",
        email=f"{uuid.uuid4().hex[:10]}@example.test",
        password_hash="x",
        display_name="U",
        tenant_id=tenant_id,
    )
    db.add(user)
    await db.flush()
    return user.id


async def _mk_model(db, tenant_id: uuid.UUID) -> uuid.UUID:
    model = LLMModel(
        provider="openai",
        model="gpt-4.1",
        api_key_encrypted="test-key",
        label="Test model",
        tenant_id=tenant_id,
        enabled=True,
    )
    db.add(model)
    await db.flush()
    return model.id


async def _mk_agent(db, *, creator_id: uuid.UUID, tenant_id: uuid.UUID, model_id: uuid.UUID, name: str) -> uuid.UUID:
    agent = Agent(name=name, creator_id=creator_id, tenant_id=tenant_id, primary_model_id=model_id)
    db.add(agent)
    await db.flush()
    return agent.id


async def _seed_tenant(owner_sessionmaker) -> dict[str, uuid.UUID]:
    async with owner_sessionmaker() as db:
        tenant_id = await _mk_tenant(db)
        user_id = await _mk_user(db, tenant_id)
        model_id = await _mk_model(db, tenant_id)
        parent_agent_id = await _mk_agent(
            db, creator_id=user_id, tenant_id=tenant_id, model_id=model_id, name="Coordinator"
        )
        target_agent_id = await _mk_agent(db, creator_id=user_id, tenant_id=tenant_id, model_id=model_id, name="Worker")
        await db.commit()
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "parent_agent_id": parent_agent_id,
        "target_agent_id": target_agent_id,
    }


def _bind_runtime_store(monkeypatch, sessionmaker) -> None:
    """Point every runtime-task accessor at the Testcontainers engine.

    ``runtime_task_service`` imports its session helpers at module scope, while
    the orchestrator and coordination wiring import
    ``tenant_scoped_session`` at call time — bind both to the same real
    sessionmaker so create/read/update and the restart rebuild all hit real
    PostgreSQL.
    """

    def _scoped(tenant_id=None, **_kwargs):
        return real_tenant_scoped_session(tenant_id, session_factory=sessionmaker)

    async def _resolve(agent_id, **_kwargs):
        return await real_resolve_tenant_for_agent(agent_id, session_factory=sessionmaker)

    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", _scoped)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", _resolve)
    monkeypatch.setattr(runtime_task_service, "async_session", sessionmaker)

    import app.agents.coordination_wiring as coordination_wiring
    import app.database as database_module

    monkeypatch.setattr(coordination_wiring, "tenant_scoped_session", _scoped)
    monkeypatch.setattr(database_module, "tenant_scoped_session", _scoped)


async def _dispatch_delegation(
    monkeypatch,
    seeded: dict[str, uuid.UUID],
    *,
    edit_mode: str | None = None,
    target_artifact_path: str | None = None,
    target_artifacts: list[dict] | None = None,
    message: str = "Draft the quarterly delegation report",
):
    """Run the real durable dispatch (delegate_async) and return (handle, spawns)."""
    from app.agents import orchestrator
    from app.core.execution_context import ExecutionPrincipal

    spawns: list[dict] = []

    async def _allow_plan(_request):
        return True, "integration_dispatch"

    monkeypatch.setattr(orchestrator, "_delegation_plan_gate_allows", _allow_plan)
    monkeypatch.setattr(orchestrator, "_spawn_async_delegation_task", lambda **kwargs: spawns.append(kwargs))

    principal = ExecutionPrincipal(
        tenant_id=seeded["tenant_id"],
        source_agent_id=seeded["parent_agent_id"],
        requester_user_id=seeded["user_id"],
        root_session_id=None,
        root_runtime_task_id=None,
        origin="agent_tool",
        delegation_chain=(f"agent:{seeded['parent_agent_id']}",),
    )
    handle = await orchestrator.delegate_async(
        target=SimpleNamespace(
            id=seeded["target_agent_id"],
            tenant_id=seeded["tenant_id"],
            name="Worker",
            role_description="Worker",
        ),
        target_model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        conversation_messages=[{"role": "user", "content": message}],
        owner_id=seeded["user_id"],
        session_id=uuid.uuid4().hex,
        parent_agent_id=seeded["parent_agent_id"],
        parent_agent_name="Coordinator",
        tenant_id=seeded["tenant_id"],
        permission_profile={
            "mode": "dontAsk",
            "allowed_tools": ["read_file"],
            "sandbox": "read_only",
        },
        execution_principal=principal.to_evidence(),
        edit_mode=edit_mode,
        target_artifact_path=target_artifact_path,
        target_artifacts=target_artifacts,
    )
    return handle, spawns


async def _restart_rebuild(task_id: str):
    """Simulate a worker restart: drop in-memory state, reload the durable record."""
    from app.agents import orchestrator

    orchestrator._async_tasks.clear()
    orchestrator._async_task_parent_ids.pop(task_id, None)
    orchestrator._async_task_fallback_records.pop(task_id, None)
    record = await runtime_task_service.get_runtime_task_record(task_id)
    assert record is not None, "durable delegation record must survive the dispatch that created it"
    rebuilt = await orchestrator._build_delegation_request_from_runtime_record(record)
    assert rebuilt is not None, "restart rebuild must rehydrate the delegation request from persisted metadata"
    return record, rebuilt


@pytest.mark.parametrize(
    ("scenario", "dispatch_kwargs"),
    [
        pytest.param("default_edit_mode_no_artifacts", {}, id="default-edit-mode-no-artifacts"),
        pytest.param(
            "explicit_edit_mode_no_artifacts",
            {"edit_mode": "modify_existing"},
            id="explicit-edit-mode-no-artifacts",
        ),
        pytest.param(
            "artifacts_with_explicit_mode",
            {
                "edit_mode": "create_new",
                "target_artifacts": [
                    {
                        "path": "reports/q3-summary.md",
                        "workspace_scope": "target_agent_workspace",
                        "expected_action": "create_new",
                    }
                ],
            },
            id="artifacts-with-explicit-mode",
        ),
        pytest.param(
            "artifact_path_shorthand",
            {"target_artifact_path": "reports/q3-summary.md"},
            id="artifact-path-shorthand",
        ),
        pytest.param(
            "shorthand_plus_artifacts_list",
            {
                "target_artifact_path": "reports/q3-summary.md",
                "target_artifacts": [
                    {
                        "path": "data/q3-figures.csv",
                        "workspace_scope": "source_agent_workspace",
                        "expected_action": "review_only",
                    }
                ],
            },
            id="shorthand-plus-artifacts-list",
        ),
    ],
)
async def test_restart_rebuild_reproduces_dispatch_request_hash(
    owner_sessionmaker,
    monkeypatch,
    scenario,
    dispatch_kwargs,
):
    from app.agents import orchestrator

    seeded = await _seed_tenant(owner_sessionmaker)
    _bind_runtime_store(monkeypatch, owner_sessionmaker)

    handle, _spawns = await _dispatch_delegation(monkeypatch, seeded, **dispatch_kwargs)
    assert handle.status == "queued", f"durable dispatch must be admitted, got {handle.status}"

    record, rebuilt = await _restart_rebuild(handle.task_id)
    receipt_hash = record["metadata"]["execution_receipt"]["request_hash"]
    rebuilt_hash = orchestrator._delegation_request_hash(rebuilt)
    assert rebuilt_hash == receipt_hash, (
        f"[{scenario}] restart rebuild must reproduce the persisted receipt request hash: "
        f"expected {receipt_hash}, rebuilt {rebuilt_hash}"
    )

    dispatched = await orchestrator.dispatch_persisted_async_delegation(handle.task_id)
    assert dispatched is True, f"[{scenario}] resume dispatch must verify the receipt and spawn the worker"
    post_record = await runtime_task_service.get_runtime_task_record(handle.task_id)
    assert post_record["status"] == "running"


async def _tamper_persisted_metadata(owner_sessionmaker, task_id: str, mutate) -> None:
    async with owner_sessionmaker() as db:
        result = await db.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(task_id)))
        row = result.scalar_one()
        metadata = dict(row.metadata_json or {})
        mutate(metadata)
        row.metadata_json = metadata
        await db.commit()


async def test_restart_rebuild_still_holds_tampered_request_messages(owner_sessionmaker, monkeypatch):
    """A genuine request-snapshot drift (persisted messages changed) must stay a typed hold."""
    from app.agents import orchestrator

    seeded = await _seed_tenant(owner_sessionmaker)
    _bind_runtime_store(monkeypatch, owner_sessionmaker)

    handle, spawns = await _dispatch_delegation(monkeypatch, seeded)
    assert handle.status == "queued"

    await _tamper_persisted_metadata(
        owner_sessionmaker,
        handle.task_id,
        lambda metadata: metadata.update(
            {"conversation_messages": [{"role": "user", "content": "tampered instruction"}]}
        ),
    )
    await _restart_rebuild(handle.task_id)

    dispatched = await orchestrator.dispatch_persisted_async_delegation(handle.task_id)
    assert dispatched is False
    assert spawns == []
    held = await runtime_task_service.get_runtime_task_record(handle.task_id)
    assert held["status"] == "needs_reconciliation"
    evidence = held["metadata"]
    assert evidence["restart_resume_blocker"] == "a2a_request_snapshot_drift"
    assert evidence["automatic_retry_disabled"] is True
    assert evidence["authority_reconciliation"]["reason_code"] == "a2a_request_snapshot_drift"


async def test_restart_rebuild_still_holds_authority_snapshot_drift(owner_sessionmaker, monkeypatch):
    """A genuine authority-snapshot drift (persisted permission profile changed) must stay a typed hold."""
    from app.agents import orchestrator

    seeded = await _seed_tenant(owner_sessionmaker)
    _bind_runtime_store(monkeypatch, owner_sessionmaker)

    handle, spawns = await _dispatch_delegation(monkeypatch, seeded)
    assert handle.status == "queued"

    def _widen_permissions(metadata):
        profile = dict(metadata.get("permission_profile") or {})
        profile["allowed_tools"] = ["read_file", "write_file"]
        profile["sandbox"] = "workspace_write"
        metadata["permission_profile"] = profile

    await _tamper_persisted_metadata(owner_sessionmaker, handle.task_id, _widen_permissions)
    await _restart_rebuild(handle.task_id)

    dispatched = await orchestrator.dispatch_persisted_async_delegation(handle.task_id)
    assert dispatched is False
    assert spawns == []
    held = await runtime_task_service.get_runtime_task_record(handle.task_id)
    assert held["status"] == "needs_reconciliation"
    evidence = held["metadata"]
    assert evidence["restart_resume_blocker"] == "a2a_authority_snapshot_drift"
    assert evidence["automatic_retry_disabled"] is True
    assert evidence["authority_reconciliation"]["reason_code"] == "a2a_authority_snapshot_drift"
