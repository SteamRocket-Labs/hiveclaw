from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4


def _snapshot(**overrides):
    from app.services.runtime_replay_policy import RuntimeReplaySnapshot

    values = {
        "task_id": uuid4(),
        "task_type": "trigger",
        "status": "running",
        "claim_version": 4,
        "claimed_by": "worker-a",
        "claim_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        "child_session_id": str(uuid4()),
        "metadata": {"side_effect_risk": "mutating"},
    }
    values.update(overrides)
    return RuntimeReplaySnapshot(**values)


def _restart_contract(task_id, task_type):
    return {
        "schema": "runtime_restart_replay_contract.v1",
        "idempotency_key": f"{task_type}:{task_id}:restart",
        "task_type": task_type,
        "task_id": str(task_id),
        "mode": "durable_restart_replay",
        "requires_completion_journal": True,
    }


def test_replay_policy_never_mutates_a_live_running_claim():
    from app.services.runtime_replay_policy import runtime_replay_disposition

    disposition = runtime_replay_disposition(
        _snapshot(claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    )

    assert disposition.action == "ignore_live_claim"


def test_replay_policy_reconciles_expired_session_bound_mutating_trigger():
    from app.services.runtime_replay_policy import runtime_replay_disposition

    disposition = runtime_replay_disposition(_snapshot())

    assert disposition.action == "needs_reconciliation"
    assert disposition.reason == "expired_session_bound_or_mutating_runtime"


def test_replay_policy_allows_only_stable_deterministic_workflow_trigger_replay():
    from app.services.runtime_replay_policy import runtime_replay_disposition

    trigger_a, trigger_b = uuid4(), uuid4()
    task_id = uuid4()
    stable = _snapshot(
        task_id=task_id,
        child_session_id=str(uuid4()),
        metadata={
            "side_effect_risk": "mutating",
            "restart_replay_contract": _restart_contract(task_id, "trigger"),
            "trigger_ids": [str(trigger_a), str(trigger_b)],
            "workflow_batch_protocol": {
                "mode": "deterministic_workflow_ref",
                "trigger_ids": [str(trigger_a), str(trigger_b)],
            },
        },
    )
    drifted = _snapshot(
        metadata={
            **stable.metadata,
            "workflow_batch_protocol": {
                "mode": "deterministic_workflow_ref",
                "trigger_ids": [str(trigger_a)],
            },
        }
    )

    assert runtime_replay_disposition(stable).action == "requeue"
    assert runtime_replay_disposition(drifted).action == "needs_reconciliation"


def test_replay_policy_allows_unstarted_heartbeat_but_not_session_bound_heartbeat():
    from app.services.runtime_replay_policy import runtime_replay_disposition

    unstarted_id = uuid4()
    unstarted = _snapshot(
        task_id=unstarted_id,
        task_type="heartbeat",
        child_session_id=None,
        metadata={
            "side_effect_risk": "internal_governed",
            "restart_replay_contract": _restart_contract(unstarted_id, "heartbeat"),
        },
    )
    session_bound = _snapshot(
        task_type="heartbeat",
        child_session_id=str(uuid4()),
        metadata={"side_effect_risk": "internal_governed"},
    )

    assert runtime_replay_disposition(unstarted).action == "requeue"
    assert runtime_replay_disposition(session_bound).action == "needs_reconciliation"


def test_replay_policy_rejects_expired_read_only_runtime_without_exact_restart_contract():
    from app.services.runtime_replay_policy import runtime_replay_disposition

    task_id = uuid4()
    missing = _snapshot(task_id=task_id, child_session_id=None, metadata={"side_effect_risk": "read_only"})
    drifted = _snapshot(
        task_id=task_id,
        child_session_id=None,
        metadata={
            "side_effect_risk": "read_only",
            "restart_replay_contract": {
                **_restart_contract(task_id, "trigger"),
                "idempotency_key": "wrong-key",
            },
        },
    )

    assert runtime_replay_disposition(missing).reason == "invalid_restart_replay_contract"
    assert runtime_replay_disposition(drifted).reason == "invalid_restart_replay_contract"


def test_replay_policy_reconciles_expired_child_session_subagent_and_delegation_even_if_read_only():
    from app.services.runtime_replay_policy import runtime_replay_disposition

    for task_type in ("subagent", "delegation"):
        task_id = uuid4()
        disposition = runtime_replay_disposition(
            _snapshot(
                task_id=task_id,
                task_type=task_type,
                child_session_id=str(uuid4()),
                metadata={
                    "side_effect_risk": "read_only",
                    "restart_replay_contract": _restart_contract(task_id, task_type),
                },
            )
        )

        assert disposition.action == "needs_reconciliation"
        assert disposition.reason == "expired_session_bound_or_mutating_runtime"
