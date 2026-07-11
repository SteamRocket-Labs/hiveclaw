from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _confirmed_plan(*, scopes: list[dict] | None = None, session_id: str | None = "session-a"):
    from app.services.plan_mode_core import compute_plan_hash

    plan_json = {
        "schema": "hive_plan.v1",
        "intent_type": "in_session_execution",
        "title": "Generate the exact report",
        "objective": "Create one governed report",
        "steps": [{"order": 1, "description": "Create the report"}],
        "success_criteria": ["The report is delivered"],
        "stop_conditions": ["The user cancels"],
        "handoff": {"target": "continue_current_session"},
    }
    if scopes is not None:
        plan_json["authorization_scopes"] = scopes
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        agent_id=uuid4(),
        requested_by_user_id=uuid4(),
        confirmed_by_user_id=uuid4(),
        session_id=session_id,
        runtime_task_id=None,
        status="confirmed",
        plan_version=3,
        plan_hash=compute_plan_hash(plan_json),
        plan_json=plan_json,
        expires_at=None,
    )


def test_canonical_action_artifact_removes_only_authorization_proof_fields():
    from app.services.plan_authorization_lease import canonical_action_artifact

    normalized = canonical_action_artifact(
        {
            "title": "报告：最终版",
            "confirmed_plan_id": "plan-id",
            "confirmed_plan_version": 7,
            "confirmed_plan_hash": "sha256:old",
            "plan_authorization": {"lease_id": "lease-id"},
            "config": {
                "expr": "0 9 * * *",
                "plan_id": "plan-id",
                "plan_version": 7,
                "plan_hash": "sha256:old",
                "business_plan": "retain-this-domain-field",
            },
        }
    )

    assert normalized == {
        "title": "报告：最终版",
        "config": {
            "expr": "0 9 * * *",
            "business_plan": "retain-this-domain-field",
        },
    }


def test_binding_key_is_order_independent_but_changes_for_every_authority_dimension():
    from app.services.plan_authorization_lease import build_plan_authorization_binding

    ids = {
        "tenant_id": uuid4(),
        "agent_id": uuid4(),
        "plan_id": uuid4(),
        "requester_user_id": uuid4(),
        "confirmed_by_user_id": uuid4(),
    }
    base = build_plan_authorization_binding(
        **ids,
        plan_version=2,
        plan_hash="sha256:plan",
        session_id="session-a",
        runtime_task_id=None,
        action_kind="start_long_task",
        target_ref="task:new",
        action_artifact={"description": "A。", "title": "Report"},
    )
    reordered = build_plan_authorization_binding(
        **ids,
        plan_version=2,
        plan_hash="sha256:plan",
        session_id="session-a",
        runtime_task_id=None,
        action_kind="start_long_task",
        target_ref="task:new",
        action_artifact={"title": "Report", "description": "A。"},
    )
    assert base.idempotency_key == reordered.idempotency_key
    assert base.canonical_args_hash == reordered.canonical_args_hash

    variants = [
        {"requester_user_id": uuid4()},
        {"session_id": "session-b"},
        {"action_kind": "create_enabled_trigger"},
        {"target_ref": "task:other"},
        {"action_artifact": {"title": "Report", "description": "A."}},
        {"plan_hash": "sha256:changed"},
    ]
    for delta in variants:
        kwargs = {
            **ids,
            "plan_version": 2,
            "plan_hash": "sha256:plan",
            "session_id": "session-a",
            "runtime_task_id": None,
            "action_kind": "start_long_task",
            "target_ref": "task:new",
            "action_artifact": {"title": "Report", "description": "A。"},
            **delta,
        }
        assert build_plan_authorization_binding(**kwargs).idempotency_key != base.idempotency_key


def test_session_binding_excludes_ephemeral_runtime_task_id():
    from app.services.plan_authorization_lease import build_plan_authorization_binding

    binding = build_plan_authorization_binding(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        plan_id=uuid4(),
        plan_version=1,
        plan_hash="sha256:plan",
        requester_user_id=uuid4(),
        confirmed_by_user_id=uuid4(),
        session_id="stable-session",
        runtime_task_id=uuid4(),
        action_kind="start_long_task",
        target_ref="task:new",
        action_artifact={"title": "Bound task"},
    )

    assert binding.execution_context_type == "session"
    assert binding.execution_context_ref == "stable-session"
    assert binding.runtime_task_id is None


