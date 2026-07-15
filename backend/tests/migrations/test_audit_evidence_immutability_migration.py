from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import import_all_models
from app.models.audit import AuditLog
from app.models.external_principal import ExternalPrincipal
from app.models.security_audit import SecurityAuditEvent
from app.models.tenant import Tenant


BACKEND_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = BACKEND_ROOT / "alembic" / "versions" / "audit_evidence_immutability_0715.py"


def test_migration_contract_guards_both_audit_evidence_tables() -> None:
    migration = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "audit_evidence_immutability_0715"' in migration
    assert 'down_revision = "personal_kb_authority_0715"' in migration
    assert "CREATE OR REPLACE FUNCTION reject_audit_evidence_mutation()" in migration
    assert "audit evidence is append-only" in migration
    assert "trg_audit_logs_immutable" in migration
    assert "BEFORE UPDATE OR DELETE ON audit_logs" in migration
    assert "trg_security_audit_events_immutable" in migration
    assert "BEFORE UPDATE OR DELETE ON security_audit_events" in migration
    assert "trg_audit_logs_no_truncate" in migration
    assert "BEFORE TRUNCATE ON audit_logs" in migration
    assert "trg_security_audit_events_no_truncate" in migration
    assert "BEFORE TRUNCATE ON security_audit_events" in migration
    assert ('op.drop_constraint("fk_audit_logs_external_principal_id", "audit_logs", type_="foreignkey")') in migration
    assert 'ondelete="RESTRICT"' in migration
    assert migration.count('ondelete="SET NULL"') == 1
    assert "DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs" in migration
    assert "DROP TRIGGER IF EXISTS trg_security_audit_events_immutable ON security_audit_events" in migration
    assert "DROP TRIGGER IF EXISTS trg_audit_logs_no_truncate ON audit_logs" in migration
    assert ("DROP TRIGGER IF EXISTS trg_security_audit_events_no_truncate ON security_audit_events") in migration
    assert "DROP FUNCTION IF EXISTS reject_audit_evidence_mutation()" in migration


def test_fresh_bootstrap_wires_the_same_audit_evidence_guard() -> None:
    bootstrap = (BACKEND_ROOT / "app" / "db_bootstrap.py").read_text(encoding="utf-8")

    assert "def apply_audit_evidence_immutability" in bootstrap
    assert "CREATE OR REPLACE FUNCTION reject_audit_evidence_mutation()" in bootstrap
    assert "BEFORE UPDATE OR DELETE ON {table}" in bootstrap
    assert "BEFORE TRUNCATE ON {table}" in bootstrap
    assert bootstrap.count("apply_audit_evidence_immutability(connection)") == 2


