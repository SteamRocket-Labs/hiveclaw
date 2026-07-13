from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.integration.conftest import (  # noqa: F401, E402
    migrated_pg_url,
    owner_engine,
    owner_sessionmaker,
    pg_container,
)


def test_heavy_startup_background_work_is_opt_in_by_default(monkeypatch):
    from app.config import get_settings

    monkeypatch.delenv("CORE_DAEMON_STARTUP_ENABLED", raising=False)
    monkeypatch.delenv("CHANNEL_STREAM_STARTUP_ENABLED", raising=False)
    monkeypatch.delenv("T0_STARTUP_BACKFILL_ENABLED", raising=False)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.CORE_DAEMON_STARTUP_ENABLED is False
    assert settings.CHANNEL_STREAM_STARTUP_ENABLED is False
    assert settings.T0_STARTUP_BACKFILL_ENABLED is False

    get_settings.cache_clear()


def test_core_daemon_startup_helper_respects_setting(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "CORE_DAEMON_STARTUP_ENABLED", False)
    assert main_mod._core_daemon_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "CORE_DAEMON_STARTUP_ENABLED", True)
    assert main_mod._core_daemon_startup_enabled() is True


def test_channel_stream_startup_helper_respects_setting(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "CHANNEL_STREAM_STARTUP_ENABLED", False)
    assert main_mod._channel_stream_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "CHANNEL_STREAM_STARTUP_ENABLED", True)
    assert main_mod._channel_stream_startup_enabled() is True


