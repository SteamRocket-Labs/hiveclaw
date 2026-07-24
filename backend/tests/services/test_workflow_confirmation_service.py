from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.workflow_confirmation import WorkflowPreviewArtifact
from app.services.workflow_confirmation_service import (
    WorkflowConfirmationConflict,
    claim_workflow_preview_record,
    mark_workflow_preview_failed_record,
    mark_workflow_preview_started_record,
    workflow_candidate_preview_id,
)


def _preview(
    *,
    now: datetime | None = None,
    confirmation_required: bool = True,
) -> WorkflowPreviewArtifact:
    created_at = now or datetime.now(UTC)
    return WorkflowPreviewArtifact(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        requested_by_user_id=uuid.uuid4(),
        status="ready",
        artifact_version=1,
        artifact_hash="artifact-hash",
        definition_hash="definition-hash",
        args_hash="args-hash",
        definition_json={"name": "audit", "steps": []},
        args_json={"scope": "runtime"},
        preview_json={
            "confirmation_required": confirmation_required,
            "confirmation_reasons": ["external effect"] if confirmation_required else [],
        },
        expires_at=created_at + timedelta(hours=1),
    )


def test_confirmation_required_preview_rejects_agent_turn_as_user_confirmation() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    preview = _preview(now=now)
    initial_attempt_count = preview.attempt_count

    with pytest.raises(WorkflowConfirmationConflict) as exc:
        claim_workflow_preview_record(
            preview,
            tenant_id=preview.tenant_id,
            agent_id=preview.agent_id,
            session_id=preview.session_id,
            user_id=preview.requested_by_user_id,
            confirmation_source="agent_current_turn_no_confirmation_required",
            confirmation_evidence_id="turn-42",
            now=now,
        )

    assert exc.value.code == "explicit_user_confirmation_required"
    assert exc.value.details == {
        "preview_id": str(preview.id),
        "confirmation_reasons": ["external effect"],
    }
    assert preview.status == "ready"
    assert preview.run_id is None
    assert preview.attempt_count == initial_attempt_count
    assert preview.confirmed_by_user_id is None
    assert preview.confirmation_source is None
    assert preview.confirmation_evidence_id is None


def test_api_explicit_start_binds_user_confirmation_and_deterministic_run() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    preview = _preview(now=now)

    outcome = claim_workflow_preview_record(
        preview,
        tenant_id=preview.tenant_id,
        agent_id=preview.agent_id,
        session_id=preview.session_id,
        user_id=preview.requested_by_user_id,
        confirmation_source="api_explicit_start",
        confirmation_evidence_id="request-42",
        now=now,
    )

    assert outcome == "claimed"
    assert preview.status == "starting"
    assert preview.run_id == preview.id
    assert preview.attempt_count == 1
    assert preview.confirmed_by_user_id == preview.requested_by_user_id
    assert preview.confirmation_source == "api_explicit_start"
    assert preview.confirmation_evidence_id == "request-42"
    assert preview.confirmed_at == now
    assert preview.claim_expires_at and preview.claim_expires_at > now


def test_low_risk_agent_start_records_authorization_without_user_confirmation() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    preview = _preview(now=now, confirmation_required=False)

    outcome = claim_workflow_preview_record(
        preview,
        tenant_id=preview.tenant_id,
        agent_id=preview.agent_id,
        session_id=preview.session_id,
        user_id=preview.requested_by_user_id,
        confirmation_source="agent_current_turn_no_confirmation_required",
        confirmation_evidence_id="turn-42",
        now=now,
    )

    assert outcome == "claimed"
    assert preview.status == "starting"
    assert preview.run_id == preview.id
    assert preview.confirmed_by_user_id is None
    assert preview.confirmed_at is None
    assert preview.confirmation_source == "agent_current_turn_no_confirmation_required"
    assert preview.confirmation_evidence_id == "turn-42"


