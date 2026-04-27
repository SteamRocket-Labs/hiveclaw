from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_approve_objective_activates_proposed_goal_and_records_actor():
    from app.services.objective_approval import apply_objective_approval

    actor_id = uuid4()
    objective = SimpleNamespace(
        status="proposed",
        metadata_json={"requires_approval": True},
        blocked_reason="waiting for approval",
    )

    changed = apply_objective_approval(objective, actor_id=actor_id, decision="approved")

    assert changed is True
    assert objective.status == "active"
    assert objective.blocked_reason is None
    assert objective.metadata_json["requires_approval"] is False
    assert objective.metadata_json["approval"]["actor_id"] == str(actor_id)


def test_reject_objective_records_rejection_without_activation():
    from app.services.objective_approval import apply_objective_approval

    objective = SimpleNamespace(
        status="proposed",
        metadata_json={"requires_approval": True},
        blocked_reason=None,
    )

    changed = apply_objective_approval(objective, actor_id=uuid4(), decision="rejected", reason="Too risky")

    assert changed is True
    assert objective.status == "rejected"
    assert objective.metadata_json["rejection"]["reason"] == "Too risky"