def test_api_role_disables_volume_bound_startup(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", "api")
    assert main_mod._volume_bound_startup_enabled() is False
    assert main_mod._schema_bootstrap_startup_enabled() is False
    assert main_mod._data_bootstrap_startup_enabled() is False

    monkeypatch.setattr(main_mod.settings, "HIVE_PROCESS_ROLE", "runtime")
    assert main_mod._volume_bound_startup_enabled() is True
    assert main_mod._schema_bootstrap_startup_enabled() is True
    assert main_mod._data_bootstrap_startup_enabled() is True


def test_api_role_path_boundary_allows_control_plane_and_rejects_volume_paths():
    import app.main as main_mod

    assert main_mod._api_role_allows_path("/api/health") is True
    assert main_mod._api_role_allows_path("/api/auth/login") is True
    assert main_mod._api_role_allows_path("/api/notifications/unread-count") is True
    assert main_mod._api_role_allows_path("/api/v1/notifications/unread-count") is True
    assert main_mod._api_role_allows_path("/api/agents") is True
    assert main_mod._api_role_allows_path("/api/agents/") is True
    assert main_mod._api_role_allows_path("/api/v1/agents/") is True
    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/runs") is True
    assert main_mod._api_role_allows_path("/ws/chat/agent-1") is True

    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/transcript") is False
    assert main_mod._api_role_allows_path("/api/agents/agent-1/sessions/session-1/workbench") is False
    assert main_mod._api_role_allows_path("/api/agents/agent-1/files/download") is False
    assert main_mod._api_role_allows_path("/api/enterprise/knowledge-base/files") is False
    assert main_mod._api_role_allows_path("/api/v1/enterprise/knowledge-base/upload") is False
    assert main_mod._api_role_allows_path("/api/tools/execute") is False


def test_runtime_resume_runs_as_background_startup_task_not_lifespan_blocker():
    project_root = Path(__file__).resolve().parents[2]
    source = (project_root / "backend/app/main.py").read_text(encoding="utf-8")
    lifespan_source = source.split("async def lifespan", 1)[1]
    pre_background_task_setup = lifespan_source.split("# Start background tasks", 1)[0]
    background_task_setup = lifespan_source.split("startup_background_tasks = [", 1)[1].split(
        "for name, coro in startup_background_tasks:", 1
    )[0]

    assert "resume_persisted_subagent_runs" not in pre_background_task_setup
    assert "_resume_runtime_tasks_after_startup(" in background_task_setup
    assert "_run_after_startup_resume_gate(" in background_task_setup
    assert "reconcile_stuck_approval_tickets" in source


def test_startup_resume_lanes_expose_shared_partial_progress_collector():
    import inspect

    from app.agents.orchestrator import resume_persisted_async_delegations
    from app.services.heartbeat import resume_persisted_heartbeat_runs
    from app.services.subagent_run_service import resume_persisted_subagent_runs
    from app.services.trigger_daemon import resume_persisted_trigger_runs
    from app.services.web_chat_runtime import resume_persisted_web_chat_runs

    lanes = (
        resume_persisted_async_delegations,
        resume_persisted_subagent_runs,
        resume_persisted_web_chat_runs,
        resume_persisted_trigger_runs,
        resume_persisted_heartbeat_runs,
    )
    for lane in lanes:
        assert "on_resumed" in inspect.signature(lane).parameters
        assert "record_startup_resumed_task(" in inspect.getsource(lane)


def test_startup_resume_collector_records_each_success_once():
    from app.services.runtime_task_service import record_startup_resumed_task

    resumed: list[str] = []
    collected: list[str] = []

    assert record_startup_resumed_task(resumed, "run-1", collected.append) == "run-1"
    assert record_startup_resumed_task(resumed, "run-1", collected.append) == "run-1"
    assert record_startup_resumed_task(resumed, "", collected.append) is None
    assert resumed == ["run-1"]
    assert collected == ["run-1"]


@pytest.mark.asyncio
async def test_runtime_task_worker_waits_for_startup_resume_gate():
    import app.main as main_mod

    resume_done = asyncio.Event()
    calls: list[str] = []

    async def worker_loop():
        calls.append("worker_started")

    worker_task = asyncio.create_task(main_mod._run_after_startup_resume_gate(resume_done, worker_loop()))
    await asyncio.sleep(0)
    assert calls == []

    resume_done.set()
    await asyncio.wait_for(worker_task, timeout=1)
    assert calls == ["worker_started"]


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_runtime_startup_failure_isolated_and_orphan_reconcile_still_runs(
    monkeypatch,
    owner_sessionmaker,  # noqa: F811
):
    import app.agents.orchestrator as orchestrator
    import app.api.chat_sessions as chat_sessions
    import app.database as database
    import app.main as main_mod
    import app.services.approval_ticket as approval_ticket
    import app.services.heartbeat as heartbeat
    import app.services.runtime_task_service as runtime_task_service
    import app.services.subagent_run_service as subagent_run_service
    import app.services.trigger_daemon as trigger_daemon
    import app.services.web_chat_runtime as web_chat_runtime

    calls: list[str] = []

    async def expire_permissions(*, db):
        del db
        calls.append("permissions")
        return 0

    async def reconcile_approvals(*, older_than):
        del older_than
        calls.append("approvals")
        return 0

    async def fail_delegation(*, limit, on_resumed=None):
        del limit
        del on_resumed
        calls.append("delegation")
        raise RuntimeError("poisoned delegation startup lane")

    def successful_lane(name):
        async def run(*, limit, on_resumed=None):
            del limit
            calls.append(name)
            task_id = f"{name}-task"
            if on_resumed is not None:
                on_resumed(task_id)
            return [task_id]

        return run

    async def reconcile_orphans(*, exclude_task_ids):
        calls.append("orphan")
        assert exclude_task_ids == {
            "subagent-task",
            "web_chat-task",
            "trigger-task",
            "heartbeat-task",
        }
        return 0

    monkeypatch.setattr(main_mod, "_runtime_execution_startup_enabled", lambda: True)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(chat_sessions, "expire_stale_session_permission_requests", expire_permissions)
    monkeypatch.setattr(approval_ticket, "reconcile_stuck_approval_tickets", reconcile_approvals)
    monkeypatch.setattr(orchestrator, "resume_persisted_async_delegations", fail_delegation)
    monkeypatch.setattr(subagent_run_service, "resume_persisted_subagent_runs", successful_lane("subagent"))
    monkeypatch.setattr(web_chat_runtime, "resume_persisted_web_chat_runs", successful_lane("web_chat"))
    monkeypatch.setattr(trigger_daemon, "resume_persisted_trigger_runs", successful_lane("trigger"))
    monkeypatch.setattr(heartbeat, "resume_persisted_heartbeat_runs", successful_lane("heartbeat"))
    monkeypatch.setattr(runtime_task_service, "reconcile_orphaned_runtime_tasks", reconcile_orphans)

    done = asyncio.Event()
    await main_mod._resume_runtime_tasks_after_startup(done)

    assert done.is_set()
    assert calls == [
        "permissions",
        "approvals",
        "delegation",
        "subagent",
        "web_chat",
        "trigger",
        "heartbeat",
        "orphan",
    ]


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_runtime_startup_partial_subagent_lane_failure_preserves_collected_run_from_orphan(
    monkeypatch,
    owner_sessionmaker,  # noqa: F811
):
    from datetime import UTC, datetime, timedelta
    import uuid

    from sqlalchemy import delete, select, text

    import app.database as database
    import app.main as main_mod
    from app.database import tenant_scoped_session
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.runtime_replay_policy import has_runtime_restart_replay_contract
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    collected_run_id = uuid.uuid4()
    poison_run_id = uuid.uuid4()
    trigger_name = f"test_fail_runtime_update_{poison_run_id.hex}"
    function_name = f"{trigger_name}_fn"
    oldest = datetime.now(UTC) - timedelta(hours=2)

    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name="Startup Partial Lane", slug=f"spl-{tenant_id.hex[:10]}"))
        await db.flush()
        db.add(
            User(
                id=user_id,
                username=f"spl-{user_id.hex[:10]}",
                email=f"spl-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Startup Partial Lane Owner",
                tenant_id=tenant_id,
            )
        )
        await db.commit()

    def resumable_subagent_metadata(run_id: uuid.UUID) -> dict[str, object]:
        side_effect_risk = "read_only"
        return {
            "resume_after_restart": True,
            "resumable_subagent": True,
            "subagent_type": "explorer",
            "side_effect_risk": side_effect_risk,
            "restart_replay_contract": runtime_task_service.build_restart_replay_contract(
                task_type="subagent",
                task_id=run_id.hex,
                side_effect_risk=side_effect_risk,
            ),
        }

    async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
        db.add_all(
            [
                RuntimeTask(
                    id=collected_run_id,
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="running",
                    root_user_id=user_id,
                    claimed_by="dead-startup-worker",
                    claim_version=1,
                    claim_expires_at=datetime.now(UTC) - timedelta(minutes=5),
                    created_at=oldest,
                    metadata_json=resumable_subagent_metadata(collected_run_id),
                ),
                RuntimeTask(
                    id=poison_run_id,
                    tenant_id=tenant_id,
                    task_type="subagent",
                    status="pending",
                    root_user_id=user_id,
                    created_at=oldest + timedelta(seconds=1),
                    metadata_json=resumable_subagent_metadata(poison_run_id),
                ),
            ]
        )

    async with owner_sessionmaker() as db:
        await db.execute(
            text(
                f"""
                CREATE FUNCTION {function_name}() RETURNS trigger AS $$
                BEGIN
                    IF NEW.id = '{poison_run_id}'::uuid THEN
                        RAISE EXCEPTION 'injected second-row startup failure';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
                """
            )
        )
        await db.execute(
            text(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE UPDATE ON runtime_tasks
                FOR EACH ROW EXECUTE FUNCTION {function_name}()
                """
            )
        )
        await db.commit()

    monkeypatch.setattr(main_mod, "_runtime_execution_startup_enabled", lambda: True)
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(runtime_task_service, "async_session", owner_sessionmaker)
    done = asyncio.Event()
    try:
        await main_mod._resume_runtime_tasks_after_startup(done)

        assert done.is_set()
        async with tenant_scoped_session(tenant_id, session_factory=owner_sessionmaker) as db:
            collected = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == collected_run_id))).scalar_one()
            poison = (await db.execute(select(RuntimeTask).where(RuntimeTask.id == poison_run_id))).scalar_one()
        assert collected.status == "running"
        assert not bool((collected.metadata_json or {}).get("orphaned_by_restart"))
        assert has_runtime_restart_replay_contract(
            collected.metadata_json,
            task_type="subagent",
            task_id=collected_run_id,
        )
        assert poison.status == "pending"
        assert poison.claimed_by is None
        assert poison.claim_version == 0
        assert not bool((poison.metadata_json or {}).get("resumed_after_restart"))
        assert not bool((poison.metadata_json or {}).get("orphaned_by_restart"))
        assert has_runtime_restart_replay_contract(
            poison.metadata_json,
            task_type="subagent",
            task_id=poison_run_id,
        )
    finally:
        async with owner_sessionmaker() as db:
            await db.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON runtime_tasks"))
            await db.execute(text(f"DROP FUNCTION IF EXISTS {function_name}()"))
            await db.execute(delete(RuntimeTask).where(RuntimeTask.id.in_((collected_run_id, poison_run_id))))
            await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await db.commit()