def test_confirmed_plan_builds_one_single_use_lease_per_hash_covered_scope():
    from app.services.plan_authorization_lease import build_plan_authorization_specs

    plan = _confirmed_plan(
        scopes=[
            {
                "action_kind": "start_long_task",
                "target_ref": "task:new",
                "arguments": {"title": "Report", "description": "Exact body"},
                "summary": "Create the approved report task",
            },
            {
                "action_kind": "create_enabled_trigger",
                "target_ref": "trigger:new",
                "arguments": {"type": "cron", "config": {"expr": "0 9 * * 1"}},
                "summary": "Create the approved Monday schedule",
            },
        ]
    )
    now = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)

    specs = build_plan_authorization_specs(plan=plan, confirming_user_id=plan.confirmed_by_user_id, now=now)

    assert [spec.action_kind for spec in specs] == ["start_long_task", "create_enabled_trigger"]
    assert all(spec.max_uses == 1 for spec in specs)
    assert all(spec.expires_at > now for spec in specs)
    assert all(spec.binding.session_id == "session-a" for spec in specs)
    assert len({spec.binding.idempotency_key for spec in specs}) == 2


def test_plan_without_explicit_scope_gets_only_its_exact_handoff_lease():
    from app.services.plan_authorization_lease import build_plan_authorization_specs

    plan = _confirmed_plan(scopes=None)
    specs = build_plan_authorization_specs(
        plan=plan,
        confirming_user_id=plan.confirmed_by_user_id,
        now=datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc),
    )

    assert len(specs) == 1
    spec = specs[0]
    assert spec.action_kind == "continue_plan_session"
    assert spec.target_ref == f"plan:{plan.id}:handoff:continue_current_session"
    assert spec.binding.action_artifact["approved_plan_hash"] == plan.plan_hash


@pytest.mark.parametrize(
    "scope,error_code",
    [
        ({"target_ref": "task:new", "arguments": {}}, "missing_action_kind"),
        ({"action_kind": "start_long_task", "arguments": {}}, "missing_target_ref"),
        ({"action_kind": "start_long_task", "target_ref": "task:new", "arguments": []}, "invalid_arguments"),
        (
            {
                "action_kind": "start_long_task",
                "target_ref": "task:new",
                "arguments": {},
                "max_uses": 2,
            },
            "invalid_max_uses",
        ),
    ],
)
def test_invalid_authorization_scope_fails_closed(scope, error_code):
    from app.services.plan_authorization_lease import PlanAuthorizationLeaseError, build_plan_authorization_specs

    plan = _confirmed_plan(scopes=[scope])
    with pytest.raises(PlanAuthorizationLeaseError) as exc_info:
        build_plan_authorization_specs(
            plan=plan,
            confirming_user_id=plan.confirmed_by_user_id,
            now=datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc),
        )
    assert exc_info.value.code == error_code


def test_stamp_confirmed_plan_provenance_carries_consumed_lease_evidence():
    from app.services.plan_mode_core import stamp_confirmed_plan_provenance

    stamped = stamp_confirmed_plan_provenance(
        {"expr": "0 9 * * *"},
        plan_id="plan-1",
        plan_version=2,
        plan_hash="sha256:plan",
        authorization_lease_id="lease-1",
        canonical_args_hash="args-hash",
        target_ref="trigger:new",
        requester_user_id="user-1",
        session_id="session-1",
        runtime_task_id=None,
        evidence_id="run-1",
    )

    assert stamped["plan_id"] == "plan-1"
    assert stamped["plan_authorization"] == {
        "schema": "hive.plan_authorization_evidence.v1",
        "lease_id": "lease-1",
        "canonical_args_hash": "args-hash",
        "target_ref": "trigger:new",
        "requester_user_id": "user-1",
        "session_id": "session-1",
        "runtime_task_id": None,
        "evidence_id": "run-1",
    }


def test_require_active_plan_authorization_returns_an_immutable_copy():
    from app.services.plan_authorization_lease import require_active_plan_authorization

    evidence = {
        "schema": "hive.plan_authorization_evidence.v1",
        "lease_id": "lease-1",
        "canonical_args_hash": "args-hash",
        "target_ref": "plan:1:handoff:delegation",
        "requester_user_id": "user-1",
        "session_id": "session-1",
        "runtime_task_id": None,
        "evidence_id": "handoff-1",
    }
    plan = SimpleNamespace(metadata_json={"active_plan_authorization": evidence})

    resolved = require_active_plan_authorization(plan)

    assert resolved == evidence
    assert resolved is not evidence


@pytest.mark.parametrize("value", [None, {}, {"schema": "wrong"}, {"schema": "hive.plan_authorization_evidence.v1"}])
def test_require_active_plan_authorization_fails_closed(value):
    from app.services.plan_authorization_lease import PlanAuthorizationLeaseError, require_active_plan_authorization

    plan = SimpleNamespace(metadata_json={"active_plan_authorization": value})

    with pytest.raises(PlanAuthorizationLeaseError) as exc_info:
        require_active_plan_authorization(plan)
    assert exc_info.value.code == "active_evidence_missing"
