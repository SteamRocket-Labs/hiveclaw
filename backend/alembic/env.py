"""Alembic environment configuration for async SQLAlchemy."""

import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.database import Base
from app.db_bootstrap import run_migrations_with_bootstrap
from app.config import get_settings

from app.models import import_all_models

# Import all model modules so Base.metadata is fully populated for both normal
# migrations and create_all/bootstrap. This is the same registry used by
# app.main startup, preventing drift between deployment paths.
import_all_models()

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    script = ScriptDirectory.from_config(config)
    context.configure(connection=connection, target_metadata=target_metadata)
    migration_fn = context.get_context().opts["fn"]

    def preflight_migrations(revisions, migration_context):
        steps = list(migration_fn(revisions, migration_context))
        # Concurrent index operations commit independently. Check migration-owned
        # retention guards before any revision can change schema or version.
        for step in steps:
            if not step.is_upgrade and not step.info.is_stamp:
                preflight = getattr(step.revision.module, "downgrade_preflight", None)
                if preflight is not None:
                    preflight()
        return steps

    context.configure(connection=connection, target_metadata=target_metadata, fn=preflight_migrations)
    run_migrations_with_bootstrap(
        connection,
        alembic_context=context,
        metadata=target_metadata,
        heads=script.get_heads(),
    )


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
