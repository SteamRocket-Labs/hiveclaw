from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "invitation_role_binding_0831.py"
PARENT_REVISION = "runtime_terminal_boundary_0831"
CONSTRAINT = "ck_invitation_codes_granted_role"
TRIGGER_FUNCTION = "bind_legacy_invitation_granted_role_0831"
TRIGGER = "trg_invitation_codes_bind_legacy_granted_role_0831"


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}"


def _new_database_url(pg_container, prefix: str) -> str:
    from tests.integration.conftest import _async_url

    database = f"{prefix}{uuid.uuid4().hex[:12]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f'CREATE DATABASE "{database}"'])
    assert code == 0, output
    return make_url(_async_url(pg_container)).set(database=database).render_as_string(hide_password=False)


def test_invitation_role_model_and_migration_contract() -> None:
    from app.database import Base
    from app.models import import_all_models

    import_all_models()
    table = Base.metadata.tables["invitation_codes"]
    assert not table.c.granted_role.nullable
    assert table.c.granted_role.server_default is None
    assert table.c.granted_role.default.arg == "member"
    assert CONSTRAINT in {constraint.name for constraint in table.constraints}

    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "invitation_role_binding_0831"' in source
    assert f'down_revision = "{PARENT_REVISION}"' in source
    assert "Preserve the old join contract at cutover" in source
    assert "ALTER COLUMN granted_role DROP DEFAULT" in source
    assert "SECURITY DEFINER" in source
    assert "SET app.current_tenant_id = 'BYPASS'" in source
    assert "target_has_admin" in source
    assert "granted_role IN ('member', 'org_admin')" in source
    assert "DROP TRIGGER IF EXISTS" in source
    assert "DROP FUNCTION IF EXISTS" in source
    assert "DROP COLUMN IF EXISTS granted_role" in source


