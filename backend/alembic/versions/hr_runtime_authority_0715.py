"""Bind HR RuntimeTasks to authenticated immutable blueprint authority.

Revision ID: hr_runtime_authority_0715
Revises: storage_blob_lifecycle_0715
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "hr_runtime_authority_0715"
down_revision = "storage_blob_lifecycle_0715"
branch_labels = None
depends_on = None


_TRIGGER = "trg_hr_creation_blueprint_immutable"
_TRIGGER_FUNCTION = "enforce_hr_creation_blueprint_immutable"
_AUTHORIZED_DRAFT_STATUSES = {"confirmed", "creating", "provisioning", "failed"}
_PRESERVED_TERMINAL_TASK_STATUSES = {"completed", "killed", "skipped", "needs_reconciliation"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _snapshot_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _payload_hash(blueprint: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(blueprint).encode("utf-8")).hexdigest()


def _blueprint_hash(blueprint: dict[str, Any]) -> str:
    return f"bp_{_payload_hash(blueprint)[:24]}"


def _authority_issues(row: Any) -> tuple[list[str], str | None, dict[str, Any] | None, dict[str, Any] | None]:
    if row.draft_id is None:
        return ["draft_link_missing"], None, None, None

    issues: list[str] = []
    if row.task_status == "running" or row.claimed_by is not None:
        issues.append("active_legacy_worker_fenced")
    if (
        row.draft_status not in _AUTHORIZED_DRAFT_STATUSES
        or row.confirmed_by_user_id is None
        or row.confirmed_at is None
        or row.confirmed_by_user_id != row.requested_by_user_id
    ):
        issues.append("missing_confirmation_evidence")
    if row.task_type != "hr_provisioning":
        issues.append("task_type_mismatch")
    if row.task_tenant_id != row.draft_tenant_id:
        issues.append("tenant_mismatch")
    if row.parent_agent_id != row.hr_agent_id:
        issues.append("hr_agent_mismatch")
    if row.root_user_id != row.requested_by_user_id:
        issues.append("requester_mismatch")
    if str(row.parent_session_id or "") != str(row.session_id):
        issues.append("session_mismatch")
    if str(row.root_session_id or "") != str(row.session_id):
        issues.append("root_session_mismatch")
    if list(row.delegation_chain_json or []) != [f"agent:{row.hr_agent_id}"]:
        issues.append("delegation_chain_mismatch")
    expected_idempotency_key = f"hr-provisioning:{row.draft_id}-v{row.blueprint_version}"
    if row.root_idempotency_key != expected_idempotency_key:
        issues.append("idempotency_key_mismatch")

    blueprint = dict(row.blueprint_json or {})
    payload_hash = _payload_hash(blueprint)
    if row.blueprint_hash != _blueprint_hash(blueprint):
        issues.append("blueprint_payload_integrity_mismatch")
    metadata = dict(row.metadata_json or {})
    expected_metadata = {
        "schema": "hr_provisioning_job.v1",
        "draft_id": str(row.draft_id),
        "blueprint_version": int(row.blueprint_version),
        "blueprint_hash": str(row.blueprint_hash),
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        issues.append("immutable_blueprint_mismatch")

    config = {
        "task_type": "hr_provisioning",
        "draft_id": str(row.draft_id),
        "blueprint_version": int(row.blueprint_version),
        "blueprint_hash": str(row.blueprint_hash),
        "blueprint_payload_hash": payload_hash,
        "hr_agent_id": str(row.hr_agent_id),
        "session_id": str(row.session_id),
    }
    policy = {
        "tenant_id": str(row.draft_tenant_id),
        "requested_by_user_id": str(row.requested_by_user_id),
        "confirmed_by_user_id": str(row.confirmed_by_user_id) if row.confirmed_by_user_id else None,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
    }
    return issues, payload_hash, config, policy


def _backfill_or_quarantine_runtime_tasks() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT
                task.id AS task_id,
                task.task_type,
                task.status AS task_status,
                task.claimed_by,
                task.tenant_id AS task_tenant_id,
                task.parent_agent_id,
                task.parent_session_id,
                task.root_user_id,
                task.root_session_id,
                task.delegation_chain_json,
                task.root_idempotency_key,
                task.metadata_json,
                draft.id AS draft_id,
                draft.tenant_id AS draft_tenant_id,
                draft.hr_agent_id,
                draft.session_id,
                draft.requested_by_user_id,
                draft.status AS draft_status,
                draft.blueprint_version,
                draft.blueprint_hash,
                draft.blueprint_json,
                draft.confirmed_by_user_id,
                draft.confirmed_at
            FROM runtime_tasks AS task
            LEFT JOIN hr_creation_drafts AS draft
              ON draft.provisioning_task_id = task.id
             AND draft.tenant_id = task.tenant_id
            WHERE task.task_type = 'hr_provisioning'
            ORDER BY task.id
            FOR UPDATE OF task
            """
        )
    ).all()

    for row in rows:
        if row.task_status in _PRESERVED_TERMINAL_TASK_STATUSES:
            metadata = {
                **dict(row.metadata_json or {}),
                "authority_terminal_preserved_by": revision,
            }
            connection.execute(
                sa.text(
                    """
                    UPDATE runtime_tasks
                    SET metadata_json = CAST(:metadata_json AS json)
                    WHERE id = :task_id
                    """
                ),
                {"task_id": row.task_id, "metadata_json": _canonical_json(metadata)},
            )
            continue

        issues, payload_hash, config, policy = _authority_issues(row)
        if not issues and payload_hash is not None and config is not None and policy is not None:
            metadata = {
                **dict(row.metadata_json or {}),
                "blueprint_payload_hash": payload_hash,
                "authority_backfilled_by": revision,
            }
            connection.execute(
                sa.text(
                    """
                    UPDATE runtime_tasks
                    SET config_snapshot_hash = :config_hash,
                        policy_snapshot_hash = :policy_hash,
                        metadata_json = CAST(:metadata_json AS json)
                    WHERE id = :task_id
                    """
                ),
                {
                    "task_id": row.task_id,
                    "config_hash": _snapshot_hash(config),
                    "policy_hash": _snapshot_hash(policy),
                    "metadata_json": _canonical_json(metadata),
                },
            )
            continue

        existing_audit = connection.execute(
            sa.text(
                """
                SELECT count(*)
                FROM audit_logs
                WHERE action = 'migration.hr_runtime_authority_quarantined'
                  AND details ->> 'runtime_task_id' = :task_id
                """
            ),
            {"task_id": str(row.task_id)},
        ).scalar_one()
        if int(existing_audit or 0) == 0:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO audit_logs (id, tenant_id, agent_id, action, details)
                    VALUES (
                        gen_random_uuid(),
                        :tenant_id,
                        :agent_id,
                        'migration.hr_runtime_authority_quarantined',
                        CAST(:details AS json)
                    )
                    """
                ),
                {
                    "tenant_id": row.task_tenant_id,
                    "agent_id": row.parent_agent_id,
                    "details": _canonical_json(
                        {
                            "schema": "hive.audit.hr_runtime_authority_quarantine.v1",
                            "runtime_task_id": str(row.task_id),
                            "draft_id": str(row.draft_id) if row.draft_id else None,
                            "authority_issues": issues,
                            "recovery": "review the confirmed HR draft and create or retry one governed provisioning job",
                        }
                    ),
                },
            )
        metadata = {
            **dict(row.metadata_json or {}),
            "phase": "terminal",
            "automatic_retry_allowed": False,
            "needs_reconciliation": True,
            "reconciliation_reason": "hr_runtime_authority_mismatch",
            "authority_issues": issues,
            "authority_quarantined_by": revision,
        }
        connection.execute(
            sa.text(
                """
                UPDATE runtime_tasks
                SET status = 'needs_reconciliation',
                    completed_at = COALESCE(completed_at, now()),
                    claim_version = claim_version + 1,
                    claimed_by = NULL,
                    claim_expires_at = NULL,
                    result_summary = 'HR provisioning runtime authority requires reconciliation.',
                    metadata_json = CAST(:metadata_json AS json)
                WHERE id = :task_id
                """
            ),
            {"task_id": row.task_id, "metadata_json": _canonical_json(metadata)},
        )
        if row.draft_id is not None:
            connection.execute(
                sa.text(
                    """
                    UPDATE hr_creation_drafts
                    SET claim_token = NULL,
                        claim_version = claim_version + 1,
                        claim_heartbeat_at = NULL,
                        claim_expires_at = NULL
                    WHERE id = :draft_id
                    """
                ),
                {"draft_id": row.draft_id},
            )

    unprocessed = connection.execute(
        sa.text(
            """
            SELECT count(*)
            FROM runtime_tasks
            WHERE task_type = 'hr_provisioning'
              AND COALESCE(metadata_json ->> 'authority_backfilled_by', '') = ''
              AND COALESCE(metadata_json ->> 'authority_quarantined_by', '') = ''
              AND COALESCE(metadata_json ->> 'authority_terminal_preserved_by', '') = ''
            """
        )
    ).scalar_one()
    if int(unprocessed or 0) != 0:
        raise RuntimeError(f"hr_runtime_authority_0715 left {int(unprocessed)} HR RuntimeTasks unprocessed")


def _install_immutability_guard() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (OLD.status = 'superseded' AND OLD.status IS DISTINCT FROM NEW.status)
               OR (
                    OLD.status IN ('confirmed', 'creating', 'provisioning', 'failed', 'completed', 'superseded')
                    AND (
                    OLD.blueprint_version IS DISTINCT FROM NEW.blueprint_version
                    OR OLD.blueprint_hash IS DISTINCT FROM NEW.blueprint_hash
                    OR OLD.blueprint_json IS DISTINCT FROM NEW.blueprint_json
                    )
               )
            THEN
                RAISE EXCEPTION 'confirmed HR blueprint is immutable'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON hr_creation_drafts")
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE UPDATE OF blueprint_version, blueprint_hash, blueprint_json, status
        ON hr_creation_drafts
        FOR EACH ROW
        EXECUTE FUNCTION {_TRIGGER_FUNCTION}()
        """
    )


def upgrade() -> None:
    # The migration is the schema-owner fleet boundary. FORCE RLS would
    # otherwise make a tenant-less backfill silently see zero rows.
    op.execute("SET LOCAL row_security = off")
    for table in ("runtime_tasks", "hr_creation_drafts", "audit_logs"):
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    _backfill_or_quarantine_runtime_tasks()
    _install_immutability_guard()
    for table in ("runtime_tasks", "hr_creation_drafts", "audit_logs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Secure downgrade: old application versions already treat confirmed rows as
    # immutable. Preserve the compatible trigger, canonical snapshot hashes, and
    # append-only quarantine evidence so rollback cannot reopen B-01.
    pass
