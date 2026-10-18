"""Managed command resources retain canonical work and acknowledge actual stop."""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.security import get_current_user
from app.database import get_db, pin_rls_tenant_context
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.user import User
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_environment_upgrade_preserves_work_and_refuses_lossy_downgrade(
    migrated_pg_url,
    owner_sessionmaker,
    owner_engine,
    app_user_sessionmaker,
    tmp_path,
):
    from app.db_bootstrap import apply_rls_policies
    from app.models.execution_environment import ExecutionEnvironment, EnvironmentLease
    from tests.integration.conftest import APP_USER

    seed = await _seed_run(owner_sessionmaker, tmp_path)

    def migrate(action, target):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "alembic", action, target],
            env={**os.environ, "DATABASE_URL": migrated_pg_url},
            capture_output=True,
            text=True,
            timeout=60,
        )
        (tmp_path / f"migration-{action}-{target}.log").write_text(result.stdout + result.stderr)
        return result

    empty = migrate("downgrade", "tenant_channel_principal_1018")
    assert empty.returncode == 0, empty.stderr
    upgraded = migrate("upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    env_id, lease_id = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        assert await db.get(Agent, seed["agent_id"]) is not None
        db.add(
            ExecutionEnvironment(
                id=env_id, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], provider_key="local_os_sandbox"
            )
        )
        await db.flush()
        db.add(
            EnvironmentLease(
                id=lease_id,
                tenant_id=seed["tenant_id"],
                environment_id=env_id,
                invocation_key="retained-command",
                generation=1,
                fence_version=1,
                expires_at=datetime.now(UTC),
            )
        )
        await db.commit()
    refused = migrate("downgrade", "tenant_channel_principal_1018")
    assert refused.returncode != 0 and "Retain environment and command-lease evidence" in refused.stderr
    async with owner_sessionmaker() as db:
        assert (
            await db.get(ExecutionEnvironment, env_id) is not None
            and await db.get(EnvironmentLease, lease_id) is not None
        )
        assert (
            await db.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "private_environment_1018"
    tables = ("execution_environments", "environment_leases")
    async with owner_engine.begin() as conn:
        # Reinstall the same policies used by the fresh-schema bootstrap path.
        for table in tables:
            await conn.execute(text(f"DROP POLICY tenant_isolation ON {table}"))
        await conn.run_sync(lambda sync: apply_rls_policies(sync, tables=tables, forced_tables=tables))
        rows = (
            await conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('execution_environments','environment_leases')"
                )
            )
        ).all()
        assert len(rows) == 2 and all(row == (True, True) for row in rows)
        await conn.execute(text(f"GRANT ALL ON execution_environments, environment_leases TO {APP_USER}"))
    for model in (ExecutionEnvironment, EnvironmentLease):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, uuid4())
            assert list(await db.scalars(select(model))) == []
            if model is ExecutionEnvironment:
                db.add(model(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], provider_key="local_os_sandbox"))
            else:
                db.add(
                    model(
                        tenant_id=seed["tenant_id"],
                        environment_id=env_id,
                        invocation_key="foreign-write",
                        generation=1,
                        fence_version=2,
                        expires_at=datetime.now(UTC),
                    )
                )
            with pytest.raises(DBAPIError, match="row-level security"):
                await db.flush()


