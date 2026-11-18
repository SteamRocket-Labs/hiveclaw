from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.database import _engine_connect_args


async def test_prepared_statements_are_reused_on_one_connection(
    migrated_pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DB_PREPARED_STATEMENT_CACHE_SIZE", raising=False)
    settings = Settings(_env_file=None)
    engine = create_async_engine(
        migrated_pg_url,
        connect_args=_engine_connect_args(settings),
        pool_size=1,
        max_overflow=0,
    )
    statements = [f"SELECT {value}::int AS v" for value in range(300)]

    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.exec_driver_sql(statement)

            driver_connection = (await connection.get_raw_connection()).driver_connection
            prepare = type(driver_connection).prepare
            prepare_calls = 0

            async def count_prepare(raw_connection, *args, **kwargs):
                nonlocal prepare_calls
                if raw_connection is driver_connection:
                    prepare_calls += 1
                return await prepare(raw_connection, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(type(driver_connection), "prepare", count_prepare)
                for statement in statements:
                    await connection.exec_driver_sql(statement)

            assert prepare_calls == 0, f"Second pass made {prepare_calls} asyncpg prepare calls"
    finally:
        await engine.dispose()
