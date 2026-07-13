from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _frame_decisions(view: dict, action: str) -> list[dict[str, str]]:
    return [
        {
            "runtime_task_id": frame["runtime_task_id"],
            "tool_call_id": frame["tool_call_id"],
            "tool_name": frame["tool_name"],
            "decision": action,
        }
        for frame in view["recovery_evidence"]["frames"]
    ]


async def _apply_reviewed(
    session,
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID,
    action: str,
    reason: str,
    actor_user_id: uuid.UUID,
    operation_id: str | None = None,
):
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    view = await get_runtime_reconciliation_task(
        session,
        task_id=task_id,
        tenant_id=tenant_id,
    )
    assert view is not None
    return await apply_runtime_reconciliation_action(
        session,
        task_id=task_id,
        tenant_id=tenant_id,
        action=action,
        reason=reason,
        actor_user_id=actor_user_id,
        confirmed=True,
        evidence_digest=view["recovery_evidence"]["digest"],
        frame_decisions=_frame_decisions(view, action),
        operation_id=operation_id,
    )


def test_canonical_recovery_evidence_is_stable_deduped_and_secret_free() -> None:
    from app.services.runtime_reconciliation import _canonical_recovery_evidence

    carrier_id = uuid.uuid4()
    prior_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    metadata = {
        "recovery_resolution_targets": [
            {
                "agent_id": str(agent_id),
                "session_id": "session-1",
                "runtime_task_id": str(carrier_id),
                "source": "carrier_run",
                "expected_manifest_state": "missing",
                "expected_manifest_ref": None,
                "expected_sha256": None,
            },
            {
                "agent_id": str(agent_id),
                "session_id": "session-1",
                "runtime_task_id": str(prior_id),
                "source": "prior_run",
                "expected_manifest_state": "present",
                "expected_manifest_ref": "runtime_artifacts/recovery_manifests/prior.json",
                "expected_sha256": "a" * 64,
            },
        ],
        "prior_run_recovery_reconciliations": [
            {
                "source_runtime_task_id": str(prior_id),
                "status": "needs_reconciliation",
                "frames": [
                    {
                        "tool_call_id": "call-prior",
                        "tool_name": "send_email",
                        "status": "needs_reconciliation",
                        "arguments": {"access_token": "must-not-leak"},
                    }
                ],
            }
        ],
        "recovery_tool_frames": [
            {
                "runtime_task_id": str(prior_id),
                "tool_call_id": "call-prior",
                "tool_name": "send_email",
                "secret": "must-not-leak",
            },
            {
                "runtime_task_id": str(carrier_id),
                "tool_call_id": "call-carrier",
                "tool_name": "write_file",
                "tool_args": {"content": "must-not-leak"},
            },
        ],
    }
    evidence = _canonical_recovery_evidence(SimpleNamespace(id=carrier_id, metadata_json=metadata))
    reordered = _canonical_recovery_evidence(
        SimpleNamespace(
            id=carrier_id,
            metadata_json={
                **metadata,
                "recovery_resolution_targets": list(reversed(metadata["recovery_resolution_targets"])),
                "recovery_tool_frames": list(reversed(metadata["recovery_tool_frames"])),
            },
        )
    )

    assert evidence["evidence_complete"] is True
    assert len(evidence["frames"]) == 2
    assert evidence["digest"] == reordered["digest"]
    assert len(evidence["digest"]) == 64
    serialized = json.dumps(evidence, sort_keys=True)
    assert "must-not-leak" not in serialized
    assert "arguments" not in serialized
    assert "tool_args" not in serialized
    assert "secret" not in serialized


def test_unreviewed_business_task_target_is_incomplete_until_manifest_evidence_is_refreshed(
    monkeypatch,
    tmp_path,
) -> None:
    from app.config import get_settings
    from app.runtime.recovery_manifest import persist_recovery_manifest_checkpoint
    from app.runtime.session import SessionContext
    from app.services.runtime_reconciliation import (
        _canonical_recovery_evidence,
        _refresh_manifest_evidence_snapshot,
    )

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    task_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = f"business-task-run-{task_id.hex}"
    original_target = {
        "agent_id": str(agent_id),
        "session_id": session_id,
        "runtime_task_id": str(task_id),
        "source": "business_task",
    }
    original = _canonical_recovery_evidence(
        SimpleNamespace(
            id=task_id,
            metadata_json={
                "recovery_resolution_targets": [original_target],
                "recovery_tool_frames": [
                    {
                        "runtime_task_id": str(task_id),
                        "tool_call_id": "call-business-write",
                        "tool_name": "write_file",
                        "status": "needs_reconciliation",
                    }
                ],
            },
        )
    )

    assert original["evidence_complete"] is False
    assert "malformed_recovery_targets" in original["incomplete_reasons"]

    session = SessionContext(
        session_id=session_id,
        metadata={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "runtime_task_id": str(task_id),
            "claim_version": 1,
            "claim_worker_id": "business-task:worker",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-business-write",
                    "tool_name": "write_file",
                    "status": "running",
                }
            ],
        },
    )
    persist_recovery_manifest_checkpoint(agent_id, session, data_root=tmp_path)

    refreshed = _refresh_manifest_evidence_snapshot(
        targets=[original_target],
        tenant_id=tenant_id,
    )
    refreshed_target = refreshed["targets"][0]
    assert refreshed_target["expected_manifest_state"] == "present"
    assert refreshed_target["expected_manifest_ref"]
    assert len(refreshed_target["expected_sha256"]) == 64
    refreshed_evidence = _canonical_recovery_evidence(
        SimpleNamespace(
            id=task_id,
            metadata_json={
                "recovery_resolution_targets": refreshed["targets"],
                "recovery_tool_frames": refreshed["frames"],
            },
        )
    )
    assert refreshed_evidence["evidence_complete"] is True


@pytest.mark.parametrize("reviewed_state", ["present", "corrupt", "identity_mismatch", "incomplete_authority"])
def test_canonical_recovery_evidence_requires_byte_cas_for_readable_states(reviewed_state: str) -> None:
    from app.services.runtime_reconciliation import _canonical_recovery_evidence

    task_id = uuid.uuid4()
    target = {
        "agent_id": str(uuid.uuid4()),
        "session_id": "reviewed-session",
        "runtime_task_id": str(task_id),
        "source": "business_task",
        "expected_manifest_state": reviewed_state,
        "expected_manifest_ref": None,
        "expected_sha256": None,
    }
    evidence = _canonical_recovery_evidence(
        SimpleNamespace(
            id=task_id,
            metadata_json={
                "recovery_resolution_targets": [target],
                "recovery_tool_frames": [
                    {
                        "runtime_task_id": str(task_id),
                        "tool_call_id": "call-reviewed-state",
                        "tool_name": "write_file",
                    }
                ],
            },
        )
    )

    assert evidence["evidence_complete"] is False
    assert "recovery_target_byte_evidence_missing" in evidence["incomplete_reasons"]


