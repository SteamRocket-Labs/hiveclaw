"""Upgrade an M1-B org_members row with the department observation column.

Empty databases bootstrap at the current head, so this test rewinds to
m1b_identity_links_1015, inserts a row that cannot mention the new column,
and upgrades again. It does not replay every historical revision.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


PARENT = "m1b_identity_links_1015"


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.migrations.conftest import _alembic_upgrade
    from tests.migrations.test_hr_draft_recovery_migration import _alembic as _run

    if command == "upgrade":
        _alembic_upgrade(database_url, target)
        return
    _run(database_url, command, target)


async def test_directory_observation_upgrade_preserves_m1b_member(pg_container) -> None:
    from app.models.org import OrgDepartment, OrgMember
    from app.models.tenant import Tenant
    from tests.integration.conftest import _async_url

    database_name = f"m1dobs_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic(database_url, "upgrade", "head")

    tenant_id = uuid.uuid4()
    department_id = uuid.uuid4()
    kept_id = uuid.uuid4()
    inserted_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            db.add(Tenant(id=tenant_id, name="Observation upgrade", slug=f"m1d-obs-{tenant_id.hex[:8]}"))
            await db.flush()
            db.add(OrgDepartment(
                id=department_id, tenant_id=tenant_id, name="Directory", feishu_id=f"od-{department_id.hex[:8]}",
            ))
            await db.flush()
            db.add(OrgMember(
                id=kept_id, tenant_id=tenant_id, department_id=department_id, name="Kept",
                title="Lead", phone="100", avatar_url="kept.png", status="active",
            ))
            await db.commit()
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", PARENT)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO org_members "
                    "(id, name, title, department_path, status, tenant_id, department_id, phone, avatar_url) "
                    "VALUES (:id, :name, :title, '', 'active', :tenant, :department, :phone, :avatar)"
                ),
                {
                    "id": inserted_id,
                    "name": "Inserted",
                    "title": "Staff",
                    "tenant": tenant_id,
                    "department": department_id,
                    "phone": "200",
                    "avatar": "inserted.png",
                },
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT name, title, phone, avatar_url, department_id::text, "
                        "observed_directory_department_ids "
                        "FROM org_members ORDER BY name"
                    )
                )
            ).all()
            constraint = await connection.scalar(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conname = 'ck_org_members_observed_directory_departments'"
                )
            )
        assert constraint == "ck_org_members_observed_directory_departments"
        assert [
            (row.name, row.title, row.phone, row.avatar_url, row.department_id, row.observed_directory_department_ids)
            for row in rows
        ] == [
            ("Inserted", "Staff", "200", "inserted.png", str(department_id), None),
            ("Kept", "Lead", "100", "kept.png", str(department_id), None),
        ]
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE org_members SET observed_directory_department_ids = CAST(:value AS jsonb) WHERE id = :id"
                ),
                {"value": '["od-a", "od-b"]', "id": kept_id},
            )
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE org_members SET observed_directory_department_ids = CAST(:value AS jsonb) WHERE id = :id"
                    ),
                    {"value": "[]", "id": inserted_id},
                )
        except DBAPIError:
            rejected = True
        else:
            rejected = False
        assert rejected, "An empty department observation must fail the check."
        async with engine.connect() as connection:
            stored = await connection.scalar(
                text("SELECT observed_directory_department_ids FROM org_members WHERE id = :id"),
                {"id": kept_id},
            )
        assert stored == ["od-a", "od-b"]
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", PARENT)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            column = await connection.scalar(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'org_members' "
                    "AND column_name = 'observed_directory_department_ids'"
                )
            )
            surviving = (
                await connection.execute(text("SELECT name, title, phone, department_id::text FROM org_members ORDER BY name"))
            ).all()
        assert column is None
        assert [(row.name, row.title, row.phone, row.department_id) for row in surviving] == [
            ("Inserted", "Staff", "200", str(department_id)),
            ("Kept", "Lead", "100", str(department_id)),
        ]
    finally:
        await engine.dispose()
