from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "agent_model_tenant_authority_0715.py"
PARENT_REVISION = "agent_tool_config_encryption_0715"
PRIMARY_FK = "fk_agents_primary_model_tenant"
FALLBACK_FK = "fk_agents_fallback_model_tenant"
MODEL_UNIQUE = "uq_llm_models_tenant_id_id"
TEMPLATE_FK = "fk_agent_templates_model_tenant"


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


def test_agent_model_tenant_authority_migration_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "agent_model_tenant_authority_0715"' in source
    assert f'down_revision = "{PARENT_REVISION}"' in source
    assert "migration.agent_model_reference_quarantined" in source
    assert "IS DISTINCT FROM" in source
    assert "NOT VALID" in source
    assert "VALIDATE CONSTRAINT" in source
    assert PRIMARY_FK in source
    assert FALLBACK_FK in source
    assert MODEL_UNIQUE in source
    assert TEMPLATE_FK in source
    assert "migration.agent_template_model_reference_quarantined" in source
    assert "secure downgrade" in source.lower()


@pytest.mark.asyncio
async def test_real_migration_quarantines_legacy_refs_and_enforces_tenant_pair(pg_container) -> None:
    from app.models.agent import AgentTemplate
    from app.models.llm import LLMModel
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import insert_agent_at_schema_revision

    database_name = f"agent_model_tenant_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)

    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", PARENT_REVISION)

    # The secure downgrade deliberately preserves the authority constraints.
    # Remove them only inside this isolated fixture to recreate the historical
    # parent schema and inject a representative legacy cross-tenant reference.
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"ALTER TABLE agents DROP CONSTRAINT IF EXISTS {PRIMARY_FK}"))
            await connection.execute(text(f"ALTER TABLE agents DROP CONSTRAINT IF EXISTS {FALLBACK_FK}"))
            await connection.execute(text(f"ALTER TABLE agent_templates DROP CONSTRAINT IF EXISTS {TEMPLATE_FK}"))
            await connection.execute(text(f"ALTER TABLE llm_models DROP CONSTRAINT IF EXISTS {MODEL_UNIQUE}"))

        tenant_a = uuid.uuid4()
        tenant_b = uuid.uuid4()
        user_a = uuid.uuid4()
        agent_id = uuid.uuid4()
        template_id = uuid.uuid4()
        local_model_id = uuid.uuid4()
        foreign_model_id = uuid.uuid4()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions.begin() as session:
            session.add_all(
                [
                    Tenant(id=tenant_a, name="Tenant A", slug=f"tenant-a-{tenant_a.hex[:8]}"),
                    Tenant(id=tenant_b, name="Tenant B", slug=f"tenant-b-{tenant_b.hex[:8]}"),
                ]
            )
            await session.flush()
            session.add(
                User(
                    id=user_a,
                    username=f"model-owner-{user_a.hex[:8]}",
                    email=f"{user_a.hex[:8]}@model-authority.test",
                    password_hash="x",
                    display_name="Model Owner",
                    tenant_id=tenant_a,
                    role="org_admin",
                )
            )
            session.add_all(
                [
                    LLMModel(
                        id=local_model_id,
                        tenant_id=tenant_a,
                        provider="openai",
                        model="same-tenant-model",
                        api_key_encrypted="encrypted",
                        label="Same tenant",
                        enabled=True,
                    ),
                    LLMModel(
                        id=foreign_model_id,
                        tenant_id=tenant_b,
                        provider="openai",
                        model="foreign-tenant-model",
                        api_key_encrypted="encrypted",
                        label="Foreign tenant",
                        enabled=True,
                    ),
                ]
            )
            await session.flush()
            session.add(
                AgentTemplate(
                    id=template_id,
                    tenant_id=tenant_a,
                    name="Legacy cross-tenant template",
                    model_id=foreign_model_id,
                    created_by=user_a,
                )
            )
            await insert_agent_at_schema_revision(
                session,
                agent_id=agent_id,
                tenant_id=tenant_a,
                creator_id=user_a,
                name="Legacy cross-tenant agent",
                status="idle",
                primary_model_id=foreign_model_id,
                fallback_model_id=local_model_id,
            )
    finally:
        await engine.dispose()

    _alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            refs = (
                await connection.execute(
                    text("SELECT primary_model_id, fallback_model_id FROM agents WHERE id = :agent_id"),
                    {"agent_id": agent_id},
                )
            ).one()
            audit = (
                await connection.execute(
                    text(
                        "SELECT action, details FROM audit_logs "
                        "WHERE agent_id = :agent_id AND action = 'migration.agent_model_reference_quarantined'"
                    ),
                    {"agent_id": agent_id},
                )
            ).one()
            template_model_id = (
                await connection.execute(
                    text("SELECT model_id FROM agent_templates WHERE id = :template_id"),
                    {"template_id": template_id},
                )
            ).scalar_one()
            template_audit = (
                await connection.execute(
                    text(
                        "SELECT action, details FROM audit_logs "
                        "WHERE action = 'migration.agent_template_model_reference_quarantined' "
                        "AND details ->> 'template_id' = :template_id"
                    ),
                    {"template_id": str(template_id)},
                )
            ).one()
            constraints = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT conname, convalidated FROM pg_constraint "
                            "WHERE conname IN (:primary_fk, :fallback_fk, :template_fk, :model_unique)"
                        ),
                        {
                            "primary_fk": PRIMARY_FK,
                            "fallback_fk": FALLBACK_FK,
                            "template_fk": TEMPLATE_FK,
                            "model_unique": MODEL_UNIQUE,
                        },
                    )
                ).all()
            )

        assert refs.primary_model_id is None
        assert refs.fallback_model_id == local_model_id
        assert audit.action == "migration.agent_model_reference_quarantined"
        assert audit.details["field"] == "primary_model_id"
        assert audit.details["model_id"] == str(foreign_model_id)
        assert audit.details["reason"] == "cross_tenant_model"
        assert template_model_id is None
        assert template_audit.details["template_id"] == str(template_id)
        assert template_audit.details["model_id"] == str(foreign_model_id)
        assert constraints == {PRIMARY_FK: True, FALLBACK_FK: True, TEMPLATE_FK: True, MODEL_UNIQUE: True}

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text("UPDATE agents SET primary_model_id = :model_id WHERE id = :agent_id"),
                    {"agent_id": agent_id, "model_id": foreign_model_id},
                )
            await transaction.rollback()

        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE agents SET primary_model_id = :model_id WHERE id = :agent_id"),
                {"agent_id": agent_id, "model_id": local_model_id},
            )

        async with engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text("UPDATE agent_templates SET model_id = :model_id WHERE id = :template_id"),
                    {"template_id": template_id, "model_id": foreign_model_id},
                )
            await transaction.rollback()
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", PARENT_REVISION)
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            preserved_constraints = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT conname, convalidated FROM pg_constraint "
                            "WHERE conname IN (:primary_fk, :fallback_fk, :template_fk, :model_unique)"
                        ),
                        {
                            "primary_fk": PRIMARY_FK,
                            "fallback_fk": FALLBACK_FK,
                            "template_fk": TEMPLATE_FK,
                            "model_unique": MODEL_UNIQUE,
                        },
                    )
                ).all()
            )
            preserved_audit_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE agent_id = :agent_id AND action = 'migration.agent_model_reference_quarantined'"
                    ),
                    {"agent_id": agent_id},
                )
            ).scalar_one()
            preserved_template_audit_count = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE action = 'migration.agent_template_model_reference_quarantined' "
                        "AND details ->> 'template_id' = :template_id"
                    ),
                    {"template_id": str(template_id)},
                )
            ).scalar_one()
        assert preserved_constraints == {
            PRIMARY_FK: True,
            FALLBACK_FK: True,
            TEMPLATE_FK: True,
            MODEL_UNIQUE: True,
        }
        assert preserved_audit_count == 1
        assert preserved_template_audit_count == 1
    finally:
        await engine.dispose()