@pytest.mark.parametrize(
    "missing_key",
    ["expected_manifest_state", "expected_manifest_ref", "expected_sha256"],
)
def test_canonical_recovery_evidence_rejects_raw_target_with_missing_review_key(missing_key: str) -> None:
    from app.services.runtime_reconciliation import _canonical_recovery_evidence

    task_id = uuid.uuid4()
    target = {
        "agent_id": str(uuid.uuid4()),
        "session_id": "raw-target-session",
        "runtime_task_id": str(task_id),
        "source": "system_plan_run",
        "expected_manifest_state": "missing",
        "expected_manifest_ref": None,
        "expected_sha256": None,
    }
    target.pop(missing_key)

    evidence = _canonical_recovery_evidence(
        SimpleNamespace(
            id=task_id,
            metadata_json={
                "recovery_resolution_targets": [target],
                "recovery_tool_frames": [
                    {
                        "runtime_task_id": str(task_id),
                        "tool_call_id": "call-raw-target",
                        "tool_name": "write_file",
                    }
                ],
            },
        )
    )

    assert evidence["evidence_complete"] is False
    assert evidence["targets"] == []
    assert "malformed_recovery_targets" in evidence["incomplete_reasons"]


@pytest.mark.parametrize(
    "missing_key",
    ["recovery_manifest_state", "recovery_manifest_ref", "recovery_manifest_sha256"],
)
def test_canonical_recovery_evidence_rejects_legacy_target_with_missing_review_key(missing_key: str) -> None:
    from app.services.runtime_reconciliation import _canonical_recovery_evidence

    task_id = uuid.uuid4()
    metadata = {
        "recovery_agent_id": str(uuid.uuid4()),
        "recovery_session_id": "legacy-target-session",
        "recovery_runtime_task_id": str(task_id),
        "recovery_manifest_state": "missing",
        "recovery_manifest_ref": None,
        "recovery_manifest_sha256": None,
        "recovery_tool_frames": [
            {
                "runtime_task_id": str(task_id),
                "tool_call_id": "call-legacy-target",
                "tool_name": "write_file",
            }
        ],
    }
    metadata.pop(missing_key)

    evidence = _canonical_recovery_evidence(SimpleNamespace(id=task_id, metadata_json=metadata))

    assert evidence["evidence_complete"] is False
    assert evidence["targets"] == []
    assert "malformed_recovery_targets" in evidence["incomplete_reasons"]


@pytest.mark.parametrize("lane", ["raw", "legacy"])
def test_explicit_null_manifest_bytes_are_reviewed_evidence_for_missing_state(lane: str) -> None:
    from app.services.runtime_reconciliation import _canonical_recovery_evidence

    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    if lane == "raw":
        metadata = {
            "recovery_resolution_targets": [
                {
                    "agent_id": str(agent_id),
                    "session_id": "reviewed-missing-session",
                    "runtime_task_id": str(task_id),
                    "source": "system_plan_run",
                    "expected_manifest_state": "missing",
                    "expected_manifest_ref": None,
                    "expected_sha256": None,
                }
            ]
        }
    else:
        metadata = {
            "recovery_agent_id": str(agent_id),
            "recovery_session_id": "reviewed-missing-session",
            "recovery_runtime_task_id": str(task_id),
            "recovery_manifest_state": "missing",
            "recovery_manifest_ref": None,
            "recovery_manifest_sha256": None,
        }
    metadata["recovery_tool_frames"] = [
        {
            "runtime_task_id": str(task_id),
            "tool_call_id": "call-reviewed-missing",
            "tool_name": "write_file",
        }
    ]

    evidence = _canonical_recovery_evidence(SimpleNamespace(id=task_id, metadata_json=metadata))

    assert evidence["evidence_complete"] is True
    assert evidence["targets"][0]["expected_manifest_state"] == "missing"
    assert evidence["targets"][0]["expected_manifest_ref"] is None
    assert evidence["targets"][0]["expected_sha256"] is None


def test_completed_retry_operation_is_consumed_only_by_a_new_claim_epoch() -> None:
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        consume_completed_reconciliation_retry,
    )

    operation_id = uuid.uuid4().hex
    metadata = {
        "needs_reconciliation": False,
        "reconciliation_status": "retry_requested",
        "reconciliation_operation": {
            "schema": "runtime_reconciliation_operation.v2",
            "operation_id": operation_id,
            "status": "completed",
            "action": "retry",
        },
    }

    consumed = consume_completed_reconciliation_retry(metadata, next_claim_version=9)

    assert "reconciliation_operation" not in consumed
    assert consumed["reconciliation_status"] == "retry_in_progress"
    assert consumed["reconciliation_retry_claim_version"] == 9
    assert consumed["consumed_reconciliation_operations"][-1]["operation_id"] == operation_id

    with pytest.raises(RuntimeReconciliationConflict, match="not an approved completed retry"):
        consume_completed_reconciliation_retry(
            {
                **metadata,
                "reconciliation_operation": {
                    **metadata["reconciliation_operation"],
                    "action": "mark_resolved",
                },
            },
            next_claim_version=9,
        )


@pytest.fixture()
async def tenant_ids(owner_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    from app.models.tenant import Tenant

    first = uuid.uuid4()
    second = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=first, name="reconcile-a", slug=f"ra-{first.hex[:10]}"))
        session.add(Tenant(id=second, name="reconcile-b", slug=f"rb-{second.hex[:10]}"))
    return first, second


@pytest.fixture()
async def operator_user_id(owner_sessionmaker, tenant_ids) -> uuid.UUID:
    from app.models.user import User

    tenant_id, _other = tenant_ids
    user_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"reconcile-{user_id.hex[:10]}",
                email=f"reconcile-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Reconciliation Operator",
                tenant_id=tenant_id,
                role="platform_admin",
            )
        )
        await session.commit()
    return user_id


async def _add_runtime_task(
    session,
    *,
    tenant_id: uuid.UUID,
    status: str = "needs_reconciliation",
    metadata: dict | None = None,
    task_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    session_id: str = "session-1",
    task_type: str = "delegation",
) -> RuntimeTask:
    bound_agent_id = agent_id or uuid.uuid4()
    runtime_task_id = task_id or uuid.uuid4()
    default_evidence = {
        "recovery_resolution_targets": [
            {
                "agent_id": str(bound_agent_id),
                "session_id": session_id,
                "runtime_task_id": str(runtime_task_id),
                "source": "current_run",
                "expected_manifest_state": "missing",
                "expected_manifest_ref": None,
                "expected_sha256": None,
            }
        ],
        "recovery_tool_frames": [
            {
                "runtime_task_id": str(runtime_task_id),
                "tool_call_id": f"call-{runtime_task_id.hex[:12]}",
                "tool_name": "write_file",
                "status": "needs_reconciliation",
            }
        ],
    }
    task = RuntimeTask(
        id=runtime_task_id,
        task_type=task_type,
        status=status,
        tenant_id=tenant_id,
        parent_agent_id=bound_agent_id,
        child_agent_id=bound_agent_id,
        child_agent_name="worker",
        parent_session_id=session_id,
        child_session_id=session_id,
        prompt="mutating work",
        result_summary="Restart interrupted a mutating run.",
        metadata_json={
            "needs_reconciliation": status == "needs_reconciliation",
            "reconciliation_reason": "missing_completion_journal",
            "side_effect_risk": "mutating",
            **default_evidence,
            **(metadata or {}),
        },
    )
    session.add(task)
    await session.flush()
    return task