@pytest.mark.asyncio
async def test_real_migration_backfill_and_rolling_old_writer_contract(pg_container) -> None:
    database_url = _new_database_url(pg_container, "inviterole")
    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", PARENT_REVISION)

    tenant_id = uuid.uuid4()
    platform_admin_id = uuid.uuid4()
    org_admin_id = uuid.uuid4()
    bootstrap_tenant_id = uuid.uuid4()
    rolling_bootstrap_tenant_id = uuid.uuid4()
    legacy_platform_invite_id = uuid.uuid4()
    legacy_org_invite_id = uuid.uuid4()
    legacy_bootstrap_invite_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants ("
                    "id,name,slug,im_provider,is_active,min_heartbeat_interval_minutes,timezone,"
                    "default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,"
                    "tokens_used_today,tokens_used_month,tokens_used_total,sync_version"
                    ") VALUES ("
                    ":id,'Legacy Invite Tenant',:slug,'web_only',true,45,'UTC',20,5,5,0,0,0,1"
                    ")"
                ),
                {"id": tenant_id, "slug": f"legacy-invite-{tenant_id.hex[:8]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO tenants ("
                    "id,name,slug,im_provider,is_active,min_heartbeat_interval_minutes,timezone,"
                    "default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,"
                    "tokens_used_today,tokens_used_month,tokens_used_total,sync_version"
                    ") VALUES ("
                    ":id,'Legacy Bootstrap Tenant',:slug,'web_only',true,45,'UTC',20,5,5,0,0,0,1"
                    ")"
                ),
                {"id": bootstrap_tenant_id, "slug": f"legacy-bootstrap-{bootstrap_tenant_id.hex[:8]}"},
            )
            await connection.execute(
                text(
                    "INSERT INTO users ("
                    "id,username,email,password_hash,display_name,role,tenant_id,is_active,"
                    "must_change_password,tokens_used_today,tokens_used_month,tokens_used_total"
                    ") VALUES ("
                    ":id,:username,:email,'hash',:display_name,:role,:tenant_id,true,"
                    "false,0,0,0"
                    ")"
                ),
                [
                    {
                        "id": platform_admin_id,
                        "username": f"legacy-platform-{platform_admin_id.hex[:8]}",
                        "email": f"legacy-platform-{platform_admin_id.hex[:8]}@example.com",
                        "display_name": "Legacy Platform Issuer",
                        "role": "platform_admin",
                        "tenant_id": tenant_id,
                    },
                    {
                        "id": org_admin_id,
                        "username": f"legacy-org-{org_admin_id.hex[:8]}",
                        "email": f"legacy-org-{org_admin_id.hex[:8]}@example.com",
                        "display_name": "Legacy Org Issuer",
                        "role": "org_admin",
                        "tenant_id": tenant_id,
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO invitation_codes "
                    "(id,code,tenant_id,max_uses,used_count,is_active,created_by) "
                    "VALUES (:id,:code,:tenant_id,1,0,true,:created_by)"
                ),
                [
                    {
                        "id": legacy_platform_invite_id,
                        "code": "LEGACYPLATFORM",
                        "tenant_id": tenant_id,
                        "created_by": platform_admin_id,
                    },
                    {
                        "id": legacy_org_invite_id,
                        "code": "LEGACYORGADMIN",
                        "tenant_id": tenant_id,
                        "created_by": org_admin_id,
                    },
                    {
                        "id": legacy_bootstrap_invite_id,
                        "code": "LEGACYBOOTSTRAP",
                        "tenant_id": bootstrap_tenant_id,
                        "created_by": platform_admin_id,
                    },
                ],
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            legacy_roles = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT code,granted_role FROM invitation_codes "
                            "WHERE id IN (:platform_id,:org_id,:bootstrap_id) ORDER BY code"
                        ),
                        {
                            "platform_id": legacy_platform_invite_id,
                            "org_id": legacy_org_invite_id,
                            "bootstrap_id": legacy_bootstrap_invite_id,
                        },
                    )
                ).all()
            )
            assert legacy_roles == {
                "LEGACYBOOTSTRAP": "org_admin",
                "LEGACYORGADMIN": "member",
                "LEGACYPLATFORM": "member",
            }

            column = (
                await connection.execute(
                    text(
                        "SELECT is_nullable,column_default FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='invitation_codes' "
                        "AND column_name='granted_role'"
                    )
                )
            ).one()
            assert column.is_nullable == "NO"
            assert column.column_default is None
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        "WHERE conrelid='invitation_codes'::regclass AND conname=:name"
                    ),
                    {"name": CONSTRAINT},
                )
                == 1
            )

            await connection.execute(
                text(
                    "INSERT INTO tenants ("
                    "id,name,slug,im_provider,is_active,min_heartbeat_interval_minutes,timezone,"
                    "default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,"
                    "tokens_used_today,tokens_used_month,tokens_used_total,sync_version"
                    ") VALUES ("
                    ":id,'Rolling Bootstrap Tenant',:slug,'web_only',true,45,'UTC',20,5,5,0,0,0,1"
                    ")"
                ),
                {
                    "id": rolling_bootstrap_tenant_id,
                    "slug": f"rolling-bootstrap-{rolling_bootstrap_tenant_id.hex[:8]}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO invitation_codes "
                    "(id,code,tenant_id,max_uses,used_count,is_active,created_by) "
                    "VALUES (:id,'OLDWRITERBOOTSTRAP',:tenant_id,1,0,true,:created_by)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": rolling_bootstrap_tenant_id,
                    "created_by": platform_admin_id,
                },
            )
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO invitation_codes "
                    "(id,code,tenant_id,max_uses,used_count,is_active,created_by) "
                    "VALUES (:id,:code,:tenant_id,1,0,true,:created_by)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "code": "OLDWRITERPLATFORM",
                        "tenant_id": tenant_id,
                        "created_by": platform_admin_id,
                    },
                    {
                        "id": uuid.uuid4(),
                        "code": "OLDWRITERORG",
                        "tenant_id": tenant_id,
                        "created_by": org_admin_id,
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO invitation_codes "
                    "(id,code,tenant_id,max_uses,used_count,is_active,created_by,granted_role) "
                    "VALUES (:id,:code,:tenant_id,1,0,true,:created_by,:granted_role)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "code": "EXPLICITMEMBER",
                        "tenant_id": tenant_id,
                        "created_by": platform_admin_id,
                        "granted_role": "member",
                    },
                    {
                        "id": uuid.uuid4(),
                        "code": "EXPLICITORGADMIN",
                        "tenant_id": tenant_id,
                        "created_by": org_admin_id,
                        "granted_role": "org_admin",
                    },
                ],
            )
            roles = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT code,granted_role FROM invitation_codes "
                            "WHERE code LIKE 'OLDWRITER%' OR code LIKE 'EXPLICIT%' ORDER BY code"
                        )
                    )
                ).all()
            )
            assert roles == {
                "EXPLICITMEMBER": "member",
                "EXPLICITORGADMIN": "org_admin",
                "OLDWRITERBOOTSTRAP": "org_admin",
                "OLDWRITERORG": "member",
                "OLDWRITERPLATFORM": "member",
            }
            assert await connection.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == str(
                tenant_id
            )

            savepoint = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO invitation_codes "
                        "(id,code,tenant_id,max_uses,used_count,is_active,created_by,granted_role) "
                        "VALUES (:id,'INVALIDROLE',:tenant_id,1,0,true,:created_by,'platform_admin')"
                    ),
                    {"id": uuid.uuid4(), "tenant_id": tenant_id, "created_by": platform_admin_id},
                )
            await savepoint.rollback()

            from tests.integration.conftest import APP_USER, APP_USER_PASSWORD

            await connection.execute(
                text(
                    f"DO $$ BEGIN CREATE ROLE {APP_USER} LOGIN PASSWORD '{APP_USER_PASSWORD}' "
                    "NOSUPERUSER NOBYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_USER}"))
            await connection.execute(text(f"GRANT INSERT, SELECT ON public.invitation_codes TO {APP_USER}"))
    finally:
        await engine.dispose()

    from tests.integration.conftest import APP_USER, APP_USER_PASSWORD

    app_database_url = make_url(database_url).set(username=APP_USER, password=APP_USER_PASSWORD)
    app_engine = create_async_engine(app_database_url, poolclass=NullPool)
    try:
        async with app_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO public.invitation_codes "
                    "(id,code,tenant_id,max_uses,used_count,is_active,created_by) "
                    "VALUES (:id,:code,:tenant_id,1,0,true,:created_by)"
                ),
                [
                    {
                        "id": uuid.uuid4(),
                        "code": "APPROLEOLDPLATFORM",
                        "tenant_id": tenant_id,
                        "created_by": platform_admin_id,
                    },
                    {
                        "id": uuid.uuid4(),
                        "code": "APPROLEOLDORG",
                        "tenant_id": tenant_id,
                        "created_by": org_admin_id,
                    },
                ],
            )
            roles = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT code,granted_role FROM public.invitation_codes "
                            "WHERE code LIKE 'APPROLEOLD%' ORDER BY code"
                        )
                    )
                ).all()
            )
            assert roles == {
                "APPROLEOLDORG": "member",
                "APPROLEOLDPLATFORM": "member",
            }
            assert await connection.scalar(text("SELECT current_setting('app.current_tenant_id', true)")) == str(
                tenant_id
            )
    finally:
        await app_engine.dispose()

    _alembic(database_url, "downgrade", PARENT_REVISION)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            cleanup = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM information_schema.columns "
                        " WHERE table_schema='public' AND table_name='invitation_codes' "
                        " AND column_name='granted_role'),"
                        "(SELECT count(*) FROM pg_constraint "
                        " WHERE conrelid='invitation_codes'::regclass AND conname=:constraint),"
                        "(SELECT count(*) FROM pg_trigger "
                        " WHERE tgrelid='invitation_codes'::regclass AND tgname=:trigger),"
                        "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        " WHERE n.nspname='public' AND p.proname=:function)"
                    ),
                    {"constraint": CONSTRAINT, "trigger": TRIGGER, "function": TRIGGER_FUNCTION},
                )
            ).one()
            assert tuple(cleanup) == (0, 0, 0, 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_migration_accepts_fresh_create_all_precreated_column_and_check(pg_container) -> None:
    from app.database import Base
    from app.models import import_all_models

    import_all_models()
    database_url = _new_database_url(pg_container, "invitefresh")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            precreated = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT column_default FROM information_schema.columns "
                        " WHERE table_schema='public' AND table_name='invitation_codes' "
                        " AND column_name='granted_role'),"
                        "(SELECT count(*) FROM pg_constraint "
                        " WHERE conrelid='invitation_codes'::regclass AND conname=:constraint),"
                        "(SELECT count(*) FROM pg_trigger "
                        " WHERE tgrelid='invitation_codes'::regclass AND tgname=:trigger)"
                    ),
                    {"constraint": CONSTRAINT, "trigger": TRIGGER},
                )
            ).one()
            assert tuple(precreated) == (None, 1, 0)
            await connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(128) NOT NULL PRIMARY KEY)")
            )
            await connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": PARENT_REVISION},
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            installed = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT column_default FROM information_schema.columns "
                        " WHERE table_schema='public' AND table_name='invitation_codes' "
                        " AND column_name='granted_role'),"
                        "(SELECT count(*) FROM pg_constraint "
                        " WHERE conrelid='invitation_codes'::regclass AND conname=:constraint),"
                        "(SELECT count(*) FROM pg_trigger "
                        " WHERE tgrelid='invitation_codes'::regclass AND tgname=:trigger),"
                        "(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        " WHERE n.nspname='public' AND p.proname=:function)"
                    ),
                    {"constraint": CONSTRAINT, "trigger": TRIGGER, "function": TRIGGER_FUNCTION},
                )
            ).one()
            assert tuple(installed) == (None, 1, 1, 1)
    finally:
        await engine.dispose()
