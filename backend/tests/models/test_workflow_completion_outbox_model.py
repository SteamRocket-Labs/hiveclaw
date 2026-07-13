from sqlalchemy import CheckConstraint

from app.models.workflow_completion_outbox import WorkflowCompletionOutbox


def test_workflow_completion_outbox_model_enforces_completed_terminal_truth() -> None:
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in WorkflowCompletionOutbox.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert checks["ck_workflow_completion_outbox_terminal_status"] == "terminal_status = 'completed'"


def test_workflow_completion_outbox_model_indexes_match_upgrade_contract() -> None:
    assert {index.name for index in WorkflowCompletionOutbox.__table__.indexes} == {
        "ix_workflow_completion_outbox_agent_id",
        "ix_workflow_completion_outbox_claim",
        "ix_workflow_completion_outbox_run_id",
        "ix_workflow_completion_outbox_tenant_id",
    }