@pytest.mark.parametrize("lane", ["raw", "legacy"])
async def test_operator_action_rejects_missing_review_key_without_state_change(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    lane: str,
):
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    tenant_id, _other = tenant_ids
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = f"missing-review-key-{lane}"
    if lane == "raw":
        metadata = {
            "recovery_resolution_targets": [
                {
                    "agent_id": str(agent_id),
                    "session_id": session_id,
                    "runtime_task_id": str(task_id),
                    "source": "system_plan_run",
                    "expected_manifest_state": "missing",
                    # expected_manifest_ref is intentionally absent.
                    "expected_sha256": None,
                }
            ]
        }
    else:
        metadata = {
            "recovery_resolution_targets": None,
            "recovery_agent_id": str(agent_id),
            "recovery_session_id": session_id,
            "recovery_runtime_task_id": str(task_id),
            "recovery_manifest_state": "missing",
            "recovery_manifest_ref": None,
            # recovery_manifest_sha256 is intentionally absent.
        }

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            task_type="system_plan_run",
            metadata=metadata,
        )
        before = {
            "status": task.status,
            "claim_version": task.claim_version,
            "claimed_by": task.claimed_by,
            "metadata": json.loads(json.dumps(task.metadata_json)),
        }

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
        assert view is not None
        with pytest.raises(RuntimeReconciliationConflict, match="Canonical recovery evidence is incomplete"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="reject target with missing reviewed evidence key",
                actor_user_id=operator_user_id,
                confirmed=True,
                evidence_digest=view["recovery_evidence"]["digest"],
                frame_decisions=_frame_decisions(view, "mark_resolved"),
                operation_id=None,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await session.get(RuntimeTask, task_id)
        assert persisted is not None
        assert persisted.status == before["status"]
        assert persisted.claim_version == before["claim_version"]
        assert persisted.claimed_by == before["claimed_by"]
        assert persisted.metadata_json == before["metadata"]


async def test_list_runtime_reconciliation_tasks_filters_by_tenant_and_status(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import list_runtime_reconciliation_tasks

    tenant_id, other_tenant_id = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        expected = await _add_runtime_task(session, tenant_id=tenant_id)
        await _add_runtime_task(session, tenant_id=tenant_id, status="running")
        await _add_runtime_task(session, tenant_id=other_tenant_id)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)

    assert [row["task_id"] for row in rows] == [str(expected.id)]
    assert rows[0]["reason"] == "missing_completion_journal"
    assert rows[0]["retry_allowed"] is False


async def test_generic_reconciliation_excludes_and_rejects_specialized_business_tasks(
    owner_sessionmaker,
    tenant_ids,
):
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        list_runtime_reconciliation_tasks,
    )

    tenant_id, _other = tenant_ids
    business_task_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        runtime_task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="business_task",
            metadata={"business_task_id": str(business_task_id)},
        )
        agent_id = runtime_task.parent_agent_id

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        assert rows == []
        with pytest.raises(
            RuntimeReconciliationConflict,
            match=rf"/agents/{agent_id}/tasks/{business_task_id}/reconcile",
        ):
            await _apply_reviewed(
                session,
                task_id=runtime_task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="use the specialized business task reconciler",
                actor_user_id=uuid.uuid4(),
            )


async def test_runtime_reconciliation_retry_is_fail_closed_without_retry_contract(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="not marked retryable"):
            await _apply_reviewed(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="retry",
                reason="try again",
                actor_user_id=uuid.uuid4(),
            )


async def test_action_recomputes_digest_and_requires_exact_frame_decision_set(
    owner_sessionmaker,
    tenant_ids,
):
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task_id = task.id
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
    assert view is not None
    decisions = _frame_decisions(view, "mark_resolved")
    common = {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "action": "mark_resolved",
        "reason": "verified canonical frame evidence",
        "actor_user_id": uuid.uuid4(),
        "operation_id": None,
    }

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="confirmation"):
            await apply_runtime_reconciliation_action(
                session,
                **common,
                confirmed=False,
                evidence_digest=view["recovery_evidence"]["digest"],
                frame_decisions=decisions,
            )
        with pytest.raises(RuntimeReconciliationConflict, match="digest changed"):
            await apply_runtime_reconciliation_action(
                session,
                **common,
                confirmed=True,
                evidence_digest="f" * 64,
                frame_decisions=decisions,
            )
        with pytest.raises(RuntimeReconciliationConflict, match="exactly match"):
            await apply_runtime_reconciliation_action(
                session,
                **common,
                confirmed=True,
                evidence_digest=view["recovery_evidence"]["digest"],
                frame_decisions=[],
            )


async def test_runtime_reconciliation_safe_retry_reopens_task(owner_sessionmaker, tenant_ids, operator_user_id):
    from app.services.runtime_reconciliation import get_runtime_reconciliation_task

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            metadata={"reconciliation_retry_allowed": True, "side_effect_risk": "read_only"},
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await _apply_reviewed(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            action="retry",
            reason="safe read-only retry",
            actor_user_id=operator_user_id,
        )

    assert view["status"] == "pending"
    assert view["metadata"]["reconciliation_status"] == "retry_requested"

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await get_runtime_reconciliation_task(session, task_id=task.id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted["status"] == "pending"


async def test_operator_resolution_atomically_invalidates_the_runtime_claim(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
):
    """A stale worker must lose both its lease and monotonic claim fence.

    The reviewed operator decision is a new authority epoch.  Keeping the old
    ``claim_version`` would let the interrupted worker overwrite the resolved
    row after the filesystem reconciliation has committed.
    """
    from datetime import datetime, timedelta, timezone

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task.claim_version = 7
        task.claimed_by = "stale-system-plan-worker"
        task.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        task_id = task.id

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await _apply_reviewed(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="operator verified the exact recovery frame",
            actor_user_id=operator_user_id,
        )

    assert view["status"] == "completed"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await session.get(RuntimeTask, task_id)
        assert persisted is not None
        assert persisted.claim_version == 8
        assert persisted.claimed_by == "operator-reconciler"
        assert persisted.claim_expires_at is None
        assert persisted.metadata_json["claim_version"] == 8
        assert persisted.metadata_json["claimed_by"] == "operator-reconciler"
        assert persisted.metadata_json["claim_expires_at"] is None
        assert persisted.metadata_json["claim_fence"] == f"{task_id.hex}:8"
        invalidation = persisted.metadata_json["reconciliation_claim_invalidation"]
        assert invalidation["previous_claim_version"] == 7
        assert invalidation["previous_claim_worker_id"] == "stale-system-plan-worker"
        assert invalidation["invalidated_by_operation_id"] == view["reconciliation_operation"]["operation_id"]


async def test_runtime_reconciliation_action_rejects_task_that_is_not_open(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id, status="running")

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="not open for reconciliation"):
            await _apply_reviewed(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="must not close a normal run",
                actor_user_id=uuid.uuid4(),
            )


