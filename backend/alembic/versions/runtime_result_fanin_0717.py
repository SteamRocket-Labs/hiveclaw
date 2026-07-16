"""Add immutable runtime results and ref-only fan-in pages.

Revision ID: runtime_result_fanin_0717
Revises: runtime_root_ledger_0716
Create Date: 2026-07-17
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "runtime_result_fanin_0717"
down_revision = "runtime_root_ledger_0716"
branch_labels = None
depends_on = None


RUNTIME_RESULT_TABLES: tuple[str, ...] = (
    "runtime_result_objects",
    "runtime_result_mailbox_cursors",
    "runtime_result_integration_pages",
)

_RESULT_NAMESPACE = uuid.UUID("6e24bb74-d601-5a6e-b7db-010f5565e28c")
_ROUTING_METADATA_KEYS = frozenset(
    {
        "approval_id",
        "approval_status",
        "tool_name",
        "origin_session_id",
        "budget_run_id",
        "agent_team_close_id",
        "team_id",
        "team_name",
        "member_id",
        "member_name",
        "workflow_run_id",
        "parent_agent_id",
        "parent_session_id",
        "runtime_task_id",
        "runtime_task_type",
        "signal_id",
        "trace_id",
        "thread_id",
        "from_agent_id",
        "from_agent",
        "to_agent",
        "to_agent_name",
        "interaction_type",
        "depth",
        "root_runtime_task_id",
        "reconciled_from_terminal_runtime_task",
    }
)


def _canonical_result_bytes(summary: str, artifacts: list[Any], metadata: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "schema": "hive.runtime_result.v1",
            "summary": str(summary),
            "artifacts": list(artifacts),
            "metadata": dict(metadata),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _runtime_result_id(
    *,
    tenant_id: uuid.UUID,
    source_kind: str,
    source_run_id: str,
    sha256: str,
) -> uuid.UUID:
    return uuid.uuid5(
        _RESULT_NAMESPACE,
        f"{tenant_id}:{str(source_kind).strip().lower()}:{str(source_run_id).strip()}:{sha256}",
    )


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _constraints(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    names = {
        str(item.get("name"))
        for item in (
            *inspector.get_unique_constraints(table_name),
            *inspector.get_foreign_keys(table_name),
            *inspector.get_check_constraints(table_name),
        )
        if item.get("name")
    }
    return names


def _create_result_tables() -> None:
    op.create_table(
        "runtime_result_objects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_run_id", sa.String(length=200), nullable=False),
        sa.Column("payload_schema", sa.String(length=80), server_default="hive.runtime_result.v1", nullable=False),
        sa.Column("payload_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=120), server_default="application/json", nullable=False),
        sa.Column("encoding", sa.String(length=32), server_default="utf-8", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("size_bytes >= 0", name="ck_runtime_result_objects_size_nonnegative"),
        sa.UniqueConstraint(
            "tenant_id", "source_kind", "source_run_id", "sha256", name="uq_runtime_result_objects_source_hash"
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_runtime_result_objects_source",
        "runtime_result_objects",
        ["tenant_id", "source_kind", "source_run_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_runtime_result_objects_tenant_id"), "runtime_result_objects", ["tenant_id"], if_not_exists=True
    )
    op.create_index(op.f("ix_runtime_result_objects_sha256"), "runtime_result_objects", ["sha256"], if_not_exists=True)

    op.create_table(
        "runtime_result_mailbox_cursors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "parent_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("next_mailbox_sequence", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("next_integration_epoch", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_prepared_sequence", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_delivered_sequence", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("next_mailbox_sequence >= 1", name="ck_runtime_result_mailbox_next_sequence"),
        sa.CheckConstraint("next_integration_epoch >= 1", name="ck_runtime_result_mailbox_next_epoch"),
        sa.UniqueConstraint("tenant_id", "parent_session_id", name="uq_runtime_result_mailbox_cursor_parent"),
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_runtime_result_mailbox_cursors_tenant_id"),
        "runtime_result_mailbox_cursors",
        ["tenant_id"],
        if_not_exists=True,
    )
    op.create_index(
        op.f("ix_runtime_result_mailbox_cursors_parent_session_id"),
        "runtime_result_mailbox_cursors",
        ["parent_session_id"],
        if_not_exists=True,
    )

    op.create_table(
        "runtime_result_integration_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "parent_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("root_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_scope_key", sa.String(length=260), nullable=False),
        sa.Column("integration_epoch", sa.BigInteger(), nullable=False),
        sa.Column("delivery_mode", sa.String(length=32), nullable=False),
        sa.Column("mailbox_sequence_start", sa.BigInteger(), nullable=False),
        sa.Column("mailbox_sequence_end", sa.BigInteger(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("manifest_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "coverage_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), server_default="prepared", nullable=False),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=200), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivery_receipt_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "delivery_mode IN ('parent_continuation','session_projection')",
            name="ck_runtime_result_integration_pages_delivery_mode",
        ),
        sa.CheckConstraint(
            "status IN ('prepared','processing','delivered','dead_letter')",
            name="ck_runtime_result_integration_pages_status",
        ),
        sa.CheckConstraint("item_count >= 1", name="ck_runtime_result_integration_pages_item_count"),
        sa.CheckConstraint(
            "mailbox_sequence_end >= mailbox_sequence_start",
            name="ck_runtime_result_integration_pages_sequence_range",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "parent_session_id",
            "integration_epoch",
            name="uq_runtime_result_integration_pages_parent_epoch",
        ),
        if_not_exists=True,
    )
    for column in (
        "tenant_id",
        "parent_session_id",
        "parent_agent_id",
        "parent_user_id",
        "root_runtime_task_id",
        "status",
        "claim_token",
    ):
        op.create_index(
            op.f(f"ix_runtime_result_integration_pages_{column}"),
            "runtime_result_integration_pages",
            [column],
            if_not_exists=True,
        )
    op.create_index(
        "ix_runtime_result_integration_pages_claim",
        "runtime_result_integration_pages",
        ["status", "lease_expires_at", "created_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_runtime_result_integration_pages_root",
        "runtime_result_integration_pages",
        ["tenant_id", "root_scope_key", "integration_epoch"],
        if_not_exists=True,
    )


def _add_outbox_columns() -> None:
    columns = _columns("runtime_notification_outbox")
    additions = (
        ("root_runtime_task_id", sa.Column("root_runtime_task_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("result_object_id", sa.Column("result_object_id", postgresql.UUID(as_uuid=True), nullable=True)),
        ("result_ref", sa.Column("result_ref", sa.String(length=180), nullable=True)),
        ("result_sha256", sa.Column("result_sha256", sa.String(length=64), nullable=True)),
        ("result_size_bytes", sa.Column("result_size_bytes", sa.BigInteger(), nullable=True)),
        ("artifact_count", sa.Column("artifact_count", sa.Integer(), server_default=sa.text("0"), nullable=False)),
        ("mailbox_sequence", sa.Column("mailbox_sequence", sa.BigInteger(), nullable=True)),
        ("claim_token", sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True)),
        ("lease_expires_at", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)),
        ("integration_page_id", sa.Column("integration_page_id", postgresql.UUID(as_uuid=True), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("runtime_notification_outbox", column)


def _backfill_outbox() -> None:
    bind = op.get_bind()
    columns = _columns("runtime_notification_outbox")
    has_legacy_payload = "summary" in columns and "artifacts_json" in columns
    if not has_legacy_payload:
        count = bind.execute(sa.text("SELECT count(*) FROM runtime_notification_outbox")).scalar_one()
        if count:
            raise RuntimeError("runtime notification rows exist without a legacy payload or result reference")
        return

    rows = (
        bind.execute(
            sa.text(
                "SELECT id, tenant_id, source_kind, source_run_id, parent_session_id, "
                "summary, artifacts_json, metadata_json "
                "FROM runtime_notification_outbox "
                "ORDER BY tenant_id, parent_session_id, created_at, id"
            )
        )
        .mappings()
        .all()
    )
    next_sequence: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    for row in rows:
        metadata = dict(row["metadata_json"] or {})
        artifacts = list(row["artifacts_json"] or [])
        payload_bytes = _canonical_result_bytes(str(row["summary"] or ""), artifacts, metadata)
        digest = hashlib.sha256(payload_bytes).hexdigest()
        result_id = _runtime_result_id(
            tenant_id=row["tenant_id"],
            source_kind=row["source_kind"],
            source_run_id=row["source_run_id"],
            sha256=digest,
        )
        result_ref = f"runtime-result://{result_id}/{digest}"
        bind.execute(
            sa.text(
                "INSERT INTO runtime_result_objects "
                "(id, tenant_id, source_kind, source_run_id, payload_schema, payload_bytes, sha256, size_bytes, media_type, encoding) "
                "VALUES (:id, :tenant_id, :source_kind, :source_run_id, 'hive.runtime_result.v1', :payload_bytes, :sha256, :size_bytes, 'application/json', 'utf-8') "
                "ON CONFLICT (tenant_id, source_kind, source_run_id, sha256) DO NOTHING"
            ),
            {
                "id": result_id,
                "tenant_id": row["tenant_id"],
                "source_kind": row["source_kind"],
                "source_run_id": row["source_run_id"],
                "payload_bytes": payload_bytes,
                "sha256": digest,
                "size_bytes": len(payload_bytes),
            },
        )
        key = (row["tenant_id"], row["parent_session_id"])
        sequence = next_sequence.get(key, 1)
        next_sequence[key] = sequence + 1
        root_runtime_task_id = None
        raw_root = metadata.get("root_runtime_task_id")
        try:
            root_runtime_task_id = uuid.UUID(str(raw_root)) if raw_root else None
        except (TypeError, ValueError):
            root_runtime_task_id = None
        if root_runtime_task_id is None:
            try:
                source_task_id = uuid.UUID(str(row["source_run_id"]))
            except (TypeError, ValueError):
                source_task_id = None
            if source_task_id is not None:
                root_runtime_task_id = bind.execute(
                    sa.text(
                        "SELECT COALESCE(root_runtime_task_id, id) FROM runtime_tasks "
                        "WHERE id=:id AND tenant_id=:tenant_id"
                    ),
                    {"id": source_task_id, "tenant_id": row["tenant_id"]},
                ).scalar_one_or_none()
        routing_metadata = {key: value for key, value in metadata.items() if key in _ROUTING_METADATA_KEYS}
        bind.execute(
            sa.text(
                "UPDATE runtime_notification_outbox SET "
                "root_runtime_task_id=:root_runtime_task_id, result_object_id=:result_object_id, "
                "result_ref=:result_ref, result_sha256=:result_sha256, result_size_bytes=:result_size_bytes, "
                "artifact_count=:artifact_count, mailbox_sequence=:mailbox_sequence, metadata_json=CAST(:metadata_json AS jsonb) "
                "WHERE id=:id"
            ),
            {
                "id": row["id"],
                "root_runtime_task_id": root_runtime_task_id,
                "result_object_id": result_id,
                "result_ref": result_ref,
                "result_sha256": digest,
                "result_size_bytes": len(payload_bytes),
                "artifact_count": len(artifacts),
                "mailbox_sequence": sequence,
                "metadata_json": json.dumps(routing_metadata, ensure_ascii=False, default=str),
            },
        )

    for (tenant_id, parent_session_id), sequence in next_sequence.items():
        cursor_id = uuid.uuid5(_RESULT_NAMESPACE, f"cursor:{tenant_id}:{parent_session_id}")
        bind.execute(
            sa.text(
                "INSERT INTO runtime_result_mailbox_cursors "
                "(id, tenant_id, parent_session_id, next_mailbox_sequence, next_integration_epoch, last_prepared_sequence, last_delivered_sequence, version) "
                "VALUES (:id, :tenant_id, :parent_session_id, :next_sequence, 1, 0, 0, 1) "
                "ON CONFLICT (tenant_id, parent_session_id) DO UPDATE SET "
                "next_mailbox_sequence=GREATEST(runtime_result_mailbox_cursors.next_mailbox_sequence, EXCLUDED.next_mailbox_sequence)"
            ),
            {
                "id": cursor_id,
                "tenant_id": tenant_id,
                "parent_session_id": parent_session_id,
                "next_sequence": sequence,
            },
        )


def _finalize_outbox_contract() -> None:
    op.alter_column(
        "runtime_notification_outbox", "result_object_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False
    )
    op.alter_column("runtime_notification_outbox", "result_ref", existing_type=sa.String(length=180), nullable=False)
    op.alter_column("runtime_notification_outbox", "result_sha256", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("runtime_notification_outbox", "result_size_bytes", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column("runtime_notification_outbox", "mailbox_sequence", existing_type=sa.BigInteger(), nullable=False)

    constraints = _constraints("runtime_notification_outbox")
    if "fk_runtime_notification_outbox_result_object" not in constraints:
        op.create_foreign_key(
            "fk_runtime_notification_outbox_result_object",
            "runtime_notification_outbox",
            "runtime_result_objects",
            ["result_object_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if "fk_runtime_notification_outbox_integration_page" not in constraints:
        op.create_foreign_key(
            "fk_runtime_notification_outbox_integration_page",
            "runtime_notification_outbox",
            "runtime_result_integration_pages",
            ["integration_page_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "uq_runtime_notification_outbox_mailbox_sequence" not in constraints:
        op.create_unique_constraint(
            "uq_runtime_notification_outbox_mailbox_sequence",
            "runtime_notification_outbox",
            ["tenant_id", "parent_session_id", "mailbox_sequence"],
        )
    for column in (
        "root_runtime_task_id",
        "result_object_id",
        "claim_token",
    ):
        op.create_index(
            op.f(f"ix_runtime_notification_outbox_{column}"),
            "runtime_notification_outbox",
            [column],
            if_not_exists=True,
        )
    op.create_index(
        "ix_runtime_notification_outbox_page",
        "runtime_notification_outbox",
        ["integration_page_id", "mailbox_sequence"],
        if_not_exists=True,
    )

    columns = _columns("runtime_notification_outbox")
    if "summary" in columns:
        op.drop_column("runtime_notification_outbox", "summary")
    if "artifacts_json" in columns:
        op.drop_column("runtime_notification_outbox", "artifacts_json")


def _install_rls() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name in RUNTIME_RESULT_TABLES:
        predicate = (
            "current_setting('app.current_tenant_id', true) = 'BYPASS' "
            f"OR {table_name}.tenant_id::text = current_setting('app.current_tenant_id', true)"
        )
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_isolation ON {table_name}")
        op.execute(
            f"CREATE POLICY {table_name}_tenant_isolation ON {table_name} USING ({predicate}) WITH CHECK ({predicate})"
        )


def upgrade() -> None:
    _create_result_tables()
    _add_outbox_columns()
    _backfill_outbox()
    _finalize_outbox_contract()
    _install_rls()


def downgrade() -> None:
    columns = _columns("runtime_notification_outbox")
    if "summary" not in columns:
        op.add_column("runtime_notification_outbox", sa.Column("summary", sa.Text(), nullable=True))
    if "artifacts_json" not in columns:
        op.add_column(
            "runtime_notification_outbox",
            sa.Column(
                "artifacts_json",
                postgresql.JSONB(astext_type=sa.Text()),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
        )
    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                "SELECT outbox.id, result.payload_bytes FROM runtime_notification_outbox outbox "
                "JOIN runtime_result_objects result ON result.id=outbox.result_object_id"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        payload = json.loads(bytes(row["payload_bytes"]).decode("utf-8"))
        bind.execute(
            sa.text(
                "UPDATE runtime_notification_outbox SET summary=:summary, "
                "artifacts_json=CAST(:artifacts AS jsonb), metadata_json=CAST(:metadata AS jsonb) "
                "WHERE id=:id"
            ),
            {
                "id": row["id"],
                "summary": str(payload.get("summary") or ""),
                "artifacts": json.dumps(list(payload.get("artifacts") or []), ensure_ascii=False, default=str),
                "metadata": json.dumps(dict(payload.get("metadata") or {}), ensure_ascii=False, default=str),
            },
        )
    op.alter_column("runtime_notification_outbox", "summary", existing_type=sa.Text(), nullable=False)

    for constraint in (
        "uq_runtime_notification_outbox_mailbox_sequence",
        "fk_runtime_notification_outbox_integration_page",
        "fk_runtime_notification_outbox_result_object",
    ):
        constraints = _constraints("runtime_notification_outbox")
        if constraint in constraints:
            op.drop_constraint(
                constraint,
                "runtime_notification_outbox",
                type_=("unique" if constraint.startswith("uq_") else "foreignkey"),
            )
    for column in (
        "integration_page_id",
        "lease_expires_at",
        "claim_token",
        "mailbox_sequence",
        "artifact_count",
        "result_size_bytes",
        "result_sha256",
        "result_ref",
        "result_object_id",
        "root_runtime_task_id",
    ):
        if column in _columns("runtime_notification_outbox"):
            op.drop_column("runtime_notification_outbox", column)
    for table_name in reversed(RUNTIME_RESULT_TABLES):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"DROP POLICY IF EXISTS {table_name}_tenant_isolation ON {table_name}")
        op.drop_table(table_name, if_exists=True)
