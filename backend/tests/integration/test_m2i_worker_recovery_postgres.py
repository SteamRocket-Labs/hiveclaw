"""A killed pre-launch worker is recoverable without guessing resource termination."""

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.database import pin_rls_tenant_context
from app.models.execution_environment import EnvironmentLease
from tests.integration.test_m2c_managed_environment_postgres import _exercise_real_worker_environment

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

_WAIT_BEFORE_RESOURCE = """
import asyncio, json, os, sys
from uuid import UUID
from app import models
from app.database import engine
from app.services.execution_environments import EnvironmentCommandScope
async def main():
    scope = EnvironmentCommandScope(*(UUID(value) for value in sys.argv[1:4]), 'crashed-before-resource', None)
    await scope._acquire('local_os_sandbox', 1)
    print(json.dumps({'lease_id': str(scope.lease_id), 'pid': os.getpid()}), flush=True)
    if len(sys.argv) == 4:
        await asyncio.sleep(3600)
    else:
        from pathlib import Path
        from app.services.code_execution import local_provider
        work = Path(sys.argv[4]); work.mkdir()
        home = work / 'home'; home.mkdir()
        spawn = local_provider.asyncio.create_subprocess_exec
        async def observed_spawn(*args, **kwargs):
            # Keep the sandbox's parent alive while the lease-owning worker dies.
            # Linux bubblewrap otherwise correctly exits with that same worker.
            supervisor = "import json,subprocess,sys; sys.exit(subprocess.run(json.loads(sys.argv[1])).returncode)"
            proc = await spawn(sys.executable, '-I', '-c', supervisor, json.dumps(args), **kwargs)
            print(json.dumps({'resource_pid': proc.pid}), flush=True)
            return proc
        local_provider.asyncio.create_subprocess_exec = observed_spawn
        scope.selected_provider = 'local_os_sandbox'
        code = "import os,time; open('resource-pid','w').write(str(os.getpid())); f=open('effects','ab',buffering=0)\\nwhile True: f.write(b'x'); time.sleep(.02)"
        async def operation():
            return await local_provider.execute_local_sandboxed_command(
                [str(Path(sys.executable).resolve()), '-I', '-c', code], work_dir=work,
                env={'HOME':str(home), 'PATH':os.environ['PATH']}, timeout=30)
        await scope.execute(operation, provider='local_os_sandbox', timeout=30)
asyncio.run(main())
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="Reconciliation requires the Darwin boot-process witness")
async def test_original_work_recovers_a_proven_prelaunch_worker_crash(
    owner_sessionmaker, app_user_sessionmaker, app_user_engine, monkeypatch, tmp_path
):
    async def recover(seed, client, url, scope, current):
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            _WAIT_BEFORE_RESOURCE,
            str(seed["tenant_id"]),
            str(seed["agent_id"]),
            str(seed["user_id"]),
            env={**os.environ, "DATABASE_URL": app_user_engine.url.render_as_string(hide_password=False)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            line = await asyncio.wait_for(worker.stdout.readline(), 15)
            assert line, (await worker.stderr.read()).decode()
            receipt = json.loads(line)
            assert receipt["pid"] == worker.pid
            lease_id = UUID(receipt["lease_id"])
            async with owner_sessionmaker() as db:
                lease = await db.get(EnvironmentLease, lease_id)
                lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await db.commit()
            present = (await client.get(url, params=scope)).json()["environment"]
            assert present["recovery"]["reason_code"] == "worker_still_present"
            assert present["recovery"]["can_reconcile"] is False
            body = {**scope, "expected_revision": present["revision"], "expected_lease_id": str(lease_id)}
            denied = await client.post(url + "/reconcile", json=body)
            assert denied.status_code == 409 and denied.json()["detail"]["code"] == "worker_still_present"
            worker.kill()
            assert await asyncio.wait_for(worker.wait(), 10) == -9
            (tmp_path / "terminated-worker.json").write_text(json.dumps(receipt))

            lost = (await client.get(url, params=scope)).json()["environment"]
            assert lost["observed_state"] == "lost" and lost["active_commands"] == 1
            assert lost["next_action"] == "reconcile"
            assert lost["recovery"] == {
                "lease_id": str(lease_id),
                "reason_code": "worker_terminated_before_resource_creation",
                "can_reconcile": True,
            }
            from app.core.security import get_current_user
            from app.models.user import User
            from app.models.security_audit import ResourcePermission

            async with owner_sessionmaker() as db:
                reader = User(
                    id=uuid4(),
                    tenant_id=seed["tenant_id"],
                    username=f"recovery-reader-{uuid4().hex}",
                    email=f"{uuid4().hex}@environment.example.com",
                    password_hash="x",
                    display_name="Resource reader",
                )
                db.add(reader)
                await db.flush()
                db.add(
                    ResourcePermission(
                        tenant_id=seed["tenant_id"],
                        principal_type="user",
                        principal_id=reader.id,
                        resource_type="agent",
                        resource_id=seed["agent_id"],
                        actions=["read"],
                        effect="allow",
                    )
                )
                await db.commit()
            web = client._transport.app
            identity = web.dependency_overrides[get_current_user]
            web.dependency_overrides[get_current_user] = lambda: reader
            try:
                reader_scope = {**scope, "expected_user_id": str(reader.id)}
                readonly = await client.get(url, params=reader_scope)
                assert readonly.status_code == 200 and readonly.json()["can_manage"] is False
                assert "recovery" not in readonly.json()["environment"] and "lease_id" not in readonly.text
                assert (
                    await client.post(
                        url + "/reconcile",
                        json={
                            **reader_scope,
                            "expected_revision": lost["revision"],
                            "expected_lease_id": str(lease_id),
                        },
                    )
                ).status_code == 403
            finally:
                web.dependency_overrides[get_current_user] = identity
            body = {**scope, "expected_revision": lost["revision"], "expected_lease_id": str(lease_id)}
            stale = await client.post(url + "/reconcile", json={**body, "expected_revision": lost["revision"] - 1})
            assert stale.status_code == 409
            wrong_lease = await client.post(url + "/reconcile", json={**body, "expected_lease_id": str(seed["run_id"])})
            assert wrong_lease.status_code == 409
            if os.environ.get("HIVE_M2I_DOM_BRIDGE") == "1":
                from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

                await _exercise_dom_bridge(
                    client._transport.app, seed, tmp_path, entry_file="e2e/command-environment-recovery-native.tsx"
                )
                settled = await client.get(url, params=scope)
            else:
                settled = await client.post(url + "/reconcile", json=body)
            assert settled.status_code == 200, settled.text
            recovered = settled.json()["environment"]
            assert recovered["active_commands"] == 0 and recovered["observed_state"] == "stopped"
            assert recovered["generation"] == current["generation"] and recovered["id"] == current["id"]
            assert "boot_session" not in settled.text and '"pid"' not in settled.text
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                lease = await db.get(EnvironmentLease, lease_id)
                assert lease.status == "released" and lease.released_at is not None
                assert lease.outcome["status"] == "interrupted"
                assert lease.outcome["resource_state"] == "not_created"
                assert lease.outcome["reconciliation"]["reason_code"] == lost["recovery"]["reason_code"]
                from sqlalchemy import select
                from app.models.audit import AuditLog

                logs = list(
                    await db.scalars(
                        select(AuditLog).where(
                            AuditLog.agent_id == seed["agent_id"], AuditLog.action == "environment.reconcile"
                        )
                    )
                )
                assert len(logs) == 1 and logs[0].user_id == seed["user_id"]
                assert logs[0].details["lease_id"] == str(lease_id)
            assert (await client.post(url + "/reconcile", json=body)).status_code == 409
            return recovered
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.communicate(), 10)

    await _exercise_real_worker_environment(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, recovery=recover
    )


async def test_dead_worker_does_not_release_its_live_command_resource(
    owner_sessionmaker, app_user_sessionmaker, app_user_engine, tmp_path
):
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run
    from app.services.execution_environments import control_environment, project_environment, EnvironmentError

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    work = tmp_path / "orphan-command"
    worker = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-c",
        _WAIT_BEFORE_RESOURCE,
        *(str(seed[key]) for key in ("tenant_id", "agent_id", "user_id")),
        str(work),
        env={
            **os.environ,
            "DATABASE_URL": app_user_engine.url.render_as_string(hide_password=False),
            "HIVE_CODE_SANDBOX_MODE": "auto",
            "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0",
        },
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    resource_pid = None
    try:
        receipt = json.loads(await asyncio.wait_for(worker.stdout.readline(), 15))
        resource = json.loads(await asyncio.wait_for(worker.stdout.readline(), 10))
        resource_pid = resource["resource_pid"]
        assert os.getpgid(resource_pid) == resource_pid
        for _ in range(100):
            if (work / "effects").exists() and (work / "effects").stat().st_size:
                break
            await asyncio.sleep(0.02)
        assert (work / "effects").stat().st_size > 0
        worker.kill()
        assert await asyncio.wait_for(worker.wait(), 10) == -9
        before = (work / "effects").stat().st_size
        await asyncio.sleep(0.1)
        assert (work / "effects").stat().st_size > before
        async with owner_sessionmaker() as db:
            lease = await db.get(EnvironmentLease, UUID(receipt["lease_id"]))
            assert lease.outcome["resource_lifecycle"]["phase"] == "creation_started"
            lease.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            view = await project_environment(
                db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], include_recovery=True
            )
            assert view["recovery"]["reason_code"] == "resource_termination_unconfirmed"
            assert view["recovery"]["can_reconcile"] is False
            with pytest.raises(EnvironmentError, match="^resource_termination_unconfirmed$"):
                await control_environment(
                    db,
                    tenant_id=seed["tenant_id"],
                    agent_id=seed["agent_id"],
                    action="reconcile",
                    expected_revision=view["revision"],
                    expected_lease_id=UUID(receipt["lease_id"]),
                )
            assert (await db.get(EnvironmentLease, UUID(receipt["lease_id"]))).status == "active"
        (tmp_path / "live-orphan-observation.json").write_text(
            json.dumps({**receipt, **resource, "view": view}, default=str)
        )
    finally:
        if resource_pid is not None:
            try:
                os.killpg(resource_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if worker.returncode is None and resource_pid is None:
            try:
                await asyncio.wait_for(worker.communicate(), 40)
            except asyncio.TimeoutError:
                worker.kill()
        elif worker.returncode is None:
            worker.kill()
        await asyncio.wait_for(worker.communicate(), 10)


@pytest.mark.parametrize("provider", ["local_os_sandbox", "vercel_sandbox"])
async def test_creation_receipt_storage_failure_prevents_the_provider_effect(
    provider, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    import app.database as database
    from types import SimpleNamespace
    from sqlalchemy.exc import SQLAlchemyError
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run
    from app.services import execution_environments as environments
    from app.services.code_execution import local_provider, vercel_provider
    from app.services.code_execution.service import execute_agent_command

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    original_session = environments.tenant_scoped_session

    @asynccontextmanager
    async def failed_receipt(*args, **kwargs):
        if kwargs.get("source") == "environment_resource_creation_intent":
            raise SQLAlchemyError("synthetic-sensitive-receipt-store-error")
        async with original_session(*args, **kwargs) as db:
            yield db

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("Resource creation cannot precede its durable intent")

    monkeypatch.setattr(environments, "tenant_scoped_session", failed_receipt)
    monkeypatch.setattr(local_provider.asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(vercel_provider.AsyncSandbox, "create", forbidden)
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", provider)
    for key in ("VERCEL_TEAM_ID", "VERCEL_PROJECT_ID", "VERCEL_TOKEN"):
        monkeypatch.setenv(key, "synthetic-only")
    context = SimpleNamespace(
        tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=seed["user_id"], runtime_task_id=None
    )
    async with environments.environment_command_scope(context, "receipt-store-failure") as owned:
        result = await execute_agent_command(["true"], work_dir=tmp_path, env={"HOME": str(tmp_path)}, timeout=1)
        assert result.evidence["status"] == "unavailable"
        assert result.evidence["reason_code"] == "environment_state_unavailable"
        assert "synthetic-sensitive" not in result.error
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        lease = await db.get(EnvironmentLease, owned.lease_id)
        assert lease.status == "released" and lease.outcome["resource_state"] == "not_created"