def test_recovery_resolution_target_count_supports_full_workflow_fanout_and_remains_bounded() -> None:
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        _recovery_resolution_targets,
    )

    supported = _recovery_resolution_targets(
        {
            "recovery_resolution_targets": [
                {
                    "agent_id": str(uuid.uuid4()),
                    "session_id": f"workflow-session-{index}",
                    "runtime_task_id": str(uuid.uuid4()),
                    "expected_manifest_state": "missing",
                    "expected_manifest_ref": None,
                    "expected_sha256": None,
                }
                for index in range(128)
            ]
        }
    )
    assert len(supported) == 128

    with pytest.raises(RuntimeReconciliationConflict, match="exceeds the 200-target limit"):
        _recovery_resolution_targets(
            {
                "recovery_resolution_targets": [
                    {
                        "agent_id": str(uuid.uuid4()),
                        "session_id": "session-1",
                        "runtime_task_id": str(uuid.uuid4()),
                        "expected_manifest_state": "missing",
                        "expected_manifest_ref": None,
                        "expected_sha256": None,
                    }
                    for _index in range(201)
                ]
            }
        )


def test_manifest_refresh_preserves_workflow_authority_and_all_fanout_frames(monkeypatch) -> None:
    from app.services.runtime_reconciliation import _refresh_manifest_evidence_snapshot

    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    targets = [
        {
            "agent_id": str(agent_id),
            "session_id": f"workflow-session-{index}",
            "runtime_task_id": str(run_id),
            "source": "workflow_leaf",
            "workflow_step_id": "fanout",
            "workflow_leaf_id": f"leaf-{index}",
        }
        for index in range(128)
    ]

    def inspect(**kwargs):
        index = int(str(kwargs["session_id"]).rsplit("-", 1)[-1])
        return {
            "state": "valid",
            "receipt": {"ref": f"manifest-{index}.json", "sha256": f"{index:064x}"},
            "expected_checkpoint_seq": 3,
            "expected_claim_version": 2,
            "expected_claim_worker_id": "workflow-worker",
            "pending_tool_frames": [
                {"tool_call_id": f"call-{index}-a", "tool_name": "edit_file"},
                {"tool_call_id": f"call-{index}-b", "tool_name": "send_email"},
            ],
            "recent_tool_outcomes": [],
            "recent_writes": ["workspace/completed.md"] if index == 0 else [],
            "current_turn_writes": [],
        }

    monkeypatch.setattr("app.runtime.recovery_manifest.inspect_recovery_manifest_checkpoint", inspect)

    snapshot = _refresh_manifest_evidence_snapshot(targets=targets, tenant_id=uuid.uuid4())

    assert len(snapshot["targets"]) == 128
    assert snapshot["targets"][0]["workflow_step_id"] == "fanout"
    assert snapshot["targets"][0]["workflow_leaf_id"].startswith("leaf-")
    assert len(snapshot["frames"]) == 257
    assert snapshot["incomplete_reasons"] == []
    assert snapshot["contains_completed_side_effect_evidence"] is True


def test_manifest_refresh_does_not_treat_completed_read_only_outcome_as_side_effect(monkeypatch) -> None:
    from app.services.runtime_reconciliation import _refresh_manifest_evidence_snapshot

    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.inspect_recovery_manifest_checkpoint",
        lambda **_kwargs: {
            "state": "valid",
            "receipt": {"ref": "manifest.json", "sha256": "a" * 64},
            "expected_checkpoint_seq": 2,
            "expected_claim_version": 1,
            "expected_claim_worker_id": "workflow-worker",
            "pending_tool_frames": [{"tool_call_id": "call-edit", "tool_name": "edit_file"}],
            "recent_tool_outcomes": [{"tool": "read_file", "summary": "read completed"}],
            "recent_writes": [],
            "current_turn_writes": [],
        },
    )

    snapshot = _refresh_manifest_evidence_snapshot(
        targets=[
            {
                "agent_id": str(agent_id),
                "session_id": "workflow-leaf-read-only",
                "runtime_task_id": str(run_id),
                "source": "workflow_leaf",
                "workflow_step_id": "edit",
                "workflow_leaf_id": "singleton",
            }
        ],
        tenant_id=uuid.uuid4(),
    )

    assert snapshot["contains_completed_side_effect_evidence"] is False


@pytest.mark.parametrize(
    ("inspection", "state", "expected_incomplete"),
    [
        (None, "missing", []),
        ({"state": "corrupt"}, "corrupt", ["recovery_refresh_manifest_byte_evidence_missing"]),
        ({"state": "nonregular"}, "nonregular", []),
        (
            {"state": "identity_mismatch"},
            "identity_mismatch",
            ["recovery_refresh_manifest_byte_evidence_missing"],
        ),
    ],
)
def test_manifest_refresh_requires_bytes_only_for_readable_checkpoint_states(
    monkeypatch,
    inspection,
    state,
    expected_incomplete,
) -> None:
    from app.services.runtime_reconciliation import _refresh_manifest_evidence_snapshot

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.inspect_recovery_manifest_checkpoint",
        lambda **_kwargs: inspection,
    )

    snapshot = _refresh_manifest_evidence_snapshot(
        targets=[
            {
                "agent_id": str(uuid.uuid4()),
                "session_id": "workflow-leaf-unsafe",
                "runtime_task_id": str(uuid.uuid4()),
                "source": "workflow_leaf",
                "workflow_step_id": "fanout",
                "workflow_leaf_id": "item-0",
            }
        ],
        tenant_id=uuid.uuid4(),
    )

    assert snapshot["incomplete_reasons"] == expected_incomplete
    target = snapshot["targets"][0]
    assert target["expected_manifest_state"] == state
    assert "expected_manifest_ref" in target
    assert "expected_sha256" in target


@pytest.mark.parametrize(
    ("checkpoint_kind", "state"), [("missing", "missing"), ("corrupt", "corrupt"), ("symlink", "nonregular")]
)
def test_manifest_refresh_real_filesystem_non_valid_checkpoints_are_exactly_reviewable(
    monkeypatch,
    tmp_path,
    checkpoint_kind,
    state,
) -> None:
    from app.config import get_settings
    from app.runtime.recovery_manifest import recovery_manifest_path
    from app.services.runtime_reconciliation import _refresh_manifest_evidence_snapshot

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    run_id = uuid.uuid4()
    session_id = f"workflow-leaf-{checkpoint_kind}"
    checkpoint = recovery_manifest_path(
        agent_id,
        session_id=session_id,
        runtime_task_id=run_id,
        data_root=tmp_path,
    )
    if checkpoint_kind == "corrupt":
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text("{not-json", encoding="utf-8")
        checkpoint.chmod(0o600)
    elif checkpoint_kind == "symlink":
        external = tmp_path / "external-manifest.json"
        external.write_text("{}", encoding="utf-8")
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.symlink_to(external)

    snapshot = _refresh_manifest_evidence_snapshot(
        targets=[
            {
                "agent_id": str(agent_id),
                "session_id": session_id,
                "runtime_task_id": str(run_id),
                "source": "workflow_leaf",
                "workflow_step_id": "fanout",
                "workflow_leaf_id": "item-0",
            }
        ],
        tenant_id=tenant_id,
    )

    assert snapshot["incomplete_reasons"] == []
    target = snapshot["targets"][0]
    assert target["expected_manifest_state"] == state
    if state == "corrupt":
        assert target["expected_manifest_ref"]
        assert len(target["expected_sha256"]) == 64
    else:
        assert target["expected_manifest_ref"] is None
        assert target["expected_sha256"] is None


