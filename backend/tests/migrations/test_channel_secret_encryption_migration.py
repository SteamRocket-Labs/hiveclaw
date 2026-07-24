from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "channel_secret_encryption_0712.py"
MASTER_KEY = "channel-migration-master-key-000001"
CHANNEL_TYPES = (
    "feishu",
    "telegram",
    "discord",
    "dingtalk",
    "microsoft_teams",
    "slack",
    "wecom",
)


def _alembic(database_url: str, command: str, target: str) -> None:
    from tests.integration.conftest import BACKEND_ROOT

    result = subprocess.run(
        [sys.executable, "-m", "alembic", command, target],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url, "SECRETS_MASTER_KEY": MASTER_KEY},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"


def test_channel_secret_migration_and_dry_run_contract_exist() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")
    script = (MIGRATION_PATH.parents[2] / "app" / "scripts" / "migrate_channel_secrets.py").read_text(encoding="utf-8")

    assert 'revision = "channel_secret_encryption_0712"' in source
    assert 'down_revision = "approval_continuation_outbox_0712"' in source
    assert "SECRETS_MASTER_KEY" in source
    assert "CHANNEL_SECRET_PREFIX" in source
    assert "plaintext" in script
    assert "migrate_delivery_target_secret_rows" in script
    assert "inspect_delivery_target_secret_rows" in script
    assert "migrate_channel_ingress_secret_rows" in script
    assert "inspect_channel_ingress_secret_rows" in script
    assert "migrate_channel_ingress_exact_secret_rows" in script
    assert 'parser.add_argument("--apply"' in script
    assert 'parser.add_argument("--confirm"' in script
    assert "--apply requires --confirm" in script


