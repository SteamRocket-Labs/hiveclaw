"""Collapse RuntimeAssembly metadata mirrors into one nested read model.

Revision ID: runtime_assembly_nested_0710
Revises: approval_ticket_governance_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "runtime_assembly_nested_0710"
down_revision = "approval_ticket_governance_0710"
branch_labels = None
depends_on = None


_STATE_KEY = "runtime_assembly_state"
_SCHEMA = "hive.ccplus.runtime_assembly_state.v1"
_ASSEMBLY_KEYS = (
    "prompt_assembly_manifest",
    "context_usage_ledger",
    "dynamic_context_section_ledger",
    "tool_result_ledger",
    "cache_decision_ledger",
    "runtime_decision_ledger",
    "agent_cycle_decision_ledger",
    "runtime_reminder_candidates",
    "activation_candidates",
    "activation_events",
    "available_deferred_tool_candidates",
    "available_deferred_tools",
    "skill_catalog_ranking",
    "skill_catalog_ranking_inputs",
)


def _promote_runtime_assembly(value: Any) -> tuple[dict[str, Any], bool]:
    """Promote old mirrors without overwriting already-canonical fields."""
    metadata = dict(value) if isinstance(value, dict) else {}
    nested_value = metadata.get(_STATE_KEY)
    has_nested = isinstance(nested_value, dict)
    legacy = {key: metadata[key] for key in _ASSEMBLY_KEYS if key in metadata}
    if not has_nested and not legacy:
        return metadata, False

    nested = dict(nested_value) if has_nested else {}
    assembly = {"schema": _SCHEMA, **legacy, **nested}
    assembly["schema"] = _SCHEMA
    promoted = {key: item for key, item in metadata.items() if key not in _ASSEMBLY_KEYS}
    promoted[_STATE_KEY] = assembly
    return promoted, promoted != metadata


def _restore_legacy_mirrors(value: Any) -> tuple[dict[str, Any], bool]:
    metadata = dict(value) if isinstance(value, dict) else {}
    nested = metadata.get(_STATE_KEY)
    if not isinstance(nested, dict):
        return metadata, False
    restored = {key: item for key, item in metadata.items() if key != _STATE_KEY}
    restored.update({key: nested[key] for key in _ASSEMBLY_KEYS if key in nested})
    return restored, restored != metadata


def _rewrite_rows(table: str, column: str, *, promote: bool, json_type: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT id, {column} AS metadata FROM {table} WHERE {column} IS NOT NULL"))
    transform = _promote_runtime_assembly if promote else _restore_legacy_mirrors
    for row in rows.mappings():
        metadata, changed = transform(row["metadata"])
        if not changed:
            continue
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = CAST(:metadata AS {json_type}) WHERE id = :row_id"),
            {"row_id": row["id"], "metadata": json.dumps(metadata, ensure_ascii=False, default=str)},
        )


def upgrade() -> None:
    _rewrite_rows("runtime_tasks", "metadata_json", promote=True, json_type="json")
    _rewrite_rows("chat_sessions", "transcript_metadata_json", promote=True, json_type="jsonb")


def downgrade() -> None:
    _rewrite_rows("chat_sessions", "transcript_metadata_json", promote=False, json_type="jsonb")
    _rewrite_rows("runtime_tasks", "metadata_json", promote=False, json_type="json")