async def test_workflow_leaf_recovery_target_uses_deterministic_journal_authority(
    owner_sessionmaker,
    tenant_ids,
):
    from app.models.workflow import WorkflowLeafCall, WorkflowStep
    from app.runtime.workflow_engine import workflow_leaf_recovery_identity
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        _recovery_resolution_targets,
        _validate_resolution_target_authority,
    )

    tenant_id, _other = tenant_ids
    step_id = "fanout-research"
    leaf_id = "leaf-7"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="workflow",
            session_id="root-chat-session",
        )
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=task.id,
                step_id=step_id,
                step_type="fanout_step",
                status="unknown_requires_reconciliation",
            )
        )
        session.add(
            WorkflowLeafCall(
                tenant_id=tenant_id,
                run_id=task.id,
                step_id=step_id,
                leaf_id=leaf_id,
                status="needs_reconciliation",
            )
        )
        await session.flush()
        identity = workflow_leaf_recovery_identity(task.id, step_id, leaf_id)
        raw_target = {
            "agent_id": str(task.parent_agent_id),
            "session_id": identity.session_id,
            "runtime_task_id": str(task.id),
            "source": "workflow_leaf",
            "workflow_step_id": step_id,
            "workflow_leaf_id": leaf_id,
            "expected_manifest_state": "missing",
            "expected_manifest_ref": None,
            "expected_sha256": None,
        }
        targets = _recovery_resolution_targets({"recovery_resolution_targets": [raw_target]})

        assert targets[0]["workflow_step_id"] == step_id
        assert targets[0]["workflow_leaf_id"] == leaf_id
        rows = await _validate_resolution_target_authority(
            session,
            task=task,
            tenant_id=tenant_id,
            targets=targets,
        )
        assert rows == {task.id: task}

        forged = _recovery_resolution_targets(
            {
                "recovery_resolution_targets": [
                    {**raw_target, "session_id": "workflow-leaf-forged"},
                ]
            }
        )
        with pytest.raises(RuntimeReconciliationConflict, match="deterministic workflow leaf authority"):
            await _validate_resolution_target_authority(
                session,
                task=task,
                tenant_id=tenant_id,
                targets=forged,
            )


async def test_live_workflow_leaf_reconciliation_event_merges_authority_and_journals_atomically(
    owner_sessionmaker,
    tenant_ids,
    monkeypatch,
):
    from app.models.workflow import WorkflowLeafCall, WorkflowStep
    from app.runtime.workflow_engine import workflow_leaf_recovery_identity
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        mark_runtime_task_recovery_reconciliation,
    )

    tenant_id, _other = tenant_ids
    step_id = "fanout-live"
    leaf_ids = ("leaf-a", "leaf-b")
    ledger_updates: list[dict] = []
    monkeypatch.setattr(
        "app.services.agent_work_ledger.upsert_agent_work_ledger_todo",
        lambda **kwargs: ledger_updates.append(kwargs) or {},
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="workflow",
            status="running",
            session_id="workflow-root-session",
        )
        task.claim_version = 4
        task.claimed_by = "workflow-worker-live"
        session.add(
            WorkflowStep(
                tenant_id=tenant_id,
                run_id=task.id,
                step_id=step_id,
                step_type="fanout_step",
                status="running",
            )
        )
        for leaf_id in leaf_ids:
            session.add(
                WorkflowLeafCall(
                    tenant_id=tenant_id,
                    run_id=task.id,
                    step_id=step_id,
                    leaf_id=leaf_id,
                    status="running",
                )
            )
        await session.flush()

        for index, leaf_id in enumerate(leaf_ids):
            identity = workflow_leaf_recovery_identity(task.id, step_id, leaf_id)
            view = await mark_runtime_task_recovery_reconciliation(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                agent_id=task.parent_agent_id,
                session_id=identity.session_id,
                event={
                    "event_type": "tool_execution_reconciliation_required",
                    "tool_name": "send_email",
                    "tool_call_id": f"call-{leaf_id}",
                    "reason": "tool_execution_outcome_unknown",
                    "runtime_failure_policy": {
                        "side_effect_risk": "unknown",
                        "requires_reconciliation": True,
                    },
                },
                recovery_manifest_receipt={
                    "ref": f"manifest-{index}.json",
                    "sha256": f"{index + 1:064x}",
                },
                recovery_authority={
                    "type": "workflow_leaf",
                    "workflow_run_id": str(task.id),
                    "workflow_step_id": step_id,
                    "workflow_leaf_id": leaf_id,
                },
                expected_status="running",
                expected_claim_version=4,
                expected_claim_worker_id="workflow-worker-live",
            )
            assert view is not None

        stale_identity = workflow_leaf_recovery_identity(task.id, step_id, leaf_ids[0])
        with pytest.raises(RuntimeReconciliationConflict, match="stale claim version"):
            await mark_runtime_task_recovery_reconciliation(
                session,
                task_id=task.id,
                tenant_id=tenant_id,
                agent_id=task.parent_agent_id,
                session_id=stale_identity.session_id,
                event={
                    "event_type": "tool_execution_reconciliation_required",
                    "tool_name": "send_email",
                    "tool_call_id": "call-stale-worker",
                },
                recovery_authority={
                    "type": "workflow_leaf",
                    "workflow_run_id": str(task.id),
                    "workflow_step_id": step_id,
                    "workflow_leaf_id": leaf_ids[0],
                },
                expected_status="running",
                expected_claim_version=3,
                expected_claim_worker_id="workflow-worker-live",
            )

        refreshed_task = await session.get(RuntimeTask, task.id)
        assert refreshed_task is not None
        targets = refreshed_task.metadata_json["recovery_resolution_targets"]
        workflow_targets = [target for target in targets if target.get("source") == "workflow_leaf"]
        assert {(target["workflow_step_id"], target["workflow_leaf_id"]) for target in workflow_targets} == {
            (step_id, "leaf-a"),
            (step_id, "leaf-b"),
        }
        workflow_frames = [
            frame
            for frame in refreshed_task.metadata_json["recovery_tool_frames"]
            if frame.get("workflow_step_id") == step_id
        ]
        assert len(workflow_frames) == 2
        assert refreshed_task.metadata_json["needs_reconciliation"] == [step_id]
        assert refreshed_task.metadata_json["reconciliation_retry_allowed"] is False
        assert refreshed_task.metadata_json["recovery_evidence_status"] == "incomplete"
        assert refreshed_task.metadata_json["recovery_evidence_incomplete_reasons"] == [
            "workflow_fanout_evidence_aggregation_pending"
        ]
        step = (
            await session.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == task.id, WorkflowStep.step_id == step_id)
            )
        ).scalar_one()
        leaves = (
            (await session.execute(select(WorkflowLeafCall).where(WorkflowLeafCall.run_id == task.id))).scalars().all()
        )
        assert step.status == "unknown_requires_reconciliation"
        assert {leaf.status for leaf in leaves} == {"needs_reconciliation"}
        assert ledger_updates
        assert ledger_updates[-1]["runtime_task_id"] == task.id
        assert ledger_updates[-1]["status"] == "pending"


