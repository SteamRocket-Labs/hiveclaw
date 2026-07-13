"""Normalize legacy break-glass sessions before restoring CC-style Full access.

Revision ID: session_permission_semantics_0713
Revises: agent_session_permission_default_0713
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op


revision = "session_permission_semantics_0713"
down_revision = "agent_session_permission_default_0713"
branch_labels = None
depends_on = None

_BACKUP_KEY = "_session_permission_semantics_0713_backup"
_TABLES = (
    ("chat_sessions", "transcript_metadata_json", "jsonb"),
    ("runtime_tasks", "metadata_json", "json"),
)


def _base(column: str) -> str:
    return f"COALESCE({column}::jsonb, '{{}}'::jsonb)"


def _upgrade_sql(table: str, column: str, column_type: str) -> str:
    base = _base(column)
    legacy_bypass = f"""COALESCE(
        {base} ->> 'permission_mode',
        {base} #>> '{{permission_profile,mode}}'
    ) = 'bypassPermissions'"""
    backup = f"""
        jsonb_build_object(
            'had_permission_mode', {base} ? 'permission_mode',
            'permission_mode', {base} -> 'permission_mode',
            'had_permission_profile', {base} ? 'permission_profile',
            'permission_profile', {base} -> 'permission_profile',
            'had_break_glass', {base} ? 'break_glass',
            'break_glass', {base} -> 'break_glass'
        )
    """
    normalized_profile = f"""
        CASE
            WHEN jsonb_typeof({base} -> 'permission_profile') = 'object'
            THEN jsonb_set({base} -> 'permission_profile', '{{mode}}', '"default"'::jsonb, true)
            ELSE '{{"mode":"default"}}'::jsonb
        END
    """
    normalized = f"""
        ({base} - 'break_glass')
        || jsonb_build_object('{_BACKUP_KEY}', {backup})
        || jsonb_build_object('permission_mode', 'default')
        || jsonb_build_object('permission_profile', {normalized_profile})
    """
    cleanup_only = f"""
        ({base} - 'break_glass')
        || jsonb_build_object('{_BACKUP_KEY}', {backup})
    """
    transformed = f"CASE WHEN {legacy_bypass} THEN ({normalized}) ELSE ({cleanup_only}) END"
    cast = "::json" if column_type == "json" else ""
    return f"""
        UPDATE {table}
        SET {column} = ({transformed}){cast}
        WHERE ({legacy_bypass} OR {base} ? 'break_glass')
          AND NOT ({base} ? '{_BACKUP_KEY}')
    """


def _downgrade_sql(table: str, column: str, column_type: str) -> str:
    base = _base(column)
    backup = f"{base} -> '{_BACKUP_KEY}'"
    restored = f"""
        ({base} - '{_BACKUP_KEY}' - 'permission_mode' - 'permission_profile' - 'break_glass')
        || CASE WHEN COALESCE(({backup} ->> 'had_permission_mode')::boolean, false)
            THEN jsonb_build_object('permission_mode', {backup} -> 'permission_mode') ELSE '{{}}'::jsonb END
        || CASE WHEN COALESCE(({backup} ->> 'had_permission_profile')::boolean, false)
            THEN jsonb_build_object('permission_profile', {backup} -> 'permission_profile') ELSE '{{}}'::jsonb END
        || CASE WHEN COALESCE(({backup} ->> 'had_break_glass')::boolean, false)
            THEN jsonb_build_object('break_glass', {backup} -> 'break_glass') ELSE '{{}}'::jsonb END
    """
    cast = "::json" if column_type == "json" else ""
    return f"""
        UPDATE {table}
        SET {column} = ({restored}){cast}
        WHERE {base} ? '{_BACKUP_KEY}'
    """


def _set_rls(enabled: bool) -> None:
    action = "ENABLE" if enabled else "DISABLE"
    for table, _column, _column_type in _TABLES:
        op.execute(f"ALTER TABLE {table} {action} ROW LEVEL SECURITY")
        if enabled:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _set_rls(False)
    for table, column, column_type in _TABLES:
        op.execute(_upgrade_sql(table, column, column_type))
    _set_rls(True)


def downgrade() -> None:
    _set_rls(False)
    for table, column, column_type in _TABLES:
        op.execute(_downgrade_sql(table, column, column_type))
    _set_rls(True)
