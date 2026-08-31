from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

from sqlalchemy import String
from sqlalchemy.engine import make_url

from tests.migrations.conftest import BACKEND_ROOT, _async_url


def test_terminal_boundary_model_and_bootstrap_rls_registry_are_complete() -> None:
    from app.database import Base
    from app.db_bootstrap import (
        RLS_FORCED_TENANT_TABLES,
        STRICT_TENANT_RLS_TABLES,
        _RUNTIME_TERMINAL_BOUNDARY_TASK_TYPES,
    )
    from app.models import import_all_models
    from app.models.runtime_task import TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES

    import_all_models()
    table = Base.metadata.tables["runtime_terminal_boundary_outbox"]
    assert {
        "id",
        "tenant_id",
        "runtime_task_id",
        "agent_id",
        "session_id",
        "event_kind",
        "terminal_status",
        "authority_ref",
        "authority_id",
        "binding_json",
        "binding_sha256",
        "idempotency_key",
        "status",
        "attempt_count",
        "available_at",
        "claimed_by",
        "lease_expires_at",
        "last_error",
        "delivery_receipt_json",
        "delivered_at",
    } <= set(table.c.keys())
    assert not table.c.tenant_id.nullable
    assert isinstance(table.c.session_id.type, String)
    assert table.c.session_id.type.length == 200
    assert {foreign_key.parent.name: foreign_key.ondelete for foreign_key in table.foreign_keys} == {
        "tenant_id": "CASCADE",
        "runtime_task_id": "CASCADE",
        "agent_id": "CASCADE",
    }
    assert "summary_through_sequence" in Base.metadata.tables["chat_sessions"].c
    assert "runtime_terminal_boundary_outbox" in STRICT_TENANT_RLS_TABLES
    assert "runtime_terminal_boundary_outbox" in RLS_FORCED_TENANT_TABLES
    assert _RUNTIME_TERMINAL_BOUNDARY_TASK_TYPES == TERMINAL_BOUNDARY_REQUIRED_TASK_TYPES
    assert Base.metadata.tables["runtime_tasks"].c.terminal_boundary_generation.server_default is None


def test_terminal_boundary_revision_has_cutover_backfill_and_reversible_schema() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "runtime_terminal_boundary_0831.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "runtime_terminal_boundary_0831"' in migration
    assert 'down_revision = "a2a_continuation_task_0828"' in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "status IN ('pending', 'running', 'resumable', 'suspended')" in migration
    assert "set_runtime_task_terminal_boundary_generation" in migration
    assert migration.count('f"AND task_type IN ({_TERMINAL_TASK_TYPES}) "') == 2
    for task_type in (
        "web_chat_turn",
        "goal_continuation",
        "team_member",
        "advanced_plan",
        "a2a_continuation",
        "business_task",
        "trigger",
        "delegation",
    ):
        assert f"'{task_type}'" in migration
    assert "NEW.terminal_boundary_generation := NULL;" in migration
    assert "server_default=None" in migration
    assert 'op.drop_table("runtime_terminal_boundary_outbox")' in migration
    assert 'op.drop_column("runtime_tasks", column)' in migration