async def test_runtime_reconciliation_rejects_target_outside_task_authority(
    owner_sessionmaker,
    tenant_ids,
    monkeypatch,
):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict

    tenant_id, _other = tenant_ids
    foreign_agent_id = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task.metadata_json = {
            **task.metadata_json,
            "recovery_resolution_targets": [
                {
                    "agent_id": str(foreign_agent_id),
                    "session_id": task.parent_session_id,
                    "runtime_task_id": str(task.id),
                    "source": "forged",
                    "expected_manifest_state": "missing",
                    "expected_manifest_ref": None,
                    "expected_sha256": None,
                }
            ],
        }
        task_id = task.id
    filesystem_called = False

    def must_not_resolve(**_kwargs):
        nonlocal filesystem_called
        filesystem_called = True
        raise AssertionError("filesystem resolver must not receive a forged target")

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        must_not_resolve,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="outside RuntimeTask authority"):
            await _apply_reviewed(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="forged target",
                actor_user_id=uuid.uuid4(),
            )

    assert filesystem_called is False


async def test_failed_manifest_batch_keeps_durable_operation_and_task_open(
    owner_sessionmaker,
    tenant_ids,
    monkeypatch,
):
    from app.runtime.recovery_manifest import RecoveryManifestReconciliationError
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        get_runtime_reconciliation_task,
    )

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task.metadata_json = {
            **task.metadata_json,
            "recovery_resolution_targets": [
                {
                    "agent_id": str(task.parent_agent_id),
                    "session_id": task.parent_session_id,
                    "runtime_task_id": str(task.id),
                    "source": "current_run",
                    "expected_manifest_state": "missing",
                    "expected_manifest_ref": None,
                    "expected_sha256": None,
                }
            ],
        }
        task_id = task.id

    def fail_batch(**_kwargs):
        raise RecoveryManifestReconciliationError("target B changed since review")

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        fail_batch,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="target B changed"):
            await _apply_reviewed(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="reviewed both targets",
                actor_user_id=uuid.uuid4(),
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted["status"] == "needs_reconciliation"
    operation = persisted["metadata"]["reconciliation_operation"]
    assert operation["status"] == "failed"
    assert "target B changed" in operation["error"]


async def test_audit_failure_keeps_reconciliation_open_and_resumable(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
):
    from app.services.runtime_reconciliation import get_runtime_reconciliation_task

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task_id = task.id

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(
        "app.services.runtime_reconciliation._write_reconciliation_audit",
        fail_audit,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeError, match="audit store unavailable"):
            await _apply_reviewed(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="verified from durable transcript",
                actor_user_id=operator_user_id,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        persisted = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted["status"] == "needs_reconciliation"
    assert persisted["metadata"]["reconciliation_operation"]["status"] == "prepared"


async def test_concurrent_operators_share_one_operation_and_only_one_closes_task(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
):
    from app.services.runtime_reconciliation import RuntimeReconciliationConflict

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task.metadata_json = {
            **task.metadata_json,
            "recovery_resolution_targets": [
                {
                    "agent_id": str(task.parent_agent_id),
                    "session_id": task.parent_session_id,
                    "runtime_task_id": str(task.id),
                    "source": "current_run",
                    "expected_manifest_state": "missing",
                    "expected_manifest_ref": None,
                    "expected_sha256": None,
                }
            ],
        }
        task_id = task.id

    operation_ids: list[str] = []

    def resolve_batch(**kwargs):
        operation_ids.append(kwargs["operation_id"])
        return [
            {
                "path": "/durable/recovery.json",
                "ref": "runtime_artifacts/recovery.json",
                "sha256": "c" * 64,
                "bytes": 10,
                "ephemeral": False,
                "source": "current_run",
                "operation_id": kwargs["operation_id"],
            }
        ]

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        resolve_batch,
    )

    async def act():
        async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
            return await _apply_reviewed(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="verified from the same evidence set",
                actor_user_id=operator_user_id,
            )

    outcomes = await asyncio.gather(act(), act(), return_exceptions=True)

    successes = [item for item in outcomes if isinstance(item, dict)]
    conflicts = [item for item in outcomes if isinstance(item, RuntimeReconciliationConflict)]
    assert len(successes) == 1
    assert successes[0]["status"] == "completed"
    assert len(conflicts) == 1
    assert len(operation_ids) == 1


async def test_recovery_event_projects_runtime_task_to_operator_reconciliation(owner_sessionmaker, tenant_ids):
    from app.services.runtime_reconciliation import mark_runtime_task_recovery_reconciliation

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id, status="running", metadata={})
        task.parent_session_id = "session-1"

    event = {
        "type": "tool_recovery",
        "event_type": "tool_execution_reconciliation_required",
        "tool_name": "send_email",
        "tool_call_id": "call-email",
        "status": "needs_reconciliation",
        "runtime_failure_policy": {
            "side_effect_risk": "unknown",
            "requires_reconciliation": True,
            "retryable": False,
        },
    }
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await mark_runtime_task_recovery_reconciliation(
            session,
            task_id=task.id,
            tenant_id=tenant_id,
            agent_id=task.parent_agent_id,
            session_id="session-1",
            event=event,
            recovery_manifest_receipt={"ref": "runtime_artifacts/recovery.json", "sha256": "a" * 64},
        )

    assert view is not None
    assert view["status"] == "needs_reconciliation"
    assert view["reason"] == "tool_execution_outcome_unknown"
    assert view["retry_allowed"] is False
    assert any(frame["tool_call_id"] == "call-email" for frame in view["metadata"]["recovery_tool_frames"])
    assert view["metadata"]["recovery_manifest_ref"] == "runtime_artifacts/recovery.json"


async def test_operator_resolution_updates_manifest_before_closing_task(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
):
    tenant_id, _other = tenant_ids
    session_id = "session-1"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            metadata={
                "recovery_agent_id": None,
                "recovery_session_id": session_id,
                "recovery_runtime_task_id": "placeholder",
            },
        )
        task.metadata_json = {
            **task.metadata_json,
            "recovery_agent_id": str(task.parent_agent_id),
            "recovery_session_id": session_id,
            "recovery_runtime_task_id": str(task.id),
        }
        task_id = task.id
        agent_id = task.parent_agent_id

    captured = {}

    def resolve_manifests(**kwargs):
        captured.update(kwargs)
        return [
            {
                "path": "/durable/recovery.json",
                "ref": "runtime_artifacts/recovery.json",
                "sha256": "b" * 64,
                "bytes": 10,
                "ephemeral": False,
                "source": "runtime_task",
            }
        ]

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        resolve_manifests,
    )
    actor_id = operator_user_id
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await _apply_reviewed(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="verified exactly once",
            actor_user_id=actor_id,
        )

    assert captured["targets"][0]["agent_id"] == str(agent_id)
    assert captured["targets"][0]["session_id"] == session_id
    assert captured["targets"][0]["runtime_task_id"] == str(task_id)
    assert captured["actor_user_id"] == actor_id
    assert view["status"] == "completed"
    assert view["metadata"]["reconciliation_status"] == "resolved"
    assert view["metadata"]["recovery_resolution_receipt"]["sha256"] == "b" * 64

    from app.models.audit import AuditLog

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        audit_result = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_id,
                AuditLog.action == "runtime_reconciliation.mark_resolved",
            )
        )
        audit = audit_result.scalar_one_or_none()
    assert audit is not None
    assert audit.user_id == actor_id
    assert audit.details["runtime_task_id"] == str(task_id)
    assert audit.details["operation_id"] == view["metadata"]["reconciliation_operation"]["operation_id"]


