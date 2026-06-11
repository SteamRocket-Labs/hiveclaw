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
    print("[grant_rls_app_role] grants applied to app_rls (current + default-privilege future tables)")


def main() -> None:
    asyncio.run(grant_rls_app_role())


if __name__ == "__main__":
    main()
