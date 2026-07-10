import importlib.util
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "ai_asset_control_plane_0710.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ai_asset_control_plane_0710", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ai_asset_migration_is_single_head_and_backfills_all_db_native_asset_types() -> None:
    module = _load_module()
    source = _PATH.read_text(encoding="utf-8")

    assert module.revision == "ai_asset_control_plane_0710"
    assert module.down_revision == "runtime_assembly_nested_0710"
    assert "ai_asset_records" in source
    for asset_type, table in (
        ("agent", "agents"),
        ("skill", "skills"),
        ("workflow", "workflow_definitions"),
        ("external_capability", "external_capability_snapshots"),
    ):
        assert asset_type in source
        assert table in source
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "FORCE ROW LEVEL SECURITY" in source
    assert "quarantine_reason" in source
    assert source.count('"control"') >= 4
    assert '"kind": "registry"' in source
    assert "enforce_config_revision_immutability" in source
    assert "trg_config_revision_immutability" in source


async def _assert_revision_trigger(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tgname FROM pg_trigger "
                            "WHERE tgrelid = 'config_revisions'::regclass AND NOT tgisinternal"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()
    assert "trg_config_revision_immutability" in names


async def test_ai_asset_upgrade_path_installs_revision_trigger(chain_migrated_pg_url: str) -> None:
    await _assert_revision_trigger(chain_migrated_pg_url)


async def test_ai_asset_bootstrap_path_installs_revision_trigger(migrated_pg_url: str) -> None:
    await _assert_revision_trigger(migrated_pg_url)
