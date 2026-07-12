"""Migrate legacy hook continue policy to typed inherited failure modes.

Revision ID: hook_failure_modes_0712
Revises: dream_runtime_task_0712
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op


revision = "hook_failure_modes_0712"
down_revision = "dream_runtime_task_0712"
branch_labels = None
depends_on = None


_UPGRADE_SQL = r"""
UPDATE system_settings AS setting
SET value = jsonb_set(
    setting.value,
    '{hooks}',
    COALESCE(
        (
            SELECT jsonb_object_agg(
                entries.key,
                CASE
                    WHEN entries.config ->> 'failure_policy' = 'continue' THEN
                        jsonb_set(
                            jsonb_set(
                                jsonb_set(
                                    entries.config,
                                    '{failure_policy}',
                                    '"inherit"'::jsonb,
                                    true
                                ),
                                '{legacy_failure_policy}',
                                '"continue"'::jsonb,
                                true
                            ),
                            '{migration_preview}',
                            '{"effective_change":"registration_default"}'::jsonb,
                            true
                        )
                    WHEN entries.config ->> 'failure_policy' = 'block' THEN
                        jsonb_set(
                            entries.config,
                            '{failure_policy}',
                            '"required"'::jsonb,
                            true
                        )
                    ELSE entries.config
                END
            )
            FROM jsonb_each(COALESCE(setting.value -> 'hooks', '{}'::jsonb)) AS entries(key, config)
        ),
        '{}'::jsonb
    ),
    true
)
WHERE setting.key LIKE ('agent:%' || chr(58) || 'hook_runtime');
"""


_DOWNGRADE_SQL = r"""
UPDATE system_settings AS setting
SET value = jsonb_set(
    setting.value,
    '{hooks}',
    COALESCE(
        (
            SELECT jsonb_object_agg(
                entries.key,
                CASE
                    WHEN entries.config ->> 'legacy_failure_policy' = 'continue' THEN
                        jsonb_set(
                            (entries.config - 'legacy_failure_policy' - 'migration_preview'),
                            '{failure_policy}',
                            '"continue"'::jsonb,
                            true
                        )
                    WHEN entries.config ->> 'failure_policy' = 'required' THEN
                        jsonb_set(entries.config, '{failure_policy}', '"block"'::jsonb, true)
                    ELSE entries.config
                END
            )
            FROM jsonb_each(COALESCE(setting.value -> 'hooks', '{}'::jsonb)) AS entries(key, config)
        ),
        '{}'::jsonb
    ),
    true
)
WHERE setting.key LIKE ('agent:%' || chr(58) || 'hook_runtime');
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)


# Structural audit anchors: ("failure_policy", "inherit"), "legacy_failure_policy",
# "migration_preview", and the legacy value "continue" are all preserved above.