@pytest.mark.asyncio
async def test_real_migration_encrypts_all_channel_rows_and_secure_downgrade_keeps_ciphertext(
    pg_container,
    monkeypatch,
) -> None:
    from tests.integration.conftest import _async_url

    database_name = f"channel_secret_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    # Empty databases take the create_all + stamp fast path. Rewind the new
    # secure migration after bootstrap so the second upgrade really exercises
    # the legacy-row backfill path.
    _alembic(database_url, "upgrade", "head")
    _alembic(database_url, "downgrade", "approval_continuation_outbox_0712")

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        from app.models.tenant import Tenant
        from app.models.user import User
        from tests.migrations.conftest import insert_agent_at_schema_revision

        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory.begin() as session:
            session.add(
                Tenant(
                    id=tenant_id,
                    name="Channel Migration",
                    slug=f"channel-{tenant_id.hex[:8]}",
                )
            )
            session.add(
                User(
                    id=user_id,
                    username=f"channel-{user_id.hex[:8]}",
                    email=f"{user_id.hex[:8]}@channel.test",
                    password_hash="x",
                    display_name="Owner",
                    tenant_id=tenant_id,
                    role="org_admin",
                )
            )
            await session.flush()
            await insert_agent_at_schema_revision(
                session,
                agent_id=agent_id,
                tenant_id=tenant_id,
                name="Channel Agent",
                creator_id=user_id,
                status="idle",
            )
        async with engine.begin() as connection:
            for channel_type in CHANNEL_TYPES:
                await connection.execute(
                    text(
                        "INSERT INTO channel_configs "
                        "(id, agent_id, tenant_id, channel_type, app_id, app_secret, encrypt_key, "
                        "verification_token, is_configured, is_connected, extra_config) "
                        "VALUES (:id, :agent_id, :tenant_id, CAST(:channel_type AS channel_type_enum), "
                        ":app_id, :app_secret, :encrypt_key, :verification_token, true, false, "
                        "CAST(:extra_config AS json))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "agent_id": agent_id,
                        "tenant_id": tenant_id,
                        "channel_type": channel_type,
                        "app_id": f"{channel_type}-app",
                        "app_secret": f"{channel_type}-plain-app-secret",
                        "encrypt_key": f"{channel_type}-plain-encrypt-key",
                        "verification_token": f"{channel_type}-plain-verification-token",
                        "extra_config": '{"bot_secret":"wecom-plain-bot-secret","region":"cn"}',
                    },
                )
            await connection.execute(
                text(
                    "INSERT INTO tenant_channel_configs "
                    "(id, tenant_id, channel_type, app_id, app_secret, encrypt_key, verification_token, extra_config) "
                    "VALUES (:id, :tenant_id, 'feishu', 'tenant-app', 'tenant-plain-app-secret', "
                    "'tenant-plain-encrypt-key', 'tenant-plain-verification-token', "
                    'CAST(\'{"client_secret":"tenant-extra-plain-secret"}\' AS jsonb))'
                ),
                {"id": uuid.uuid4(), "tenant_id": tenant_id},
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
                        "SELECT channel_type::text, app_secret, encrypt_key, verification_token, extra_config "
                        "FROM channel_configs WHERE tenant_id = :tenant_id ORDER BY channel_type::text"
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            tenant_row = (
                await connection.execute(
                    text(
                        "SELECT app_secret, encrypt_key, verification_token, extra_config "
                        "FROM tenant_channel_configs WHERE tenant_id = :tenant_id"
                    ),
                    {"tenant_id": tenant_id},
                )
            ).one()
            lengths = await connection.run_sync(
                lambda sync_connection: {
                    table: {
                        column["name"]: column["type"].length
                        for column in inspect(sync_connection).get_columns(table)
                        if column["name"] in {"app_secret", "encrypt_key", "verification_token"}
                    }
                    for table in ("channel_configs", "tenant_channel_configs")
                }
            )
        assert {row.channel_type for row in rows} == set(CHANNEL_TYPES)
        for row in [*rows, tenant_row]:
            rendered = str(tuple(row))
            assert "plain" not in rendered
            assert row.app_secret.startswith("hive:channel-secret:v1:")
            assert row.encrypt_key.startswith("hive:channel-secret:v1:")
            assert row.verification_token.startswith("hive:channel-secret:v1:")
            assert "hive:channel-secret:v1:" in str(row.extra_config)
        assert lengths == {
            "channel_configs": {"app_secret": 1024, "encrypt_key": 1024, "verification_token": 1024},
            "tenant_channel_configs": {"app_secret": 1024, "encrypt_key": 1024, "verification_token": 1024},
        }

        from app.models.channel_config import ChannelConfig
        from app.models.tenant_channel_config import TenantChannelConfig
        from app.services import secrets_provider
        from app.services.secrets_provider import FernetSecretsProvider

        monkeypatch.setattr(secrets_provider, "_provider", FernetSecretsProvider(MASTER_KEY))
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            configs = (
                (
                    await session.execute(
                        ChannelConfig.__table__.select()
                        .where(ChannelConfig.tenant_id == tenant_id)
                        .order_by(ChannelConfig.channel_type)
                    )
                )
                .mappings()
                .all()
            )
            tenant_config = (
                (
                    await session.execute(
                        TenantChannelConfig.__table__.select().where(TenantChannelConfig.tenant_id == tenant_id)
                    )
                )
                .mappings()
                .one()
            )
        assert {config["channel_type"] for config in configs} == set(CHANNEL_TYPES)
        for config in configs:
            channel_type = config["channel_type"]
            assert config["app_secret"] == f"{channel_type}-plain-app-secret"
            assert config["encrypt_key"] == f"{channel_type}-plain-encrypt-key"
            assert config["verification_token"] == f"{channel_type}-plain-verification-token"
            assert config["extra_config"]["bot_secret"] == "wecom-plain-bot-secret"
        assert tenant_config["app_secret"] == "tenant-plain-app-secret"
        assert tenant_config["extra_config"]["client_secret"] == "tenant-extra-plain-secret"

        from app.services.channel_secret_storage import channel_secret_key_id, migrate_channel_secret_rows

        rotated_provider = FernetSecretsProvider(
            "channel-migration-rotated-key-00001",
            previous_master_keys=[MASTER_KEY],
        )
        async with engine.begin() as connection:
            rotation_report = await connection.run_sync(
                lambda bind: migrate_channel_secret_rows(bind, provider=rotated_provider, apply=True)
            )
        assert rotation_report["totals"]["plaintext"] == 0
        assert rotation_report["totals"]["non_current"] == 0
        assert rotation_report["rewritten_rows"] == len(CHANNEL_TYPES) + 1
        async with engine.connect() as connection:
            rotated_values = (
                (
                    await connection.execute(
                        text("SELECT app_secret FROM channel_configs WHERE tenant_id = :tenant_id"),
                        {"tenant_id": tenant_id},
                    )
                )
                .scalars()
                .all()
            )
        assert rotated_values
        assert {channel_secret_key_id(value) for value in rotated_values} == {rotated_provider.key_id}
    finally:
        await engine.dispose()

    _alembic(database_url, "downgrade", "approval_continuation_outbox_0712")
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            values = (
                (
                    await connection.execute(
                        text("SELECT app_secret FROM channel_configs WHERE tenant_id = :tenant_id"),
                        {"tenant_id": tenant_id},
                    )
                )
                .scalars()
                .all()
            )
        assert values and all(value.startswith("hive:channel-secret:v1:") for value in values)
        assert all("plain" not in value for value in values)
    finally:
        await engine.dispose()
