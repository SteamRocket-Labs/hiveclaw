"""Bootstrap + grant the non-owner RLS application role (stage-3 prep).

Runs every deploy on the owner connection (``schema_engine``):

1. **Create/refresh** the ``app_rls`` LOGIN role (NOSUPERUSER NOBYPASSRLS) when
   ``RLS_APP_PASSWORD`` is set — the password is quoted with PostgreSQL
   ``format('%L')`` and passed as a bind parameter, so there is no
   string-formatted SQL and no injection surface. No-op when the variable is
   unset (the owner has not chosen to prepare the flip yet).
2. **Grant** the role DML on every current + future table. RLS still filters what
   ``app_rls`` SEES; GRANT only controls table-level access, which a non-owner
   role lacks by default. Skipped (with a log) if the role still does not exist.

The role flip itself (pointing the runtime ``DATABASE_URL`` at ``app_rls``) is a
separate env change — see docs/rls-stage3-cutover.md. Idempotent; safe every deploy.

    python -m app.scripts.grant_rls_app_role
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from app.config import get_settings
from app.database import schema_engine

# Constant SQL — role name is a literal, never interpolated.
_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO app_rls",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rls",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rls",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rls",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_rls",
)

# format('%L') templates — the password arrives as a bind param, format quotes it
# safely into the DDL. No f-string, no concatenation of the secret into SQL.
_CREATE_ROLE_FMT = (
    "SELECT format("
    "'CREATE ROLE app_rls LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE', "
    "cast(:pw AS text))"
)
_ALTER_ROLE_FMT = "SELECT format('ALTER ROLE app_rls LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS', cast(:pw AS text))"

_ROLE_SETTING_FORMATS = (
    (
        "statement_timeout",
        "SELECT format('ALTER ROLE app_rls SET statement_timeout = %L', cast(:value AS text))",
    ),
    (
        "idle_in_transaction_session_timeout",
        "SELECT format('ALTER ROLE app_rls SET idle_in_transaction_session_timeout = %L', cast(:value AS text))",
    ),
    (
        "temp_file_limit",
        "SELECT format('ALTER ROLE app_rls SET temp_file_limit = %L', cast(:value AS text))",
    ),
    (
        "log_temp_files",
        "SELECT format('ALTER ROLE app_rls SET log_temp_files = %L', cast(:value AS text))",
    ),
)


def _role_setting_values() -> dict[str, str]:
    settings = get_settings()
    return {
        "statement_timeout": f"{max(1, int(settings.DB_STATEMENT_TIMEOUT_MS))}ms",
        "idle_in_transaction_session_timeout": (f"{max(1, int(settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS))}ms"),
        "temp_file_limit": f"{max(1, int(settings.DB_TEMP_FILE_LIMIT_KB))}kB",
        "log_temp_files": f"{max(0, int(settings.DB_LOG_TEMP_FILES_KB))}kB",
    }


async def grant_rls_app_role() -> None:
    """Create/refresh app_rls (if RLS_APP_PASSWORD set) and grant it DML."""
    password = os.environ.get("RLS_APP_PASSWORD")
    async with schema_engine.begin() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'app_rls'"))).scalar()
        if password:
            was_existing = bool(exists)
            fmt = _ALTER_ROLE_FMT if was_existing else _CREATE_ROLE_FMT
            ddl = (await conn.execute(text(fmt), {"pw": password})).scalar()
            if not ddl:
                raise RuntimeError("format() returned no DDL for the app_rls role")
            await conn.execute(text(str(ddl)))
            exists = True
            print("[grant_rls_app_role] app_rls role " + ("refreshed" if was_existing else "created"))
        if not exists:
            print(
                "[grant_rls_app_role] role app_rls does not exist and RLS_APP_PASSWORD is unset — "
                "skipping grants. Set RLS_APP_PASSWORD to prepare the stage-3 flip."
            )
            return
        for stmt in _GRANTS:
            await conn.execute(text(stmt))
        role_settings = _role_setting_values()
        for setting_name, format_sql in _ROLE_SETTING_FORMATS:
            ddl = (await conn.execute(text(format_sql), {"value": role_settings[setting_name]})).scalar()
            if not ddl:
                raise RuntimeError(f"format() returned no DDL for app_rls {setting_name}")
            await conn.execute(text(str(ddl)))
    print(
        "[grant_rls_app_role] grants and bounded query defaults applied to app_rls "
        "(current + default-privilege future tables)"
    )


def main() -> None:
    asyncio.run(grant_rls_app_role())


if __name__ == "__main__":
    main()
