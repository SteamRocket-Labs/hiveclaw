"""Regression contract for the RuntimeAssembly single-source migration."""

import importlib.util
import json
from pathlib import Path
import uuid

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


_PATH = Path(__file__).parents[2] / "alembic" / "versions" / "runtime_assembly_nested_0710.py"
_SPEC = importlib.util.spec_from_file_location("runtime_assembly_nested_0710", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_promote_runtime_assembly = _MODULE._promote_runtime_assembly


def test_promote_runtime_assembly_moves_legacy_mirrors_and_preserves_nested_authority():
    metadata = {
        "source": "web",
        "tool_result_ledger": [{"legacy": True}],
        "activation_events": [{"event_type": "legacy"}],
        "runtime_assembly_state": {
            "tool_result_ledger": [{"canonical": True}],
            "skill_catalog_ranking": [{"skill": "existing"}],
        },
    }

    promoted, changed = _promote_runtime_assembly(metadata)

    assert changed is True
    assert promoted["source"] == "web"
    assert "tool_result_ledger" not in promoted
    assert "activation_events" not in promoted
    assert promoted["runtime_assembly_state"] == {
        "schema": "hive.ccplus.runtime_assembly_state.v1",
        "tool_result_ledger": [{"canonical": True}],
        "activation_events": [{"event_type": "legacy"}],
        "skill_catalog_ranking": [{"skill": "existing"}],
    }


def test_promote_runtime_assembly_is_idempotent_and_does_not_create_empty_state():
    canonical = {
        "source": "channel",
        "runtime_assembly_state": {
            "schema": "hive.ccplus.runtime_assembly_state.v1",
            "activation_candidates": [{"candidate_id": "one"}],
        },
    }

    promoted, changed = _promote_runtime_assembly(canonical)
    untouched, untouched_changed = _promote_runtime_assembly({"source": "channel"})

    assert changed is False
    assert promoted == canonical
    assert untouched_changed is False
    assert untouched == {"source": "channel"}


def test_runtime_assembly_upgrade_is_set_based_and_filters_legacy_rows_in_sql():
    source = _PATH.read_text(encoding="utf-8")

    assert "for row in rows.mappings()" not in source
    assert "jsonb_object_agg" in source
    assert "?| CAST(:assembly_keys AS text[])" in source
    assert "autocommit_block" in source


async def test_runtime_assembly_set_based_upgrade_preserves_nested_authority(pg_container):
    from tests.integration.conftest import _async_url
    from tests.migrations.conftest import _alembic_upgrade

    database_name = f"assembly_nested_{uuid.uuid4().hex[:10]}"
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f"CREATE DATABASE {database_name}"])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database_name).render_as_string(hide_password=False)
    engine = create_async_engine(database_url, poolclass=NullPool)
    task_id = uuid.uuid4()
    session_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO alembic_version VALUES ('approval_ticket_governance_0710')"))
            await connection.execute(text("CREATE TABLE runtime_tasks (id UUID PRIMARY KEY, metadata_json JSON)"))
            await connection.execute(
                text("CREATE TABLE chat_sessions (id UUID PRIMARY KEY, transcript_metadata_json JSONB)")
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO runtime_tasks (id, metadata_json)
                    VALUES (:id, CAST(:metadata AS json))
                    """
                ),
                {
                    "id": task_id,
                    "metadata": json.dumps(
                        {
                            "source": "web",
                            "tool_result_ledger": [{"legacy": True}],
                            "activation_events": [{"event_type": "legacy"}],
                            "runtime_assembly_state": {
                                "tool_result_ledger": [{"canonical": True}],
                                "skill_catalog_ranking": [{"skill": "existing"}],
                            },
                        }
                    ),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO chat_sessions (id, transcript_metadata_json)
                    VALUES (:id, CAST(:metadata AS jsonb))
                    """
                ),
                {
                    "id": session_id,
                    "metadata": json.dumps({"source": "channel", "activation_candidates": [{"id": "one"}]}),
                },
            )
    finally:
        await engine.dispose()

    _alembic_upgrade(database_url, "runtime_assembly_nested_0710")

    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            task_metadata = await connection.scalar(
                text("SELECT metadata_json FROM runtime_tasks WHERE id = :id"), {"id": task_id}
            )
            session_metadata = await connection.scalar(
                text("SELECT transcript_metadata_json FROM chat_sessions WHERE id = :id"), {"id": session_id}
            )
        assert task_metadata == {
            "source": "web",
            "runtime_assembly_state": {
                "schema": "hive.ccplus.runtime_assembly_state.v1",
                "tool_result_ledger": [{"canonical": True}],
                "activation_events": [{"event_type": "legacy"}],
                "skill_catalog_ranking": [{"skill": "existing"}],
            },
        }
        assert session_metadata == {
            "source": "channel",
            "runtime_assembly_state": {
                "schema": "hive.ccplus.runtime_assembly_state.v1",
                "activation_candidates": [{"id": "one"}],
            },
        }
    finally:
        await engine.dispose()