async def test_fresh_bootstrap_installs_both_audit_evidence_guards(
    migrated_pg_url: str,
) -> None:
    engine = create_async_engine(migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            trigger_rows = (
                await connection.execute(
                    text(
                        "SELECT c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid) "
                        "FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal "
                        "AND t.tgname IN "
                        "('trg_audit_logs_immutable', "
                        "'trg_security_audit_events_immutable', "
                        "'trg_audit_logs_no_truncate', "
                        "'trg_security_audit_events_no_truncate')"
                    )
                )
            ).all()
            audit_principal_fk = await connection.scalar(
                text(
                    "SELECT pg_get_constraintdef(oid) "
                    "FROM pg_constraint "
                    "WHERE conrelid = 'audit_logs'::regclass "
                    "AND conname = 'fk_audit_logs_external_principal_id'"
                )
            )

        triggers = {row.tgname: row for row in trigger_rows}
        assert set(triggers) == {
            "trg_audit_logs_immutable",
            "trg_security_audit_events_immutable",
            "trg_audit_logs_no_truncate",
            "trg_security_audit_events_no_truncate",
        }
        assert triggers["trg_audit_logs_immutable"].relname == "audit_logs"
        assert triggers["trg_security_audit_events_immutable"].relname == "security_audit_events"
        assert triggers["trg_audit_logs_no_truncate"].relname == "audit_logs"
        assert triggers["trg_security_audit_events_no_truncate"].relname == "security_audit_events"
        enabled_states = {row.tgenabled for row in triggers.values()}
        assert enabled_states == {b"O"}
        assert all("BEFORE" in row.pg_get_triggerdef for row in triggers.values())
        assert all(
            "UPDATE" in triggers[name].pg_get_triggerdef and "DELETE" in triggers[name].pg_get_triggerdef
            for name in (
                "trg_audit_logs_immutable",
                "trg_security_audit_events_immutable",
            )
        )
        assert audit_principal_fk is not None
        assert "ON DELETE RESTRICT" in audit_principal_fk
        assert all(
            "TRUNCATE" in triggers[name].pg_get_triggerdef
            for name in (
                "trg_audit_logs_no_truncate",
                "trg_security_audit_events_no_truncate",
            )
        )
    finally:
        await engine.dispose()


async def test_release_upgrade_rejects_direct_mutation_for_both_audit_tables(
    chain_migrated_pg_url: str,
) -> None:
    import_all_models()
    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    audit_log_id = uuid.uuid4()
    security_event_id = uuid.uuid4()
    external_principal_id = uuid.uuid4()

    try:
        async with session_factory() as db:
            db.add(
                Tenant(
                    id=tenant_id,
                    name="Audit Immutability Tenant",
                    slug=f"audit-immutable-{tenant_id.hex[:12]}",
                )
            )
            await db.commit()

        async with session_factory() as db:
            db.add(
                ExternalPrincipal(
                    id=external_principal_id,
                    tenant_id=tenant_id,
                    provider="slack",
                    installation_ref="audit-immutability-proof",
                    subject_id="audit-proof-subject",
                    display_name="Audit Proof Principal",
                )
            )
            await db.commit()

        async with session_factory() as db:
            db.add(
                AuditLog(
                    id=audit_log_id,
                    tenant_id=tenant_id,
                    external_principal_id=external_principal_id,
                    action="audit.proof.created",
                    details={"proof": "original"},
                )
            )
            db.add(
                SecurityAuditEvent(
                    id=security_event_id,
                    event_type="audit_proof",
                    severity="info",
                    actor_type="system",
                    tenant_id=tenant_id,
                    action="security.audit.proof.created",
                    details={"proof": "original"},
                    prev_hash="genesis",
                    event_hash="a" * 64,
                )
            )
            await db.commit()

        async with engine.connect() as connection:
            trigger_rows = (
                await connection.execute(
                    text(
                        "SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid) "
                        "FROM pg_trigger t "
                        "JOIN pg_class c ON c.oid = t.tgrelid "
                        "WHERE NOT t.tgisinternal "
                        "AND c.relname IN ('audit_logs', 'security_audit_events')"
                    )
                )
            ).all()
        trigger_defs = {(row.relname, row.tgname): row.pg_get_triggerdef for row in trigger_rows}
        for definition in (
            trigger_defs[("audit_logs", "trg_audit_logs_immutable")],
            trigger_defs[("security_audit_events", "trg_security_audit_events_immutable")],
        ):
            assert "BEFORE" in definition
            assert "UPDATE" in definition
            assert "DELETE" in definition

        attacks = (
            ("UPDATE audit_logs SET action = 'tampered' WHERE id = :id", audit_log_id),
            ("DELETE FROM audit_logs WHERE id = :id", audit_log_id),
            ("TRUNCATE TABLE audit_logs", None),
            (
                "UPDATE security_audit_events SET action = 'tampered' WHERE id = :id",
                security_event_id,
            ),
            ("DELETE FROM security_audit_events WHERE id = :id", security_event_id),
            ("TRUNCATE TABLE security_audit_events", None),
        )
        for statement, row_id in attacks:
            async with session_factory() as db:
                with pytest.raises(
                    DBAPIError,
                    match="audit evidence is append-only",
                ) as rejected:
                    params = {} if row_id is None else {"id": row_id}
                    await db.execute(text(statement), params)
                    await db.commit()
                assert rejected.value.orig.sqlstate == "55000"
                await db.rollback()

        async with session_factory() as db:
            with pytest.raises(DBAPIError) as principal_delete_rejected:
                await db.execute(
                    text("DELETE FROM external_principals WHERE id = :id"),
                    {"id": external_principal_id},
                )
                await db.commit()
            assert principal_delete_rejected.value.orig.sqlstate == "23503"
            await db.rollback()

        async with session_factory() as db:
            audit_log = await db.get(AuditLog, audit_log_id)
            security_event = await db.get(SecurityAuditEvent, security_event_id)

        assert audit_log is not None
        assert audit_log.action == "audit.proof.created"
        assert audit_log.details == {"proof": "original"}
        assert audit_log.external_principal_id == external_principal_id
        assert security_event is not None
        assert security_event.action == "security.audit.proof.created"
        assert security_event.details == {"proof": "original"}

        async with session_factory() as db:
            rows = list(
                (
                    await db.execute(select(SecurityAuditEvent).where(SecurityAuditEvent.tenant_id == tenant_id))
                ).scalars()
            )
        assert [row.id for row in rows] == [security_event_id]
    finally:
        await engine.dispose()


async def test_schema_readiness_fails_closed_when_an_audit_guard_is_dropped(
    chain_migrated_pg_url: str,
) -> None:
    from app.db_bootstrap import apply_audit_evidence_immutability
    from app.scripts.verify_schema_readiness import inspect_schema_readiness

    engine = create_async_engine(chain_migrated_pg_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert (await inspect_schema_readiness(connection)).ready is True

        try:
            async with engine.begin() as connection:
                await connection.execute(text("DROP TRIGGER trg_audit_logs_immutable ON audit_logs"))

            async with engine.connect() as connection:
                drifted = await inspect_schema_readiness(connection)
            issues = {(issue.code, issue.object_name) for issue in drifted.issues}
            assert ("schema_trigger_missing", "trg_audit_logs_immutable") in issues
        finally:
            async with engine.begin() as connection:
                await connection.run_sync(apply_audit_evidence_immutability)

        async with engine.connect() as connection:
            restored = await inspect_schema_readiness(connection)
        assert restored.ready is True
        assert restored.checked_trigger_count == 4
    finally:
        await engine.dispose()