def test_terminal_boundary_revision_real_pg_roundtrip_and_forced_rls(pg_container) -> None:
    database = f"terminalboundary{uuid.uuid4().hex[:12]}"
    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    active_web_id = uuid.uuid4()
    active_workflow_id = uuid.uuid4()
    historical_web_id = uuid.uuid4()
    bootstrap_trigger_id = uuid.uuid4()
    bootstrap_dream_id = uuid.uuid4()
    rolling_trigger_id = uuid.uuid4()
    rolling_dream_id = uuid.uuid4()
    code, output = pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f'CREATE DATABASE "{database}"'])
    assert code == 0, output
    database_url = make_url(_async_url(pg_container)).set(database=database).render_as_string(hide_password=False)
    env = {**os.environ, "DATABASE_URL": database_url}

    def alembic(*arguments: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"

    try:
        # Empty databases use create_all + stamp-head, so prove the bootstrap
        # path installs the same task-type-aware writer guard as the revision.
        alembic("upgrade", "head")
        bootstrap_sql = f"""
          INSERT INTO tenants(
            id,name,slug,im_provider,is_active,min_heartbeat_interval_minutes,timezone,
            default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,
            tokens_used_today,tokens_used_month,tokens_used_total,sync_version
          ) VALUES (
            '{tenant_id}','Terminal boundary bootstrap','tb-{tenant_id.hex}','web_only',true,45,'UTC',20,5,5,0,0,0,1
          );
          INSERT INTO runtime_tasks(
            id,task_type,parent_agent_id,tenant_id,status,delegation_chain_json,depth,priority,
            attempt_count,claim_version,root_idempotency_key,config_snapshot_hash,policy_snapshot_hash
          ) VALUES
            ('{bootstrap_trigger_id}','trigger','{parent_agent_id}','{tenant_id}','pending','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{bootstrap_trigger_id}',repeat('a',64),repeat('b',64)),
            ('{bootstrap_dream_id}','dream','{parent_agent_id}','{tenant_id}','pending','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{bootstrap_dream_id}',repeat('a',64),repeat('b',64));
          SELECT
            (SELECT count(*) FROM pg_trigger
              WHERE tgname='trg_runtime_task_terminal_boundary_generation'),
            (SELECT count(*) FROM pg_proc
              WHERE proname='set_runtime_task_terminal_boundary_generation'),
            (SELECT terminal_boundary_generation FROM runtime_tasks WHERE id='{bootstrap_trigger_id}'),
            COALESCE(
              (SELECT terminal_boundary_generation::text FROM runtime_tasks WHERE id='{bootstrap_dream_id}'),
              'null'
            );
        """
        code, output = pg_container.exec(["psql", "-U", "test", "-d", database, "-qAt", "-F", "|", "-c", bootstrap_sql])
        assert code == 0, output
        evidence = (output.decode() if isinstance(output, bytes) else str(output)).strip()
        assert evidence == "1|1|1|null"
        cleanup_sql = (
            f"DELETE FROM runtime_tasks WHERE id IN ('{bootstrap_trigger_id}', '{bootstrap_dream_id}'); "
            f"DELETE FROM tenants WHERE id='{tenant_id}'"
        )
        code, output = pg_container.exec(["psql", "-U", "test", "-d", database, "-c", cleanup_sql])
        assert code == 0, output

        # The custom bootstrap intentionally stamps head on an empty database.
        # Project the exact revision parent before exercising the real upgrade.
        alembic("downgrade", "a2a_continuation_task_0828")
        seed_sql = f"""
          INSERT INTO tenants(
            id,name,slug,im_provider,is_active,min_heartbeat_interval_minutes,timezone,
            default_max_triggers,min_poll_interval_floor,max_webhook_rate_ceiling,
            tokens_used_today,tokens_used_month,tokens_used_total,sync_version
          ) VALUES (
            '{tenant_id}','Terminal boundary migration','tb-{tenant_id.hex}','web_only',true,45,'UTC',20,5,5,0,0,0,1
          );
          INSERT INTO runtime_tasks(
            id,task_type,parent_agent_id,tenant_id,status,delegation_chain_json,depth,priority,
            attempt_count,claim_version,root_idempotency_key,config_snapshot_hash,policy_snapshot_hash
          ) VALUES
            ('{active_web_id}','web_chat_turn','{parent_agent_id}','{tenant_id}','running','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{active_web_id}',repeat('a',64),repeat('b',64)),
            ('{active_workflow_id}','workflow','{parent_agent_id}','{tenant_id}','running','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{active_workflow_id}',repeat('a',64),repeat('b',64)),
            ('{historical_web_id}','web_chat_turn','{parent_agent_id}','{tenant_id}','completed','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{historical_web_id}',repeat('a',64),repeat('b',64));
        """
        code, output = pg_container.exec(["psql", "-U", "test", "-d", database, "-c", seed_sql])
        assert code == 0, output

        alembic("upgrade", "head")
        query = """
          SELECT
            c.relrowsecurity,
            c.relforcerowsecurity,
            (SELECT count(*) FROM pg_policies
              WHERE schemaname='public'
                AND tablename='runtime_terminal_boundary_outbox'
                AND policyname='tenant_isolation_runtime_terminal_boundary_outbox'),
            (SELECT count(*) FROM information_schema.columns
              WHERE table_schema='public' AND table_name='runtime_tasks'
                AND column_name LIKE 'terminal_boundary_%'),
            (SELECT count(*) FROM information_schema.columns
              WHERE table_schema='public' AND table_name='chat_sessions'
                AND column_name='summary_through_sequence'),
            (SELECT data_type || ':' || character_maximum_length::text
              FROM information_schema.columns
              WHERE table_schema='public' AND table_name='runtime_terminal_boundary_outbox'
                AND column_name='session_id'),
            (SELECT count(*) FROM pg_constraint
              WHERE conrelid='runtime_terminal_boundary_outbox'::regclass
                AND contype='f' AND confdeltype='c')
          FROM pg_class AS c
          JOIN pg_namespace AS n ON n.oid=c.relnamespace
          WHERE n.nspname='public' AND c.relname='runtime_terminal_boundary_outbox'
        """
        code, output = pg_container.exec(["psql", "-U", "test", "-d", database, "-At", "-F", "|", "-c", query])
        assert code == 0, output
        evidence = (output.decode() if isinstance(output, bytes) else str(output)).strip()
        assert evidence == "t|t|1|5|1|character varying:200|3"

        generation_query = f"""
          INSERT INTO runtime_tasks(
            id,task_type,parent_agent_id,tenant_id,status,delegation_chain_json,depth,priority,
            attempt_count,claim_version,root_idempotency_key,config_snapshot_hash,policy_snapshot_hash
          ) VALUES
            ('{rolling_trigger_id}','trigger','{parent_agent_id}','{tenant_id}','pending','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{rolling_trigger_id}',repeat('a',64),repeat('b',64)),
            ('{rolling_dream_id}','dream','{parent_agent_id}','{tenant_id}','pending','[]'::jsonb,1,0,0,0,
             'terminal-boundary:{rolling_dream_id}',repeat('a',64),repeat('b',64));
          SELECT id, COALESCE(terminal_boundary_generation::text, 'null')
          FROM runtime_tasks
          WHERE id IN (
            '{active_web_id}', '{active_workflow_id}', '{historical_web_id}',
            '{rolling_trigger_id}', '{rolling_dream_id}'
          )
          ORDER BY id;
        """
        code, output = pg_container.exec(
            ["psql", "-U", "test", "-d", database, "-qAt", "-F", "|", "-c", generation_query]
        )
        assert code == 0, output
        rows = dict(
            line.split("|", 1)
            for line in (output.decode() if isinstance(output, bytes) else str(output)).strip().splitlines()
        )
        assert rows == {
            str(active_web_id): "1",
            str(active_workflow_id): "null",
            str(historical_web_id): "null",
            str(rolling_trigger_id): "1",
            str(rolling_dream_id): "null",
        }

        alembic("downgrade", "a2a_continuation_task_0828")
        rollback_query = """
          SELECT
            to_regclass('public.runtime_terminal_boundary_outbox') IS NULL,
            (SELECT count(*) FROM information_schema.columns
              WHERE table_schema='public' AND table_name='runtime_tasks'
                AND column_name LIKE 'terminal_boundary_%'),
            (SELECT count(*) FROM information_schema.columns
              WHERE table_schema='public' AND table_name='chat_sessions'
                AND column_name='summary_through_sequence')
        """
        code, output = pg_container.exec(["psql", "-U", "test", "-d", database, "-At", "-F", "|", "-c", rollback_query])
        assert code == 0, output
        evidence = (output.decode() if isinstance(output, bytes) else str(output)).strip()
        assert evidence == "t|0|0"

        alembic("upgrade", "head")
    finally:
        pg_container.exec(["psql", "-U", "test", "-d", "postgres", "-c", f'DROP DATABASE IF EXISTS "{database}"'])