async def test_unknown_resource_keeps_lease_and_blocks_duplicate_execution(
    owner_sessionmaker, app_user_sessionmaker, tmp_path, monkeypatch
):
    import app.database as database
    from app.models.execution_environment import EnvironmentLease
    from app.services.execution_environments import (
        EnvironmentCommandScope,
        EnvironmentError,
        control_environment,
        project_environment,
    )

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    scope = EnvironmentCommandScope(seed["tenant_id"], seed["agent_id"], seed["user_id"], "unknown-command", None)
    await scope._acquire("vercel_sandbox", 1)
    scope.resource_state = "unknown"
    scope.outcome = {"status": "interrupted"}
    await scope.release()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        view = await project_environment(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"])
        lease = await db.get(EnvironmentLease, scope.lease_id)
        assert view["observed_state"] == "lost" and view["active_commands"] == 1
        assert view["next_action"] == "inspect_original_worker" and lease.status == "active"
        assert lease.outcome == {
            "status": "interrupted", "resource_state": "unknown",
            "resource_lifecycle": {"schema": "hive.environment.resource-lifecycle.v1", "phase": "unconfirmed", "worker": scope.worker_witness},
        }
        with pytest.raises(EnvironmentError, match="environment_reconciliation_required"):
            await control_environment(
                db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                action="resume",
                expected_revision=view["revision"],
            )
    invoked = False

    async def operation():
        nonlocal invoked
        invoked = True
        raise AssertionError("An unconfirmed resource must not be duplicated")

    retry = EnvironmentCommandScope(seed["tenant_id"], seed["agent_id"], seed["user_id"], "retry", None)
    result = await retry.execute(operation, provider="vercel_sandbox", timeout=1)
    assert result.evidence["reason_code"] == "environment_reconciliation_required" and not invoked
    assert retry.lease_id is None


async def test_real_worker_command_environment_stop_resume_and_saved_files(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path
):
    await _exercise_real_worker_environment(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)


async def _exercise_real_worker_environment(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, *, recovery=None,
    initial_delivery=False, recovery_invocation_key="crashed-before-resource",
):
    from app.api.environments import router
    import app.database as database
    import app.runtime.invoker as invoker
    from app.config import get_settings
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import execute_web_chat_run

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    import app.services.llm_client as llm_module

    original_factory = llm_module.create_llm_client

    def unexpected_provider(*_args, **_kwargs):
        raise AssertionError("A native resource test cannot construct a real model transport")

    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original_factory:
                    monkeypatch.setattr(module, name, unexpected_provider)

    class MemoryTransport:
        async def stream(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"summary": "Synthetic command-resource continuity."}))

        async def close(self):
            pass

    monkeypatch.setattr(llm_module, "create_llm_client_from_config", lambda *_args, **_kwargs: MemoryTransport())
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed["user_id"])
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        from app.models.capability_policy import CapabilityPolicy

        db.add(
            CapabilityPolicy(
                tenant_id=seed["tenant_id"],
                capability="workspace.command.execute",
                allowed=True,
                requires_approval=False,
            )
        )
        await AgentManager().initialize_agent_files(
            db, agent, personality="Managed command employee", original_author_id=owner.id
        )
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-environment-model")
        model.max_input_tokens = 256_000
        await db.commit()
    reader = owner

    async def identity():
        return reader

    async def db_dependency():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()

    web = FastAPI()
    web.include_router(router, prefix="/api")
    web.dependency_overrides[get_current_user] = identity
    web.dependency_overrides[get_db] = db_dependency
    scope = {"expected_tenant_id": str(seed["tenant_id"]), "expected_user_id": str(owner.id)}
    url = f"/api/agents/{agent.id}/environment"

    async def run(command, *, first=False):
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.session_v2_persistence import resolve_session_mutation_authority, accept_human_input
        from app.services.session_input_admission import run_user_prompt_admission
        from app.services.session_input_dispatch import dispatch_admitted_input_fast_path

        if first:
            run_id, worker, version = seed["run_id"], "kernel-worker-1", 1
        else:
            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, seed["tenant_id"])
                authority = await resolve_session_mutation_authority(
                    db, user=owner, agent_id=agent.id, session_id=seed["session_id"], action="mutate_session_input"
                )
                input_id = uuid4()
                await accept_human_input(
                    db,
                    authority=authority,
                    intent={
                        "kind": "start_turn",
                        "input_id": str(input_id),
                        "idempotency_key": str(input_id),
                        "session_id": str(seed["session_id"]),
                        "content_parts": [{"type": "text", "text": "Continue the command assignment"}],
                    },
                )
                admitted = await run_user_prompt_admission(
                    db, authority=authority, input_id=input_id, worker_id="managed-admission"
                )
                await db.commit()
                receipt = await dispatch_admitted_input_fast_path(
                    db, admission_id=admitted.admission_id, worker_id="managed-dispatch"
                )
                assert "run" in receipt.receipt, (receipt.state, receipt.receipt)
                run_id = UUID(receipt.receipt["run"]["run_id"])
                claims = await RuntimeTaskClaimService(
                    db=db, worker_id="managed-worker", lease_seconds=600
                ).claim_available()
                claim = next(item for item in claims if item.id == run_id)
                worker, version = "managed-worker", claim.claim_version
                await db.commit()
        script = ScriptedLLMClient(
            [
                {
                    "tool_calls": [
                        {
                            "id": "managed-command",
                            "type": "function",
                            "function": {
                                "name": "run_command",
                                "arguments": json.dumps({"command": command, "timeout": 10}),
                            },
                        }
                    ],
                    "finish_reason": "tool_calls",
                },
                {"content": "The command outcome is recorded in this work."
                 + ("\nDELIVERABLE: workspace/saved.txt" if initial_delivery and first else ""), "finish_reason": "stop"},
            ]
        )
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: script)
        fence = set_runtime_task_fence(task_id=run_id, worker_id=worker, claim_version=version)
        try:
            await execute_web_chat_run(run_id)
        finally:
            reset_runtime_task_fence(fence)
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

        processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"],
            worker_id="managed-boundary",
            canonical_validator=processor.validate,
            process_callback=processor,
            task_types=("web_chat_turn",),
        )
        assert boundary["delivered"] == 1, boundary
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            task = await db.get(RuntimeTask, run_id)
            assert task.status == "completed", (task.status, task.result_summary)
        return script

    python = shlex.quote(str(Path(sys.executable).resolve()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        initial = await client.get(url, params=scope)
        assert initial.status_code == 200 and initial.json()["environment"] is None
        assert initial.headers["cache-control"] == "no-store"
        script = await run(
            f"{python} -I -c \"open('saved.txt','w').write('CANONICAL_ENVIRONMENT_FILE'); print('FIRST_COMMAND')\"",
            first=True,
        )
        assert (seed["agent_root"] / "workspace/saved.txt").read_text() == "CANONICAL_ENVIRONMENT_FILE"
        assert "FIRST_COMMAND" in json.dumps(script.requests[-1]["messages"])
        current = (await client.get(url, params=scope)).json()["environment"]
        assert current["generation"] == 1 and current["active_commands"] == 0
        assert current["capabilities"]["persistent_compute"] is False
        if recovery is not None:
            current = await recovery(seed, client, url, scope, current)
        stopped = await client.post(url + "/stop", json={**scope, "expected_revision": current["revision"]})
        assert stopped.status_code == 200 and stopped.json()["environment"]["observed_state"] == "stopped"
        denied = await run(f"{python} -I -c \"open('must-not-run','w').write('blocked')\"")
        assert not (seed["agent_root"] / "workspace/must-not-run").exists()
        assert "environment_stopped" in json.dumps(denied.requests[-1]["messages"])
        conflict = await client.post(url + "/resume", json={**scope, "expected_revision": current["revision"]})
        assert conflict.status_code == 409
        resumed = await client.post(
            url + "/resume", json={**scope, "expected_revision": stopped.json()["environment"]["revision"]}
        )
        assert resumed.status_code == 200
        ready = resumed.json()["environment"]
        assert ready["id"] == current["id"] and ready["generation"] == current["generation"] + 1
        script = await run(
            f"{python} -I -c \"print(open('saved.txt').read()); open('saved.txt','a').write('_CONTINUED')\""
        )
        assert "CANONICAL_ENVIRONMENT_FILE" in json.dumps(script.requests[-1]["messages"])
        assert (seed["agent_root"] / "workspace/saved.txt").read_text().endswith("_CONTINUED")

        from app.services.code_execution import local_provider
        from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment

        real_spawn = local_provider.asyncio.create_subprocess_exec
        processes = []
        started = asyncio.Event()

        async def spawn(*args, **kwargs):
            process = await real_spawn(*args, **kwargs)
            if "ACTIVE_ENV_COMMAND" in " ".join(args):
                processes.append((process, Path(kwargs["cwd"])))
                started.set()
            return process

        monkeypatch.setattr(local_provider.asyncio, "create_subprocess_exec", spawn)
        loop = (
            "import time; print('ACTIVE_ENV_COMMAND'); f=open('active-effects','a'); "
            + "\nwhile True: f.write('x'); f.flush(); time.sleep(.02)"
        )
        pending = asyncio.create_task(run(f"{python} -I -c {shlex.quote(loop)}"))
        try:
            await asyncio.wait_for(started.wait(), 10)
            path = processes[0][1] / "active-effects"
            for _ in range(100):
                if path.exists() and path.stat().st_size:
                    break
                await asyncio.sleep(0.02)
            assert path.exists() and path.stat().st_size > 0
            inflight = (await client.get(url, params=scope)).json()["environment"]
            assert inflight["active_commands"] == 1
            stopping = await client.post(url + "/stop", json={**scope, "expected_revision": inflight["revision"]})
            assert stopping.status_code == 200 and stopping.json()["environment"]["observed_state"] == "stopping"
            stopped_script = await asyncio.wait_for(pending, 20)
            assert "environment_stopped" in json.dumps(stopped_script.requests[-1]["messages"])
            final = (await client.get(url, params=scope)).json()["environment"]
            assert final["observed_state"] == "stopped" and final["active_commands"] == 0
            assert all(process.returncode is not None for process, _cwd in processes)
            output = seed["agent_root"] / "workspace/active-effects"
            before = output.read_bytes()
            await asyncio.sleep(0.15)
            assert output.read_bytes() == before
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        wrong_user = await client.get(url, params={**scope, "expected_user_id": str(uuid4())})
        assert wrong_user.status_code == 409
        wrong_company = await client.get(url, params={**scope, "expected_tenant_id": str(uuid4())})
        assert wrong_company.status_code == 403
        assert (await client.get(f"/api/agents/{uuid4()}/environment", params=scope)).status_code == 404
        from app.models.security_audit import ResourcePermission

        async with owner_sessionmaker() as db:
            member = User(
                id=uuid4(),
                tenant_id=seed["tenant_id"],
                username=f"env-reader-{uuid4().hex}",
                email=f"{uuid4().hex}@environment.example.com",
                display_name="Resource reader",
                password_hash="x",
            )
            db.add(member)
            await db.flush()
            db.add(
                ResourcePermission(
                    tenant_id=seed["tenant_id"],
                    principal_type="user",
                    principal_id=member.id,
                    resource_type="agent",
                    resource_id=agent.id,
                    actions=["read"],
                    effect="allow",
                )
            )
            await db.commit()
        reader = member
        member_scope = {**scope, "expected_user_id": str(member.id)}
        view = await client.get(url, params=member_scope)
        assert view.status_code == 200 and view.json()["can_manage"] is False
        assert "lease_id" not in view.text and "runtime_task_id" not in view.text and "invocation_key" not in view.text
        assert (
            await client.post(url + "/resume", json={**member_scope, "expected_revision": final["revision"]})
        ).status_code == 403
        reader = owner
        if os.environ.get("HIVE_M2C_DOM_BRIDGE") == "1":
            from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge

            await _exercise_dom_bridge(web, seed, tmp_path, entry_file="e2e/command-environment-native.tsx")
            refreshed = (await client.get(url, params=scope)).json()["environment"]
            assert refreshed["observed_state"] == "stopped" and refreshed["generation"] == final["generation"] + 1
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            leases = list(
                await db.scalars(select(EnvironmentLease).where(EnvironmentLease.environment_id == UUID(current["id"])))
            )
            command_leases = [lease for lease in leases if lease.invocation_key != recovery_invocation_key]
            assert len(leases) == 3 + int(recovery is not None)
            assert len(command_leases) == 3 and all(lease.status == "released" for lease in leases)
            assert any(
                lease.outcome.get("status") == "cancelled" and lease.outcome.get("resource_state") == "stopped"
                for lease in leases
            )
            await pin_rls_tenant_context(db, uuid4())
            assert list(await db.scalars(select(ExecutionEnvironment))) == []
            assert list(await db.scalars(select(EnvironmentLease))) == []
        (tmp_path / "environment-observation.json").write_text(
            json.dumps(
                {
                    "environment_id": current["id"],
                    "initial_generation": 1,
                    "resumed_generation": ready["generation"],
                    "final": final,
                    "stopped_processes": [
                        {"pid": process.pid, "exit_code": process.returncode} for process, _cwd in processes
                    ],
                    "leases": [
                        {
                            "id": str(lease.id),
                            "status": lease.status,
                            "generation": lease.generation,
                            "outcome": lease.outcome,
                        }
                        for lease in leases
                    ],
                },
                default=str,
                indent=2,
            )
        )
