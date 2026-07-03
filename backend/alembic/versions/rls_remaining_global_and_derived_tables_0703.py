"""Close remaining RLS gaps on global and parent-derived tables.

Revision ID: rls_remaining_global_and_derived_tables_0703
Revises: rls_post_0615_gap_closure_0703
Create Date: 2026-07-03
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "rls_remaining_global_and_derived_tables_0703"
down_revision = "rls_post_0615_gap_closure_0703"
branch_labels = None
depends_on = None


_SECRET_SYSTEM_SETTING_KEYS: tuple[str, ...] = (
    "jina_api_key",
    "tavily_api_key",
    "exa_api_key",
    "firecrawl_api_key",
)

_ALL_TABLES: tuple[str, ...] = (
    "tenants",
    "notifications",
    "refresh_tokens",
    "plaza_comments",
    "plaza_likes",
    "skill_files",
    "external_identities",
    "participants",
    "feature_flags",
    "system_settings",
    "identities",
)


def _existing_tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _bypass_predicate() -> str:
    return "current_setting('app.current_tenant_id', true) = 'BYPASS'"


def _tenant_catalog_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR tenants.id::text = current_setting('app.current_tenant_id', true)
    """


def _user_owned_predicate(table: str) -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM users
            WHERE users.id = {table}.user_id
              AND (
                  users.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR users.tenant_id IS NULL
              )
        )
    """


def _plaza_child_predicate(table: str) -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM plaza_posts
            WHERE plaza_posts.id = {table}.post_id
              AND (
                  plaza_posts.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR plaza_posts.tenant_id IS NULL
              )
        )
    """


def _skill_file_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM skills
            WHERE skills.id = skill_files.skill_id
              AND (
                  skills.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR skills.tenant_id IS NULL
              )
        )
    """


def _external_identity_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR EXISTS (
            SELECT 1
            FROM identity_providers
            WHERE identity_providers.id = external_identities.provider_id
              AND (
                  identity_providers.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR identity_providers.tenant_id IS NULL
              )
        )
        OR EXISTS (
            SELECT 1
            FROM users
            WHERE users.id = external_identities.user_id
              AND (
                  users.tenant_id::text = current_setting('app.current_tenant_id', true)
                  OR users.tenant_id IS NULL
              )
        )
    """


def _participant_predicate() -> str:
    return f"""
        {_bypass_predicate()}
        OR (
            participants.type = 'user'
            AND EXISTS (
                SELECT 1
                FROM users
                WHERE users.id = participants.ref_id
                  AND (
                      users.tenant_id::text = current_setting('app.current_tenant_id', true)
                      OR users.tenant_id IS NULL
                  )
            )
        )
        OR (
            participants.type = 'agent'
            AND EXISTS (
                SELECT 1
                FROM agents
                WHERE agents.id = participants.ref_id
                  AND (
                      agents.tenant_id::text = current_setting('app.current_tenant_id', true)
                      OR agents.tenant_id IS NULL
                  )
            )
        )
    """


def _feature_flag_predicate() -> str:
    return "true"


def _system_settings_predicate() -> str:
    quoted_keys = ", ".join(f"'{key}'" for key in _SECRET_SYSTEM_SETTING_KEYS)
    return f"""
        {_bypass_predicate()}
        OR system_settings.key <> ALL (ARRAY[{quoted_keys}]::text[])
    """


def _bypass_only_predicate() -> str:
    return _bypass_predicate()


def _policy_specs() -> dict[str, tuple[str, str]]:
    user_predicate = _user_owned_predicate
    return {
        "tenants": (_tenant_catalog_predicate(), _tenant_catalog_predicate()),
        "notifications": (user_predicate("notifications"), user_predicate("notifications")),
        "refresh_tokens": (user_predicate("refresh_tokens"), user_predicate("refresh_tokens")),
        "plaza_comments": (_plaza_child_predicate("plaza_comments"), _plaza_child_predicate("plaza_comments")),
        "plaza_likes": (_plaza_child_predicate("plaza_likes"), _plaza_child_predicate("plaza_likes")),
        "skill_files": (_skill_file_predicate(), _skill_file_predicate()),
        "external_identities": (_external_identity_predicate(), _external_identity_predicate()),
        "participants": (_participant_predicate(), _participant_predicate()),
        "feature_flags": (_feature_flag_predicate(), _feature_flag_predicate()),
        "system_settings": (_system_settings_predicate(), _system_settings_predicate()),
        "identities": (_bypass_only_predicate(), _bypass_only_predicate()),
    }


def _enable_rls(table: str, using_predicate: str, check_predicate: str) -> None:
    policy_name = f"tenant_isolation_{table}"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {policy_name} ON {table}")
    op.execute(
        f"""
        CREATE POLICY {policy_name} ON {table}
            USING ({using_predicate})
            WITH CHECK ({check_predicate})
        """
    )


def upgrade() -> None:
    existing = _existing_tables()
    for table, (using_predicate, check_predicate) in _policy_specs().items():
        if table in existing:
            _enable_rls(table, using_predicate, check_predicate)


def downgrade() -> None:
    existing = _existing_tables()
    for table in reversed(_ALL_TABLES):
        if table not in existing:
            continue
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
