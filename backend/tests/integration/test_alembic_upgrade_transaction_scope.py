"""Native migration plans retain upgrade commits and preflight refused downgrades."""

from __future__ import annotations

import asyncio
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import BACKEND_ROOT, _async_url


async def test_programmatic_migrations_preserve_upgrade_commits_and_preflight_refusals(
    pg_container, tmp_path, monkeypatch
) -> None:
    from app.config import get_settings

    settings = get_settings()

    database_name = f"upgrade_scope_{uuid.uuid4().hex[:12]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    engine = create_async_engine(database_url, poolclass=NullPool)
    project = tmp_path / "migration_scope"
    versions = project / "versions"
    versions.mkdir(parents=True)
    (project / "env.py").write_text((BACKEND_ROOT / "alembic" / "env.py").read_text())
    (versions / "base.py").write_text(
        'revision = "upgrade_scope_base"\ndown_revision = None\ndef upgrade():\n    pass\ndef downgrade():\n    pass\n'
    )
    (versions / "first.py").write_text(
        "from alembic import op\nimport sqlalchemy as sa\n"
        'revision = "upgrade_scope_first"\ndown_revision = "upgrade_scope_base"\n'
        "def upgrade():\n"
        '    op.create_table("upgrade_scope_witness", sa.Column("id", sa.Integer(), primary_key=True))\n'
        '    op.execute("INSERT INTO upgrade_scope_witness (id) VALUES (1)")\n'
        'def downgrade_preflight():\n    raise RuntimeError("downgrade_scope_probe_guard")\n'
        'def downgrade():\n    op.drop_table("upgrade_scope_witness")\n'
    )
    (versions / "rejected.py").write_text(
        'revision = "upgrade_scope_rejected"\ndown_revision = "upgrade_scope_first"\n'
        'def upgrade():\n    raise RuntimeError("upgrade_scope_probe_failed")\n'
        "def downgrade():\n    pass\n"
    )
    config = Config()
    config.set_main_option("script_location", str(project))
    monkeypatch.setattr(settings, "DATABASE_URL", database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('upgrade_scope_base')"))
        with pytest.raises(RuntimeError, match="upgrade_scope_probe_failed"):
            await asyncio.to_thread(command.upgrade, config, "head")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "upgrade_scope_first"
            assert await connection.scalar(text("SELECT id FROM upgrade_scope_witness")) == 1
        with pytest.raises(RuntimeError, match="downgrade_scope_probe_guard"):
            await asyncio.to_thread(command.downgrade, config, "base")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "upgrade_scope_first"
            assert await connection.scalar(text("SELECT id FROM upgrade_scope_witness")) == 1
        # Explicit stamping changes version metadata without running downgrade DDL.
        await asyncio.to_thread(command.stamp, config, "upgrade_scope_base")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "upgrade_scope_base"
            assert await connection.scalar(text("SELECT id FROM upgrade_scope_witness")) == 1
    finally:
        await engine.dispose()
