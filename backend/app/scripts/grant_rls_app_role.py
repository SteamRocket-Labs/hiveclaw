"""Idempotent GRANTs for the non-owner RLS application role (stage-3 prep).

Run during deploy AFTER migrations so every table — including ones added this
release — is reachable by ``app_rls``, the non-owner role the stage-3 cutover
points the runtime ``DATABASE_URL`` at. RLS still filters what ``app_rls`` SEES;
GRANT only controls table-level access, which a freshly created non-owner role
lacks by default. Without this, the role flip would make every table return
"permission denied" (not just RLS-filtered) for tables added after the role was
first granted.

No-op (with a clear log) when the role does not exist yet — the owner creates it
once via ``docs/rls-stage3-cutover.md``. Runs on the owner connection
(``schema_engine``) since only the owner can GRANT. Idempotent; safe every deploy.

    python -m app.scripts.grant_rls_app_role
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.database import schema_engine

# Constant SQL — the role name is a literal, never interpolated, so there is no
# injection surface and the statements stay parameter-free DDL.
_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO app_rls",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rls",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rls",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rls",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_rls",
)


async def grant_rls_app_role() -> None:
    """Grant the app_rls role DML access to every current + future table."""
    async with schema_engine.begin() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'app_rls'"))).scalar()
        if not exists:
            print(
                "[grant_rls_app_role] role app_rls does not exist yet — skipping. "
                "Create it via docs/rls-stage3-cutover.md before the role flip."
            )
            return
        for stmt in _GRANTS:
            await conn.execute(text(stmt))
    print("[grant_rls_app_role] grants applied to app_rls (current + default-privilege future tables)")


def main() -> None:
    asyncio.run(grant_rls_app_role())


if __name__ == "__main__":
    main()
