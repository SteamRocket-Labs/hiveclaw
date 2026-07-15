from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "personal_kb_authority_0715.py"


def _alembic(database_url: str, command: str, target: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout={result.stdout[-3000:]} stderr={result.stderr[-3000:]}"


def test_personal_kb_authority_migration_declares_reversible_fail_closed_contract() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "personal_kb_authority_0715"' in source
    assert 'down_revision = "personal_kb_sensitivity_canonical_0715"' in source
    for field in (
        "requester_user_id",
        "session_id",
        "purpose",
        "delegation_id",
        "sensitivity_ceiling",
        "binding_key",
        "revoked_at",
        "revoked_by_user_id",
    ):
        assert field in source
    assert "legacy_authority_unverifiable" in source
    assert "ck_knowledge_grant_resource_binding" in source
    assert "autonomous_agent" in source
    assert "a2a_delegation" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "def downgrade()" in source


async def test_personal_kb_authority_real_postgres_quarantines_legacy_and_roundtrips(pg_container) -> None:
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"kb_authority_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    _alembic_upgrade(database_url, "head")
    _alembic(database_url, "downgrade", "personal_kb_sensitivity_canonical_0715")

    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    grant_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET session_replication_role = replica"))
            await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_grants (
                        id, tenant_id, scope_type, scope_id, resource_type, resource_id,
                        grantee_type, grantee_id, permission, grant_metadata_json,
                        created_by_user_id
                    ) VALUES (
                        :id, :tenant_id, 'person', :owner_id, 'scope', :owner_id,
                        'agent', :agent_id, 'search', CAST(:metadata AS jsonb), :owner_id
                    )
                    """
                ),
                {
                    "id": grant_id,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "agent_id": agent_id,
                    "metadata": json.dumps({"keep": "legacy"}),
                },
            )
            await connection.execute(text("SET session_replication_role = DEFAULT"))
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "head")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT requester_user_id, session_id, purpose, delegation_id,
                               sensitivity_ceiling, binding_key, revoked_at,
                               revoked_by_user_id, grant_metadata_json
                        FROM knowledge_grants WHERE id = :grant_id
                        """
                    ),
                    {"grant_id": grant_id},
                )
            ).one()
            constraints = (
                await connection.execute(
                    text(
                        """
                        SELECT conname, convalidated FROM pg_constraint
                        WHERE conname IN (
                            'ck_knowledge_grant_sensitivity_ceiling',
                            'ck_knowledge_grant_agent_binding',
                            'ck_knowledge_grant_resource_binding',
                            'ck_knowledge_grant_revoke_actor'
                        ) ORDER BY conname
                        """
                    )
                )
            ).all()
            rls = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'knowledge_grants'")
                )
            ).one()

        assert row.requester_user_id is None
        assert row.session_id is None
        assert row.purpose == "legacy_quarantined"
        assert row.delegation_id is None
        assert row.sensitivity_ceiling == "PL1_public"
        assert row.binding_key == f"legacy:{grant_id}"
        assert row.revoked_at is not None
        assert row.revoked_by_user_id is None
        assert row.grant_metadata_json["keep"] == "legacy"
        assert row.grant_metadata_json["authority_status"] == "legacy_authority_unverifiable"
        assert len(constraints) == 4
        assert all(item.convalidated for item in constraints)
        assert tuple(rls) == (True, True)
    finally:
        await engine.dispose()

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL session_replication_role = replica"))
                await connection.execute(
                    text(
                        """
                        INSERT INTO knowledge_grants (
                            id, tenant_id, scope_type, scope_id, resource_type, resource_id,
                            grantee_type, grantee_id, permission, requester_user_id,
                            purpose, sensitivity_ceiling, binding_key, expires_at,
                            grant_metadata_json, created_by_user_id
                        ) VALUES (
                            :id, :tenant_id, 'person', :owner_id, 'scope', :owner_id,
                            'agent', :agent_id, 'read', :owner_id,
                            'interactive_session', 'PL3_sensitive', :binding_key, now() + interval '1 hour',
                            '{}'::jsonb, :owner_id
                        )
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "owner_id": owner_id,
                        "agent_id": agent_id,
                        "binding_key": f"pkb:invalid:{uuid.uuid4()}",
                    },
                )
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "personal_kb_sensitivity_canonical_0715")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            downgraded = (
                await connection.execute(
                    text("SELECT grant_metadata_json, expires_at FROM knowledge_grants WHERE id = :grant_id"),
                    {"grant_id": grant_id},
                )
            ).one()
            columns = {
                row.column_name
                for row in (
                    await connection.execute(
                        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'knowledge_grants'")
                    )
                ).all()
            }
        assert downgraded.grant_metadata_json["keep"] == "legacy"
        assert downgraded.grant_metadata_json["authority_status"] == "downgrade_quarantined"
        assert downgraded.expires_at is not None
        assert "sensitivity_ceiling" not in columns
        assert "revoked_at" not in columns
        assert "binding_key" not in columns
    finally:
        await engine.dispose()
