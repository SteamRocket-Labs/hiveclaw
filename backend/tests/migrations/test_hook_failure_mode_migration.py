from __future__ import annotations

import ast
import runpy
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "hook_failure_modes_0712.py"


def test_hook_failure_mode_migration_is_reversible_and_preserves_legacy_preview() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    assert {"upgrade", "downgrade"} <= functions
    assert '"failure_policy", "inherit"' in source
    assert '"legacy_failure_policy"' in source
    assert '"continue"' in source
    assert '"migration_preview"' in source


async def test_hook_failure_mode_migration_transforms_and_rolls_back_real_postgres(
    revision_parent_migrated_pg_url: str,
) -> None:
    namespace = runpy.run_path(str(MIGRATION))
    engine = create_async_engine(revision_parent_migrated_pg_url, poolclass=NullPool)
    key = "agent:00000000-0000-4000-8000-000000000005:hook_runtime"
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM system_settings WHERE key = :key"), {"key": key})
            await connection.execute(
                text("INSERT INTO system_settings (key, value) VALUES (:key, CAST(:value AS jsonb))"),
                {
                    "key": key,
                    "value": '{"hooks":{"hook.stop":{"enabled":true,"failure_policy":"continue"}}}',
                },
            )
            await connection.execute(text(namespace["_UPGRADE_SQL"]))
            upgraded = (
                await connection.execute(text("SELECT value FROM system_settings WHERE key = :key"), {"key": key})
            ).scalar_one()
            assert upgraded["hooks"]["hook.stop"]["failure_policy"] == "inherit"
            assert upgraded["hooks"]["hook.stop"]["legacy_failure_policy"] == "continue"
            assert upgraded["hooks"]["hook.stop"]["migration_preview"] == {"effective_change": "registration_default"}

            await connection.execute(text(namespace["_DOWNGRADE_SQL"]))
            downgraded = (
                await connection.execute(text("SELECT value FROM system_settings WHERE key = :key"), {"key": key})
            ).scalar_one()
            assert downgraded["hooks"]["hook.stop"]["failure_policy"] == "continue"
            assert "legacy_failure_policy" not in downgraded["hooks"]["hook.stop"]
            await connection.execute(text("DELETE FROM system_settings WHERE key = :key"), {"key": key})
    finally:
        await engine.dispose()
