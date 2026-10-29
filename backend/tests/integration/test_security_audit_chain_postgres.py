"""Tenant audit writes retain their order inside one PostgreSQL transaction."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.policy import compute_audit_event_hash, write_audit_event
from app.database import tenant_scoped_session
from app.db_bootstrap import apply_security_audit_sequence
from app.models.security_audit import SecurityAuditEvent
from app.models.tenant import Tenant
from tests.integration import conftest as database_fixtures
from tests.integration import test_terminal_lane_route_migration_postgres as migration_fixtures

app_user_engine = database_fixtures.app_user_engine
app_user_sessionmaker = database_fixtures.app_user_sessionmaker
owner_engine = database_fixtures.owner_engine
owner_sessionmaker = database_fixtures.owner_sessionmaker
migrate = migration_fixtures.migrate
migrated_pg_url = migration_fixtures.migrated_pg_url
pg_container = migration_fixtures.pg_container


async def test_same_transaction_audit_writes_form_one_ordered_chain(
    owner_sessionmaker, app_user_sessionmaker,
):
    tenant_id = uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Audit chain", slug=uuid4().hex))
        await db.commit()

    async with tenant_scoped_session(tenant_id, session_factory=app_user_sessionmaker) as db:
        receipts = [
            await write_audit_event(
                db, tenant_id=tenant_id, event_type="tool.execution", severity="info",
                actor_type="system", actor_id=None, action="observe", details={"observation": index},
            )
            for index in range(4)
        ]
        events = list((await db.scalars(
            select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == tenant_id)
            .order_by(SecurityAuditEvent.sequence_num)
        )).all())
        assert len(events) == 4
        assert all(isinstance(event.sequence_num, int) for event in events)
        assert len({event.sequence_num for event in events}) == 4
        assert [event.id for event in events] == [receipt.event_id for receipt in receipts]
        assert len({event.created_at for event in events}) == 1
        previous_hash = "genesis"
        for event in events:
            assert event.prev_hash == previous_hash
            assert event.event_hash == compute_audit_event_hash(
                tenant_id=tenant_id, event_type=event.event_type, severity=event.severity,
                actor_type=event.actor_type, actor_id=event.actor_id, action=event.action, details=event.details,
                prev_hash=previous_hash,
            )
            previous_hash = event.event_hash


@pytest.mark.parametrize("allocator", ["missing", "ahead"])
async def test_populated_upgrade_preserves_legacy_rows_and_sequence_high_water(
    owner_sessionmaker, migrated_pg_url, tmp_path, allocator,
):
    tenant_id, legacy_tenant_id = uuid4(), uuid4()
    migrate(migrated_pg_url, "downgrade", "terminal_route_bootstrap_1029", tmp_path, "parent")
    async with owner_sessionmaker() as db:
        db.add_all([
            Tenant(id=tenant_id, name="Existing audit", slug=uuid4().hex),
            Tenant(id=legacy_tenant_id, name="Unordered legacy audit", slug=uuid4().hex),
        ])
        await db.flush()
        anchor_sequence = await db.scalar(select(func.coalesce(func.max(SecurityAuditEvent.sequence_num), 0))) + 9000
        high_water = anchor_sequence + 3000
        await db.execute(text("ALTER TABLE security_audit_events ALTER COLUMN sequence_num DROP DEFAULT"))
        for tenant, sequence in [(tenant_id, anchor_sequence), (legacy_tenant_id, None)]:
            event_hash = compute_audit_event_hash(
                tenant_id=tenant, event_type="tool.execution", severity="info",
                actor_type="system", actor_id=None, action="legacy_observation", prev_hash="genesis",
            )
            await db.execute(text("""
                INSERT INTO security_audit_events
                    (id,tenant_id,sequence_num,event_type,severity,actor_type,action,details,prev_hash,event_hash)
                VALUES (:id,:tenant,:sequence,'tool.execution','info','system','legacy_observation',
                    '{}'::jsonb,'genesis',:hash)
            """), {"id": uuid4(), "tenant": tenant, "sequence": sequence, "hash": event_hash})
        if allocator == "missing":
            await db.execute(text("DROP SEQUENCE security_audit_events_sequence_num_seq"))
        else:
            await db.execute(text("SELECT setval('security_audit_events_sequence_num_seq',:high_water,true)"),
                {"high_water": high_water})
        role, password = "audit_schema_" + uuid4().hex, uuid4().hex
        await db.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"))
        await db.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {role}"))
        await db.execute(text(f"ALTER TABLE alembic_version OWNER TO {role}"))
        await db.execute(text(f"ALTER TABLE security_audit_events OWNER TO {role}"))
        snapshot = text("SELECT to_jsonb(e) FROM security_audit_events e ORDER BY id")
        before = list((await db.scalars(snapshot)).all())
        await db.commit()

    migration_url = make_url(migrated_pg_url).set(username=role, password=password).render_as_string(hide_password=False)
    migrate(migration_url, "upgrade", "head", tmp_path, "repair")
    schema_engine = create_async_engine(migration_url)
    try:
        async with schema_engine.begin() as connection:
            assert (await connection.execute(text(
                "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ))).one() == (False, False)
            assert (await connection.execute(text("""
                SELECT relrowsecurity,relforcerowsecurity,pg_get_userbyid(relowner)=current_user
                FROM pg_class WHERE oid='security_audit_events'::regclass
            """))).one() == (True, True, True)
            await connection.execute(text("SELECT set_config('app.current_tenant_id',:tenant,true)"),
                {"tenant": str(legacy_tenant_id)})
            await connection.run_sync(apply_security_audit_sequence)
            assert await connection.scalar(text("SELECT current_setting('app.current_tenant_id')")) == str(legacy_tenant_id)
    finally:
        await schema_engine.dispose()
    async with owner_sessionmaker() as db:
        assert list((await db.scalars(snapshot)).all()) == before
        default = await db.scalar(text("""
            SELECT column_default FROM information_schema.columns
            WHERE table_schema='public' AND table_name='security_audit_events' AND column_name='sequence_num'
        """))
        assert "nextval" in default
        receipt = await write_audit_event(
            db, tenant_id=tenant_id, event_type="tool.execution", actor_type="system", actor_id=None,
            action="current_observation",
        )
        current = await db.get(SecurityAuditEvent, receipt.event_id)
        assert current.sequence_num > (anchor_sequence if allocator == "missing" else high_water)
        previous = next(row for row in before if row["tenant_id"] == str(tenant_id))
        assert current.prev_hash == previous["event_hash"]
        assert await db.scalar(select(SecurityAuditEvent.sequence_num).where(
            SecurityAuditEvent.tenant_id == legacy_tenant_id,
        )) is None
        await db.commit()
