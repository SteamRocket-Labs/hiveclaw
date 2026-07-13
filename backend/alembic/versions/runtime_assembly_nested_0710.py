"""Collapse RuntimeAssembly metadata mirrors into one nested read model.

Revision ID: runtime_assembly_nested_0710
Revises: approval_ticket_governance_0710
Create Date: 2026-07-10
"""

from __future__ import annotations

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
    """Rewrite only matching rows inside PostgreSQL, never one row per round trip."""

    if json_type not in {"json", "jsonb"}:  # pragma: no cover - migration invariant
        raise ValueError(f"unsupported JSON type: {json_type}")
    bind = op.get_bind()
    params = {
        "assembly_keys": list(_ASSEMBLY_KEYS),
        "schema": _SCHEMA,
        "state_key": _STATE_KEY,
    }
    if promote:
        statement = sa.text(
            f"""
            UPDATE {table} AS target
               SET {column} = CAST(
                    (
                        target.{column}::jsonb - CAST(:assembly_keys AS text[])
                    ) || jsonb_build_object(
                        :state_key,
                        jsonb_build_object('schema', :schema)
                        || COALESCE(
                            (
                                SELECT jsonb_object_agg(entry.key, entry.value)
                                FROM jsonb_each(target.{column}::jsonb) AS entry
                                WHERE entry.key = ANY(CAST(:assembly_keys AS text[]))
                            ),
                            '{{}}'::jsonb
                        )
                        || CASE
                            WHEN jsonb_typeof(target.{column}::jsonb -> :state_key) = 'object'
                            THEN target.{column}::jsonb -> :state_key
                            ELSE '{{}}'::jsonb
                        END
                        || jsonb_build_object('schema', :schema)
                    )
                    AS {json_type}
               )
             WHERE target.{column} IS NOT NULL
               AND (
                    target.{column}::jsonb ?| CAST(:assembly_keys AS text[])
                    OR (
                        jsonb_typeof(target.{column}::jsonb -> :state_key) = 'object'
                        AND target.{column}::jsonb -> :state_key ->> 'schema' IS DISTINCT FROM :schema
                    )
               )
            """
        )
    else:
        statement = sa.text(
            f"""
            UPDATE {table} AS target
               SET {column} = CAST(
                    (target.{column}::jsonb - :state_key)
                    || COALESCE(
                        (
                            SELECT jsonb_object_agg(entry.key, entry.value)
                            FROM jsonb_each(target.{column}::jsonb -> :state_key) AS entry
                            WHERE entry.key = ANY(CAST(:assembly_keys AS text[]))
                        ),
                        '{{}}'::jsonb
                    )
                    AS {json_type}
               )
             WHERE target.{column} IS NOT NULL
               AND jsonb_typeof(target.{column}::jsonb -> :state_key) = 'object'
            """
        )
    bind.execute(statement, params)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        _rewrite_rows("runtime_tasks", "metadata_json", promote=True, json_type="json")
        _rewrite_rows("chat_sessions", "transcript_metadata_json", promote=True, json_type="jsonb")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        _rewrite_rows("chat_sessions", "transcript_metadata_json", promote=False, json_type="jsonb")
        _rewrite_rows("runtime_tasks", "metadata_json", promote=False, json_type="json")