async def test_operator_resolution_closes_every_prior_and_carrier_manifest_target(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
):
    tenant_id, _other = tenant_ids
    agent_id = uuid.uuid4()
    prior_run_id = uuid.uuid4()
    carrier_run_id = uuid.uuid4()
    session_id = "session-shared"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            status="completed",
            task_id=prior_run_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_id=carrier_run_id,
            agent_id=agent_id,
            session_id=session_id,
            metadata={
                "recovery_resolution_targets": [
                    {
                        "agent_id": str(agent_id),
                        "session_id": session_id,
                        "runtime_task_id": str(prior_run_id),
                        "source": "prior_run",
                        "expected_manifest_state": "missing",
                        "expected_manifest_ref": None,
                        "expected_sha256": None,
                    },
                    {
                        "agent_id": str(agent_id),
                        "session_id": session_id,
                        "runtime_task_id": str(carrier_run_id),
                        "source": "carrier_run",
                        "expected_manifest_state": "missing",
                        "expected_manifest_ref": None,
                        "expected_sha256": None,
                    },
                ]
            },
        )
        task_id = task.id

    resolved_targets: list[dict] = []

    def resolve_manifests(**kwargs):
        resolved_targets.extend(dict(target) for target in kwargs["targets"])
        return [
            {
                "path": f"/durable/{target['runtime_task_id']}.json",
                "ref": f"runtime_artifacts/{target['runtime_task_id']}.json",
                "sha256": str(target["runtime_task_id"]).replace("-", "")[:32].ljust(64, "0"),
                "bytes": 10,
                "ephemeral": False,
                "source": target["source"],
            }
            for target in kwargs["targets"]
        ]

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        resolve_manifests,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        view = await _apply_reviewed(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="verified all related runs",
            actor_user_id=operator_user_id,
        )

    assert [item["runtime_task_id"] for item in resolved_targets] == sorted([str(prior_run_id), str(carrier_run_id)])
    assert all(item["agent_id"] == str(agent_id) for item in resolved_targets)
    assert view["status"] == "completed"
    assert len(view["metadata"]["recovery_resolution_receipts"]) == 2
    assert {item["source"] for item in view["metadata"]["recovery_resolution_receipts"]} == {
        "prior_run",
        "carrier_run",
    }