def test_claim_rejects_unknown_authorization_source_before_mutation() -> None:
    preview = _preview(confirmation_required=False)
    initial_attempt_count = preview.attempt_count

    with pytest.raises(WorkflowConfirmationConflict) as exc:
        claim_workflow_preview_record(
            preview,
            tenant_id=preview.tenant_id,
            agent_id=preview.agent_id,
            session_id=preview.session_id,
            user_id=preview.requested_by_user_id,
            confirmation_source="agent_user_turn",
            confirmation_evidence_id="turn-legacy",
        )

    assert exc.value.code == "invalid_confirmation_source"
    assert preview.status == "ready"
    assert preview.run_id is None
    assert preview.attempt_count == initial_attempt_count


def test_claim_rejects_cross_session_or_cross_user_replay() -> None:
    preview = _preview()

    for field, value in (
        ("session_id", uuid.uuid4()),
        ("user_id", uuid.uuid4()),
        ("agent_id", uuid.uuid4()),
        ("tenant_id", uuid.uuid4()),
    ):
        kwargs = {
            "tenant_id": preview.tenant_id,
            "agent_id": preview.agent_id,
            "session_id": preview.session_id,
            "user_id": preview.requested_by_user_id,
            "confirmation_source": "api_explicit_start",
            "confirmation_evidence_id": "request-1",
        }
        kwargs[field] = value
        with pytest.raises(WorkflowConfirmationConflict) as exc:
            claim_workflow_preview_record(preview, **kwargs)
        assert exc.value.code == "identity_mismatch"


def test_unexpired_claim_is_busy_but_started_preview_replays() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    preview = _preview(now=now)
    identity = {
        "tenant_id": preview.tenant_id,
        "agent_id": preview.agent_id,
        "session_id": preview.session_id,
        "user_id": preview.requested_by_user_id,
        "confirmation_source": "api_explicit_start",
        "confirmation_evidence_id": "request-1",
    }
    assert claim_workflow_preview_record(preview, now=now, **identity) == "claimed"

    with pytest.raises(WorkflowConfirmationConflict) as exc:
        claim_workflow_preview_record(preview, now=now + timedelta(seconds=1), **identity)
    assert exc.value.code == "start_in_progress"

    mark_workflow_preview_started_record(preview, run_id=preview.id, now=now + timedelta(seconds=2))
    assert claim_workflow_preview_record(preview, now=now + timedelta(seconds=3), **identity) == "replay"


def test_failed_preview_retries_same_run_identity_after_lease_release() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    preview = _preview(now=now)
    identity = {
        "tenant_id": preview.tenant_id,
        "agent_id": preview.agent_id,
        "session_id": preview.session_id,
        "user_id": preview.requested_by_user_id,
        "confirmation_source": "api_explicit_start",
        "confirmation_evidence_id": "request-9",
    }
    assert claim_workflow_preview_record(preview, now=now, **identity) == "claimed"
    run_id = preview.run_id
    mark_workflow_preview_failed_record(preview, code="launch_failed", message="database unavailable")

    assert claim_workflow_preview_record(preview, now=now + timedelta(minutes=1), **identity) == "claimed"
    assert preview.run_id == run_id
    assert preview.attempt_count == 2
    assert preview.failure_code is None
    assert preview.failure_message is None


def test_expired_preview_cannot_start() -> None:
    now = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    preview = _preview(now=now)

    with pytest.raises(WorkflowConfirmationConflict) as exc:
        claim_workflow_preview_record(
            preview,
            tenant_id=preview.tenant_id,
            agent_id=preview.agent_id,
            session_id=preview.session_id,
            user_id=preview.requested_by_user_id,
            confirmation_source="api_explicit_start",
            confirmation_evidence_id="request-expired",
            now=now + timedelta(hours=2),
        )
    assert exc.value.code == "preview_expired"
    assert preview.status == "expired"


def test_candidate_preview_identity_is_deterministic_per_proposal_and_candidate() -> None:
    proposal_id = uuid.uuid4()

    first = workflow_candidate_preview_id(proposal_id=proposal_id, candidate_id="fanout-critic")
    replay = workflow_candidate_preview_id(proposal_id=proposal_id, candidate_id="fanout-critic")
    other = workflow_candidate_preview_id(proposal_id=proposal_id, candidate_id="sequential-review")

    assert first == replay
    assert first != other
