"""§9 P4 red tests: workflow REST surface — preview / start / get / cancel,
plus the asset-view endpoints (run history list / promote-from-run /
promote suggestions) and run-ownership guards.

API-layer responsibilities only (service behaviour is covered on real PG in
tests/services/): agent access control, confirmation notes without low/high risk
grades, run↔agent/tenant ownership, and error mapping. The runtime service is
stubbed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.workflows as workflows_api
from app.core.security import get_current_user
from app.database import get_db
from app.runtime.workflow_engine import WorkflowRunOutcome
from app.services.workflow_runtime_service import WorkflowRunHandle


def _user(tenant_id=None):
    return SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id or uuid.uuid4(), username="u")


def _low_risk_definition() -> dict:
    return {
        "name": "read-probe",
        "args_schema": {},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan the workspace",
            }
        ],
    }


def _high_risk_definition() -> dict:
    return {
        "name": "external-send",
        "args_schema": {},
        "steps": [
            {"id": "gate", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send externally",
                "effects": "external",
            },
        ],
    }


def _client(user, monkeypatch, *, gate_allowed=True, gate_reason=None, access_level="manage"):
    api = FastAPI()
    api.include_router(workflows_api.router)

    async def override_user():
        return user

    async def override_db():
        yield SimpleNamespace()

    api.dependency_overrides[get_current_user] = override_user
    api.dependency_overrides[get_db] = override_db

    # Agent access always passes (cross-agent denial tested separately).
    # Mirrors the real contract: returns (agent, access_level).
    async def fake_access(db, current_user, agent_id):
        return SimpleNamespace(id=agent_id, tenant_id=user.tenant_id, name="agent"), access_level

    monkeypatch.setattr(workflows_api, "check_agent_access", fake_access)

    async def fake_launch(**kwargs):
        fake_launch.calls.append(kwargs)
        return WorkflowRunHandle(run_id=uuid.uuid4(), outcome=WorkflowRunOutcome(status="completed"))

    fake_launch.calls = []
    monkeypatch.setattr(workflows_api, "start_ephemeral_workflow_for_agent", fake_launch)

    async def fake_gate_check(db, **kwargs):
        fake_gate_check.calls.append(kwargs)
        return SimpleNamespace(allowed=gate_allowed, reason=gate_reason)

    fake_gate_check.calls = []
    monkeypatch.setattr(workflows_api, "_plan_gate_check", fake_gate_check)

    client = TestClient(api)
    client.fake_launch = fake_launch
    client.fake_gate_check = fake_gate_check
    return client


def test_preview_returns_hash_confirmation_notes_and_planned_leaves(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/preview",
        json={"definition": _low_risk_definition(), "args": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["definition_hash"]
    assert "risk" not in body
    assert body["confirmation_required"] is False
    assert body["confirmation_reasons"] == []
    assert body["planned_leaf_calls"] == 1


def test_preview_external_effects_has_confirmation_notes_not_risk_level(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/preview",
        json={"definition": _high_risk_definition(), "args": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "risk" not in body
    assert "risk_reasons" not in body
    assert body["confirmation_required"] is True
    assert body["confirmation_reasons"]


def test_preview_maps_compile_error_to_400(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/preview",
        json={"definition": {"steps": []}, "args": {}},
    )
    assert resp.status_code == 400


def test_start_runs_without_plan_gate(monkeypatch):
    client = _client(_user(), monkeypatch)
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/runs",
        json={"definition": _low_risk_definition(), "args": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert len(client.fake_launch.calls) == 1
    assert client.fake_gate_check.calls == []  # start never consults PlanModeGate


def test_external_effect_start_without_plan_still_runs_without_plan_mode_gate(monkeypatch):
    client = _client(_user(), monkeypatch, gate_allowed=False, gate_reason="needs_plan")
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/runs",
        json={"definition": _high_risk_definition(), "args": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["confirmation_required"] is True
    assert len(client.fake_launch.calls) == 1
    assert client.fake_gate_check.calls == []


def test_confirmed_plan_metadata_is_forwarded_as_optional_provenance_without_gate(monkeypatch):
    client = _client(_user(), monkeypatch, gate_allowed=True)
    plan_id = str(uuid.uuid4())
    resp = client.post(
        f"/agents/{uuid.uuid4()}/workflows/runs",
        json={
            "definition": _high_risk_definition(),
            "args": {},
            "confirmed_plan_id": plan_id,
            "plan_version": 2,
            "plan_hash": "abc123",
        },
    )
    assert resp.status_code == 200
    assert client.fake_gate_check.calls == []
    launch_kwargs = client.fake_launch.calls[0]
    assert launch_kwargs["confirmed_plan_id"] == plan_id
    assert len(client.fake_launch.calls) == 1


def _run_task(*, run_id, agent_id, tenant_id, status="completed", name="contract-batch", source="ephemeral"):
    """A RuntimeTask(task_type=workflow) stand-in carrying the ephemeral archive."""
    return SimpleNamespace(
        id=run_id,
        status=status,
        task_type="workflow",
        parent_agent_id=agent_id,
        created_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 6, 5, 12, 5, tzinfo=timezone.utc) if status == "completed" else None,
        metadata_json={
            "tenant_id": str(tenant_id),
            "definition_source": source,
            "definition_hash": "h",
            "definition_json": {
                "name": name,
                "description": "OCR → extract → risk table",
                "steps": [
                    {
                        "id": "scan",
                        "type": "agent_step",
                        "leaf": {"name": "scanner", "type": "explorer"},
                        "task": "Scan",
                    }
                ],
            },
            "args": {},
        },
    )


def _dynamic_run_task(*, run_id, agent_id, tenant_id, status="failed"):
    task = _run_task(
        run_id=run_id,
        agent_id=agent_id,
        tenant_id=tenant_id,
        status=status,
        name="repo-audit",
        source="dynamic_workflow",
    )
    task.metadata_json["dynamic_workflow"] = {
        "proposal_id": "proposal-1",
        "candidate_id": "fanout-critic",
        "pattern_mix": ["fanout_synthesize", "adversarial_verify"],
        "success_criteria": ["Each slice cites evidence."],
        "failure_policy": {"leaf_failure": "record_and_continue", "repair_rounds": 1},
        "outcome_evidence": {
            "status": status,
            "steps_total": 1,
            "steps_done": 0,
            "steps_failed": 1,
            "leaf_total": 2,
            "leaf_done": 1,
            "leaf_failed": 1,
            "promotion_eligible": False,
        },
        "repair_plan": {
            "repairable": True,
            "strategy": "resume_failed_leaves",
            "failed_leaf_count": 1,
            "failed_leaves": [{"step_id": "scan", "leaf_id": "item-1", "error": "timeout"}],
        },
    }
    return task


def _patch_load(monkeypatch, loaded_factory):
    async def fake_load(self, rid, *, tenant_id=None):
        return loaded_factory(rid)

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "load_run", fake_load)


def test_get_run_returns_steps(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id),
            steps=[SimpleNamespace(step_id="scan", status="done", step_type="agent_step", error=None)],
        ),
    )
    resp = client.get(f"/agents/{agent_id}/workflows/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["steps"][0]["step_id"] == "scan"


def test_get_run_returns_dynamic_metadata_evidence_and_leaf_calls(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_dynamic_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id),
            steps=[SimpleNamespace(step_id="scan", status="failed", step_type="fanout_step", error="item-1: timeout")],
            leaf_calls=[
                SimpleNamespace(step_id="scan", leaf_id="item-0", status="done", error=None, token_usage={"total": 42}),
                SimpleNamespace(step_id="scan", leaf_id="item-1", status="failed", error="timeout", token_usage=None),
            ],
        ),
    )

    resp = client.get(f"/agents/{agent_id}/workflows/runs/{run_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["definition_source"] == "dynamic_workflow"
    assert body["dynamic_workflow"]["proposal_id"] == "proposal-1"
    assert body["outcome_evidence"]["leaf_failed"] == 1
    assert body["repair_plan"]["strategy"] == "resume_failed_leaves"
    assert body["leaf_calls"][1]["leaf_id"] == "item-1"
    assert body["leaf_calls"][1]["status"] == "failed"


def test_get_run_404_when_run_belongs_to_another_agent(monkeypatch):
    """Run↔agent binding: agent A's URL must not expose agent B's run."""
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=uuid.uuid4(), tenant_id=user.tenant_id),
            steps=[],
        ),
    )
    resp = client.get(f"/agents/{uuid.uuid4()}/workflows/runs/{run_id}")
    assert resp.status_code == 404


def test_get_run_404_when_tenant_mirror_mismatch(monkeypatch):
    """runtime_tasks has no tenant column — the metadata mirror is the
    tenant boundary and MUST be enforced at the API."""
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=agent_id, tenant_id=uuid.uuid4()),  # foreign tenant
            steps=[],
        ),
    )
    resp = client.get(f"/agents/{agent_id}/workflows/runs/{run_id}")
    assert resp.status_code == 404


def test_cancel_run_kills(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    killed: list = []
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id, status="running"),
            steps=[],
        ),
    )

    async def fake_kill(self, rid, *, tenant_id=None):
        killed.append(rid)

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "kill_run", fake_kill)
    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/cancel")
    assert resp.status_code == 200
    assert killed == [run_id]


def test_cancel_404_for_foreign_run_and_never_kills(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    killed: list = []
    run_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=uuid.uuid4(), tenant_id=user.tenant_id, status="running"),
            steps=[],
        ),
    )

    async def fake_kill(self, rid, *, tenant_id=None):
        killed.append(rid)

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "kill_run", fake_kill)
    resp = client.post(f"/agents/{uuid.uuid4()}/workflows/runs/{run_id}/cancel")
    assert resp.status_code == 404
    assert killed == []  # the kill must NOT happen


def test_repair_run_resumes_owned_failed_dynamic_run(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_dynamic_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id),
            steps=[SimpleNamespace(step_id="scan", status="failed", step_type="fanout_step", error="item-1: timeout")],
            leaf_calls=[],
        ),
    )
    calls: list[dict] = []
    attempts: list[dict] = []

    async def fake_resume(self, rid, *, tenant_id=None, leaf_executor=None):
        calls.append({"run_id": rid, "tenant_id": tenant_id, "leaf_executor": leaf_executor})
        return WorkflowRunOutcome(status="completed", reason="repaired")

    async def fake_record_attempt(self, rid, *, tenant_id=None):
        attempts.append({"run_id": rid, "tenant_id": tenant_id})

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "resume_run", fake_resume)
    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "record_dynamic_repair_attempt", fake_record_attempt)
    monkeypatch.setattr(workflows_api, "build_resumable_workflow_leaf_executor", lambda: "executor", raising=False)

    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/repair")

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert calls[0]["run_id"] == run_id
    assert str(calls[0]["tenant_id"]) == str(user.tenant_id)
    assert calls[0]["leaf_executor"] == "executor"
    assert attempts == [{"run_id": run_id, "tenant_id": user.tenant_id}]


def test_repair_run_rejects_completed_run(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_dynamic_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id, status="completed"),
            steps=[],
            leaf_calls=[],
        ),
    )

    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/repair")

    assert resp.status_code == 409


def test_repair_run_rejects_non_repairable_dynamic_plan(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    def loaded_run(_rid):
        task = _dynamic_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id)
        task.metadata_json["dynamic_workflow"]["repair_plan"] = {
            "repairable": False,
            "strategy": "resume_failed_leaves",
            "repair_attempts": 1,
            "repair_rounds": 1,
        }
        return SimpleNamespace(task=task, steps=[], leaf_calls=[])

    _patch_load(monkeypatch, loaded_run)
    calls: list = []

    async def fake_resume(self, rid, *, tenant_id=None, leaf_executor=None):
        calls.append(rid)
        return WorkflowRunOutcome(status="completed", reason="should-not-run")

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "resume_run", fake_resume)

    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/repair")

    assert resp.status_code == 409
    assert calls == []


# ── run history list (asset view) ──────────────────────────────────


def test_list_runs_returns_archived_summaries(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    promoted_id = uuid.uuid4()
    calls: list = []

    async def fake_list(self, aid, *, tenant_id=None, limit=50):
        calls.append({"agent_id": aid, "tenant_id": tenant_id, "limit": limit})
        return [
            SimpleNamespace(
                task=_run_task(run_id=run_id, agent_id=aid, tenant_id=user.tenant_id),
                step_counts={"done": 2, "failed": 1},
                promoted_definition_id=promoted_id,
            )
        ]

    monkeypatch.setattr(workflows_api.WorkflowRuntimeService, "list_runs_for_agent", fake_list, raising=False)
    resp = client.get(f"/agents/{agent_id}/workflows/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    run = body[0]
    assert run["run_id"] == str(run_id)
    assert run["name"] == "contract-batch"
    assert run["description"] == "OCR → extract → risk table"
    assert run["definition_source"] == "ephemeral"
    assert run["status"] == "completed"
    assert run["steps_total"] == 3
    assert run["steps_done"] == 2
    assert run["steps_failed"] == 1
    assert run["promoted_definition_id"] == str(promoted_id)
    assert run["created_at"].startswith("2026-06-05")
    # the service is called with the ACCESS-CHECKED agent/tenant, not raw input
    assert calls[0]["agent_id"] == agent_id
    assert str(calls[0]["tenant_id"]) == str(user.tenant_id)


# ── promote from run (固化) ────────────────────────────────────────


def _promote_client(user, monkeypatch, *, access_level="manage"):
    from app.api.workflow_definitions import get_workflow_definition_service

    client = _client(user, monkeypatch, access_level=access_level)
    created: list[dict] = []

    class FakeDefinitionService:
        async def create_draft(self, **kwargs):
            created.append(kwargs)
            definition = kwargs["definition_data"]
            return SimpleNamespace(
                id=uuid.uuid4(),
                name=definition["name"],
                definition_version=1,
                definition_hash="newhash",
                definition_json=definition,
                status="draft",
                visibility_scope=kwargs.get("visibility_scope", "agent"),
                owner_type=kwargs.get("owner_type", "agent"),
                owner_id=kwargs.get("owner_id"),
                call_policy=None,
                promoted_from_run_id=kwargs.get("promoted_from_run_id"),
            )

    client.app.dependency_overrides[get_workflow_definition_service] = lambda: FakeDefinitionService()
    client.created = created
    return client


def test_promote_run_creates_draft_with_provenance(monkeypatch):
    user = _user()
    client = _promote_client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id),
            steps=[],
        ),
    )
    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/promote")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft"
    assert body["promoted_from_run_id"] == str(run_id)
    assert body["description"] == "OCR → extract → risk table"
    kwargs = client.created[0]
    assert kwargs["promoted_from_run_id"] == run_id
    assert kwargs["owner_type"] == "agent"
    assert kwargs["owner_id"] == agent_id
    assert kwargs["created_by_user_id"] == user.id
    assert kwargs["definition_data"]["name"] == "contract-batch"


def test_promote_run_requires_manage_access(monkeypatch):
    user = _user()
    client = _promote_client(user, monkeypatch, access_level="use")
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id),
            steps=[],
        ),
    )
    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/promote")
    assert resp.status_code == 403
    assert client.created == []


def test_promote_run_rejects_uncompleted_run(monkeypatch):
    user = _user()
    client = _promote_client(user, monkeypatch)
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    _patch_load(
        monkeypatch,
        lambda rid: SimpleNamespace(
            task=_run_task(run_id=run_id, agent_id=agent_id, tenant_id=user.tenant_id, status="running"),
            steps=[],
        ),
    )
    resp = client.post(f"/agents/{agent_id}/workflows/runs/{run_id}/promote")
    assert resp.status_code == 409
    assert client.created == []


# ── promote suggestions ────────────────────────────────────────────


def test_promote_suggestions_returns_agent_scoped_payload(monkeypatch):
    user = _user()
    client = _client(user, monkeypatch)
    agent_id = uuid.uuid4()
    sample = uuid.uuid4()
    calls: list = []

    async def fake_collect(*, tenant_id, agent_id=None, **kwargs):
        calls.append({"tenant_id": tenant_id, "agent_id": agent_id})
        return [
            SimpleNamespace(
                definition_hash="abc",
                name="contract-batch",
                run_count=3,
                sample_run_ids=[sample],
                quality_evidence={"promotion_eligible": True, "leaf_failed": 0},
            )
        ]

    monkeypatch.setattr(workflows_api, "collect_promote_suggestions", fake_collect, raising=False)
    resp = client.get(f"/agents/{agent_id}/workflows/promote-suggestions")
    assert resp.status_code == 200
    body = resp.json()
    assert body == [
        {
            "definition_hash": "abc",
            "name": "contract-batch",
            "run_count": 3,
            "sample_run_ids": [str(sample)],
            "quality_evidence": {"promotion_eligible": True, "leaf_failed": 0},
        }
    ]
    assert calls[0]["agent_id"] == agent_id
    assert str(calls[0]["tenant_id"]) == str(user.tenant_id)