async def test_failed_reconciliation_requires_operation_id_and_preserves_decision_actor_on_resume(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
):
    from app.runtime.recovery_manifest import RecoveryManifestReconciliationError
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    tenant_id, _other = tenant_ids
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(session, tenant_id=tenant_id)
        task_id = task.id

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        initial = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
    assert initial is not None
    decisions = _frame_decisions(initial, "mark_resolved")
    calls: list[dict] = []

    def fail_then_succeed(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RecoveryManifestReconciliationError("temporary durable store outage")
        return [
            {
                "ref": "runtime_artifacts/recovery.json",
                "sha256": "d" * 64,
                "source": "current_run",
            }
        ]

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        fail_then_succeed,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="temporary durable store outage"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="verified exactly once",
                actor_user_id=operator_user_id,
                confirmed=True,
                evidence_digest=initial["recovery_evidence"]["digest"],
                frame_decisions=decisions,
                operation_id=None,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        failed = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
    assert failed is not None
    operation_id = failed["reconciliation_operation"]["operation_id"]
    resumed_by = uuid.uuid4()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="operation_id"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="verified exactly once",
                actor_user_id=resumed_by,
                confirmed=True,
                evidence_digest=initial["recovery_evidence"]["digest"],
                frame_decisions=decisions,
                operation_id=None,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        completed = await apply_runtime_reconciliation_action(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="verified exactly once",
            actor_user_id=resumed_by,
            confirmed=True,
            evidence_digest=initial["recovery_evidence"]["digest"],
            frame_decisions=decisions,
            operation_id=operation_id,
        )

    operation = completed["reconciliation_operation"]
    assert operation["actor_user_id"] == str(operator_user_id)
    assert operation["resumed_by_user_id"] == str(resumed_by)
    assert all(call["actor_user_id"] == operator_user_id for call in calls)
    history = completed["metadata"]["reconciliation_history"][-1]
    assert history["evidence_digest"] == initial["recovery_evidence"]["digest"]
    assert history["frame_decisions"] == decisions
    assert history["actor_user_id"] == str(operator_user_id)
    assert history["resumed_by_user_id"] == str(resumed_by)


async def test_manifest_evidence_drift_retires_prepared_operation_and_allows_fresh_review(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.runtime.recovery_manifest import (
        inspect_recovery_manifest_checkpoint,
        persist_recovery_manifest_checkpoint,
    )
    from app.runtime.session import SessionContext
    from app.services.runtime_reconciliation import (
        RuntimeReconciliationConflict,
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
    )

    tenant_id, _other = tenant_ids
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = "system-plan-evidence-drift"
    recovery_session = SessionContext(
        session_id=session_id,
        source="system_plan",
        channel="internal",
        metadata={
            "tenant_id": str(tenant_id),
            "agent_id": str(agent_id),
            "runtime_task_id": task_id.hex,
            "claim_version": 1,
            "claim_worker_id": "system-plan:author",
            "pending_tool_frames": [
                {
                    "tool_call_id": "call-before-drift",
                    "tool_name": "write_file",
                    "status": "needs_reconciliation",
                }
            ],
            "pending_tool_frame": {
                "tool_call_id": "call-before-drift",
                "tool_name": "write_file",
                "status": "needs_reconciliation",
            },
            "recovery_reconciliation_blocked": True,
        },
    )
    [initial_receipt] = persist_recovery_manifest_checkpoint(
        agent_id,
        recovery_session,
        data_root=tmp_path,
    )
    initial_inspection = inspect_recovery_manifest_checkpoint(
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        runtime_task_id=task_id,
        data_root=tmp_path,
    )
    assert initial_inspection is not None and initial_inspection["state"] == "valid"
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        task = await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_type="system_plan_run",
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            metadata={
                "recovery_resolution_targets": [
                    {
                        "agent_id": str(agent_id),
                        "session_id": session_id,
                        "runtime_task_id": str(task_id),
                        "source": "current_run",
                        "expected_manifest_state": "present",
                        "expected_manifest_ref": initial_receipt["ref"],
                        "expected_sha256": initial_receipt["sha256"],
                        "expected_checkpoint_seq": initial_inspection["expected_checkpoint_seq"],
                        "expected_claim_version": initial_inspection["expected_claim_version"],
                        "expected_claim_worker_id": initial_inspection["expected_claim_worker_id"],
                    }
                ],
                "recovery_tool_frames": [
                    {
                        "runtime_task_id": str(task_id),
                        "tool_call_id": "call-before-drift",
                        "tool_name": "write_file",
                        "status": "needs_reconciliation",
                    }
                ],
            },
        )
        assert task.id == task_id

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        initial = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
        assert initial is not None

    recovery_session.metadata["pending_tool_frames"] = [
        {
            "tool_call_id": "call-after-drift",
            "tool_name": "send_email",
            "status": "needs_reconciliation",
        }
    ]
    recovery_session.metadata["pending_tool_frame"] = recovery_session.metadata["pending_tool_frames"][0]
    [drifted_receipt] = persist_recovery_manifest_checkpoint(
        agent_id,
        recovery_session,
        data_root=tmp_path,
    )
    assert drifted_receipt["sha256"] != initial_receipt["sha256"]

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        with pytest.raises(RuntimeReconciliationConflict, match="changed since operator review"):
            await apply_runtime_reconciliation_action(
                session,
                task_id=task_id,
                tenant_id=tenant_id,
                action="mark_resolved",
                reason="reviewed exact manifest evidence",
                actor_user_id=operator_user_id,
                confirmed=True,
                evidence_digest=initial["recovery_evidence"]["digest"],
                frame_decisions=_frame_decisions(initial, "mark_resolved"),
                operation_id=None,
            )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        drifted = await get_runtime_reconciliation_task(session, task_id=task_id, tenant_id=tenant_id)
    assert drifted is not None
    assert "reconciliation_operation" not in drifted["metadata"]
    assert drifted["metadata"]["recovery_evidence_status"] == "ready"
    retired = drifted["metadata"]["retired_reconciliation_operations"][-1]
    assert retired["status"] == "evidence_drifted"
    assert retired["evidence_digest"] == initial["recovery_evidence"]["digest"]
    refreshed_target = drifted["recovery_evidence"]["targets"][0]
    assert refreshed_target["expected_sha256"] == drifted_receipt["sha256"]
    assert refreshed_target["expected_checkpoint_seq"] > initial_inspection["expected_checkpoint_seq"]
    assert drifted["recovery_evidence"]["digest"] != initial["recovery_evidence"]["digest"]
    assert [frame["tool_call_id"] for frame in drifted["recovery_evidence"]["frames"]] == ["call-after-drift"]

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        completed = await apply_runtime_reconciliation_action(
            session,
            task_id=task_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="reviewed exact manifest evidence",
            actor_user_id=operator_user_id,
            confirmed=True,
            evidence_digest=drifted["recovery_evidence"]["digest"],
            frame_decisions=_frame_decisions(drifted, "mark_resolved"),
            operation_id=None,
        )

    assert completed["status"] == "completed"
    assert completed["metadata"]["reconciliation_status"] == "resolved"


async def test_multi_target_open_rows_are_grouped_and_closed_in_one_operation(
    owner_sessionmaker,
    tenant_ids,
    operator_user_id,
    monkeypatch,
):
    from app.services.runtime_reconciliation import (
        apply_runtime_reconciliation_action,
        get_runtime_reconciliation_task,
        list_runtime_reconciliation_tasks,
    )

    tenant_id, _other = tenant_ids
    agent_id = uuid.uuid4()
    prior_id = uuid.uuid4()
    carrier_id = uuid.uuid4()
    session_id = "session-grouped"
    prior_frame = {
        "tool_call_id": "call-prior-open",
        "tool_name": "send_email",
        "status": "needs_reconciliation",
        "arguments": {"secret": "never-return"},
    }
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_id=prior_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        await _add_runtime_task(
            session,
            tenant_id=tenant_id,
            task_id=carrier_id,
            agent_id=agent_id,
            session_id=session_id,
            metadata={
                "prior_run_recovery_reconciliations": [
                    {
                        "source_runtime_task_id": str(prior_id),
                        "status": "needs_reconciliation",
                        "frames": [prior_frame],
                    }
                ],
                "recovery_resolution_targets": [
                    {
                        "agent_id": str(agent_id),
                        "session_id": session_id,
                        "runtime_task_id": str(prior_id),
                        "source": "prior_run",
                        "expected_manifest_state": "missing",
                        "expected_manifest_ref": None,
                        "expected_sha256": None,
                    },
                    {
                        "agent_id": str(agent_id),
                        "session_id": session_id,
                        "runtime_task_id": str(carrier_id),
                        "source": "carrier_run",
                        "expected_manifest_state": "missing",
                        "expected_manifest_ref": None,
                        "expected_sha256": None,
                    },
                ],
                "recovery_tool_frames": [],
            },
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        queue = await list_runtime_reconciliation_tasks(session, tenant_id=tenant_id)
        carrier_view = await get_runtime_reconciliation_task(session, task_id=carrier_id, tenant_id=tenant_id)
    assert [item["task_id"] for item in queue] == [str(carrier_id)]
    assert carrier_view is not None
    assert carrier_view["recovery_evidence"]["frames"] == [
        {
            "runtime_task_id": str(prior_id),
            "tool_call_id": "call-prior-open",
            "tool_name": "send_email",
            "status": "needs_reconciliation",
            "source": "prior_run",
        }
    ]
    decisions = _frame_decisions(carrier_view, "mark_resolved")

    def resolve_group(**kwargs):
        return [
            {
                "ref": f"runtime_artifacts/{target['runtime_task_id']}.json",
                "sha256": "e" * 64,
                "source": target["source"],
            }
            for target in kwargs["targets"]
        ]

    monkeypatch.setattr(
        "app.runtime.recovery_manifest.resolve_recovery_manifest_reconciliations",
        resolve_group,
    )
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        completed = await apply_runtime_reconciliation_action(
            session,
            task_id=carrier_id,
            tenant_id=tenant_id,
            action="mark_resolved",
            reason="verified grouped prior operation",
            actor_user_id=operator_user_id,
            confirmed=True,
            evidence_digest=carrier_view["recovery_evidence"]["digest"],
            frame_decisions=decisions,
            operation_id=None,
        )

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        rows = (
            (await session.execute(select(RuntimeTask).where(RuntimeTask.id.in_([prior_id, carrier_id]))))
            .scalars()
            .all()
        )
    by_id = {row.id: row for row in rows}
    prior_metadata = dict(by_id[prior_id].metadata_json or {})
    carrier_metadata = dict(by_id[carrier_id].metadata_json or {})
    assert by_id[prior_id].status == "completed"
    assert by_id[carrier_id].status == "completed"
    assert prior_metadata["reconciliation_superseded_by"] == str(carrier_id)
    assert prior_metadata["reconciliation_operation"] == carrier_metadata["reconciliation_operation"]
    assert completed["reconciliation_operation"]["evidence_digest"] == carrier_view["recovery_evidence"]["digest"]
    assert completed["reconciliation_operation"]["frame_decisions"] == decisions
