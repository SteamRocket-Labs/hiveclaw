"""Terminal callback commands use the live outbox claim, not a finished turn."""

import asyncio
import json
import shlex
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("state", ["ready", "stopped", "root_missing", "revoked", "claim_lost", "expired", "inflight"])
async def test_terminal_callback_command_obeys_environment_and_live_claim(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, state
):
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.models.user import User
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookContext, HookEvent
    from app.services.runtime_terminal_boundary_outbox import (
        RuntimeTerminalBoundaryOutboxService,
        StaleTerminalBoundaryClaim,
    )
    from app.services.web_terminal_boundary_processor import validate_web_terminal_boundary
    from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task

    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    seed, client, *_ = await _run_native_skill_task(
        owner_sessionmaker,
        app_user_sessionmaker,
        monkeypatch,
        tmp_path,
        client_script=[{"content": "COMMITTED_AGENT_RESULT", "finish_reason": "stop"}],
    )
    assert len(client.requests) == 1
    work, home = tmp_path / "terminal-work", tmp_path / "terminal-home"
    work.mkdir()
    home.mkdir()
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed" and "COMMITTED_AGENT_RESULT" in task.result_summary
        if state == "root_missing":
            task.root_user_id = None
        db.add(
            ExecutionEnvironment(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                provider_key="local_os_sandbox",
                desired_state="stopped" if state == "stopped" else "ready",
                observed_state="stopped" if state == "stopped" else "ready",
            )
        )
        await db.commit()
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    [item] = await service.claim_batch(tenant_id=seed["tenant_id"], worker_id="terminal-source")
    events = []
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=work, allowed_env_vars={"HOME", "PATH"}),
        transcript_writer=events.append,
    )
    program = "import time; f=open('terminal.txt','w');\nfor _ in range(150): f.write('x'); f.flush(); time.sleep(.02)"
    command = (
        f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(program)}"
        if state == "inflight"
        else "printf TERMINAL_EFFECT > terminal.txt"
    )
    handler = runner.build_handler(
        HookSpec(
            key="terminal-command",
            event=HookEvent.RESPONSE_COMPLETE,
            type="command",
            command=command,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            timeout_seconds=10,
            failure_mode="advisory",
        )
    )

    async def lose_claim():
        async with owner_sessionmaker() as db:
            (await db.get(RuntimeTerminalBoundaryOutbox, item.id)).claim_token = uuid4()
            await db.commit()

    async def callback(claimed):
        assert claimed.id == item.id
        if state == "claim_lost":
            await lose_claim()
        if state == "expired":
            async with owner_sessionmaker() as db:
                (await db.get(RuntimeTerminalBoundaryOutbox, item.id)).lease_expires_at = datetime.now(UTC) - timedelta(
                    seconds=1
                )
                await db.commit()
        if state == "revoked":
            async with owner_sessionmaker() as db:
                (await db.get(User, seed["user_id"])).is_active = False
                await db.commit()
        # The extension's mutable payload cannot supply compute authority.
        context = HookContext(
            event=HookEvent.RESPONSE_COMPLETE,
            agent_id=uuid4(),
            metadata={"tenant_id": str(uuid4()), "user_id": str(uuid4()), "runtime_task_id": str(uuid4())},
        )
        if state == "inflight":
            pending = asyncio.create_task(handler(context))
            try:
                async with asyncio.timeout(5):
                    while not (work / "terminal.txt").exists():
                        await asyncio.sleep(0.02)
                await lose_claim()
                await asyncio.wait_for(pending, 5)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        else:
            await handler(context)
        return {"boundary_id": str(item.id)}

    async def process():
        from app.services.runtime_task_fence import (
            StaleRuntimeTaskFenceError,
            current_runtime_task_fence,
            reset_runtime_task_fence,
            set_runtime_task_fence,
        )

        token = set_runtime_task_fence(task_id=seed["run_id"], claim_version=1, worker_id="finished-worker")
        prior = current_runtime_task_fence()
        try:
            # A stale invocation remains rejected before any callback effect.
            # The independent background consumer below has its own live claim.
            with pytest.raises(StaleRuntimeTaskFenceError):
                await service.process_terminal_boundary(
                    item=item,
                    worker_id="terminal-source",
                    canonical_validator=validate_web_terminal_boundary,
                    process_callback=callback,
                )
            assert not (work / "terminal.txt").exists()
        finally:
            assert current_runtime_task_fence() is prior
            reset_runtime_task_fence(token)
        return await service.process_terminal_boundary(
            item=item,
            worker_id="terminal-source",
            canonical_validator=validate_web_terminal_boundary,
            process_callback=callback,
        )

    if state in {"claim_lost", "inflight"}:
        with pytest.raises(StaleTerminalBoundaryClaim):
            await process()
    else:
        assert await process()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        leases = list(await db.scalars(select(EnvironmentLease)))
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed" and "COMMITTED_AGENT_RESULT" in task.result_summary
        if state in {"ready", "inflight"}:
            assert (work / "terminal.txt").exists()
            assert len(leases) == 1 and leases[0].status == "released"
            assert leases[0].runtime_task_id == seed["run_id"] and leases[0].principal_id == seed["user_id"]
            assert leases[0].outcome["source"]["boundary_id"] == str(item.id)
            if state == "inflight":
                assert leases[0].outcome["resource_state"] == "stopped"
                before = (work / "terminal.txt").read_bytes()
                await asyncio.sleep(0.1)
                assert before == (work / "terminal.txt").read_bytes() and len(before) < 150
        else:
            assert not (work / "terminal.txt").exists(), "A terminal callback cannot bypass current resource authority"
            assert leases == []
    (tmp_path / "terminal-command-observation.json").write_text(
        json.dumps(
            {
                "state": state,
                "boundary_id": str(item.id),
                "run_id": str(seed["run_id"]),
                "events": events,
                "leases": [{"id": str(row.id), "outcome": row.outcome} for row in leases],
            },
            indent=2,
        )
    )


@pytest.mark.parametrize("role", ["platform_admin", "member"])
async def test_terminal_requester_recovery_is_exact_and_checks_current_global_role(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, role
):
    import app.database as database
    from app.models.runtime_task import RuntimeTask
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.services.code_execution.service import execute_agent_command
    from app.services.direct_invocation_terminal_boundary_processor import (
        enqueue_direct_terminal_boundary_for_task,
        validate_direct_terminal_boundary,
    )
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from tests.services.test_runtime_terminal_boundary_outbox import _seed_terminal_task

    seed = await _seed_terminal_task(owner_sessionmaker, tenant_name="Terminal target company")
    home_tenant, requester = uuid4(), uuid4()
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home_tenant, name="Administrator home", slug=f"terminal-home-{home_tenant.hex}"))
        await db.flush()
        db.add(
            User(
                id=requester,
                tenant_id=home_tenant,
                role="platform_admin",
                username=f"terminal-{requester.hex}",
                email=f"{requester.hex}@terminal.example.com",
                password_hash="x",
                display_name="Canonical global requester",
            )
        )
        await db.flush()
        task = await db.get(RuntimeTask, seed.task_id)
        task.root_user_id = requester
        task.status = "failed"
        task.terminal_boundary_generation = 1
        await enqueue_direct_terminal_boundary_for_task(db, task)
        await db.commit()
    original = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    for key, value in {
        "HIVE_CODE_EXEC_PROVIDER": "local_os_sandbox",
        "HIVE_CODE_SANDBOX_MODE": "auto",
        "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0",
    }.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "home"
    home.mkdir()
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    [item] = await service.claim_batch(tenant_id=seed.tenant_id, worker_id="global-terminal")

    async def callback(_item):
        async with owner_sessionmaker() as db:
            (await db.get(User, requester)).role = role
            await db.commit()
        result = await execute_agent_command(
            ["bash", "-lc", "printf EXACT_REQUESTER > requester.txt"],
            work_dir=tmp_path,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            timeout=5,
        )
        assert (result.error is None) == (role == "platform_admin"), result.error
        return {"boundary_id": str(item.id)}

    assert await service.process_terminal_boundary(
        item=item,
        worker_id="global-terminal",
        canonical_validator=validate_direct_terminal_boundary,
        process_callback=callback,
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        assert await db.get(User, requester) is None
        leases = list(await db.scalars(select(EnvironmentLease)))
        if role == "platform_admin":
            assert (tmp_path / "requester.txt").read_text() == "EXACT_REQUESTER"
            assert (
                len(leases) == 1 and leases[0].principal_id == requester and leases[0].runtime_task_id == seed.task_id
            )
        else:
            assert not (tmp_path / "requester.txt").exists() and leases == []


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("lane", ["web", "delegation"])
async def test_installed_response_command_runs_through_real_terminal_processor(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, stopped, lane
):
    from app.models.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutbox
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookEvent, hook_registry
    from app.services import conversation_summarizer
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor
    from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task

    for key, value in {
        "HIVE_CODE_EXEC_PROVIDER": "local_os_sandbox",
        "HIVE_CODE_SANDBOX_MODE": "auto",
        "HIVE_ALLOW_UNSANDBOXED_CODE_EXEC": "0",
    }.items():
        monkeypatch.setenv(key, value)
    seed, client, *_ = await _run_native_skill_task(
        owner_sessionmaker,
        app_user_sessionmaker,
        monkeypatch,
        tmp_path,
        client_script=[{"content": "COMMITTED_WEB_RESULT", "finish_reason": "stop"}],
        delegated=lane == "delegation",
        settle_delegation=lane == "delegation",
    )
    assert len(client.requests) == 1
    if lane == "delegation":
        from app.agents import orchestrator

        for registry in (
            orchestrator._async_tasks,
            orchestrator._async_task_fallback_records,
            orchestrator._async_task_parent_ids,
        ):
            assert str(seed["run_id"]) not in registry
    async with owner_sessionmaker() as db:
        db.add(
            ExecutionEnvironment(
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                provider_key="local_os_sandbox",
                desired_state="stopped" if stopped else "ready",
                observed_state="stopped" if stopped else "ready",
            )
        )
        await db.commit()
    home = tmp_path / "home"
    home.mkdir()
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=tmp_path, allowed_env_vars={"HOME", "PATH"})
    )
    handler = runner.build_handler(
        HookSpec(
            key="installed-terminal-command",
            event=HookEvent.RESPONSE_COMPLETE,
            type="command",
            command="printf INSTALLED_EFFECT > installed.txt",
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            failure_mode="advisory",
        )
    )

    async def summary(*_args, **_kwargs):
        return "Synthetic external-model summary for the committed local turn."

    monkeypatch.setattr(conversation_summarizer, "_llm_summarize", summary)
    processor_type = DirectInvocationTerminalBoundaryProcessor if lane == "delegation" else WebTerminalBoundaryProcessor
    processor = processor_type(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    service = RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker)
    hook_registry.register(
        HookEvent.RESPONSE_COMPLETE, handler, key="installed-terminal-command", failure_mode="advisory"
    )
    try:
        counts = await service.drain_once(
            tenant_id=seed["tenant_id"],
            worker_id="real-web-terminal",
            canonical_validator=processor.validate,
            process_callback=processor,
        )
    finally:
        hook_registry.unregister(HookEvent.RESPONSE_COMPLETE, handler)
    assert counts == {"claimed": 1, "delivered": 1, "retried": 0, "dead_lettered": 0}, counts
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        row = await db.scalar(select(RuntimeTerminalBoundaryOutbox))
        assert row.status == "delivered" and row.delivery_receipt_json["response_projection_sha256"]
        leases = list(await db.scalars(select(EnvironmentLease)))
        if stopped:
            assert not (tmp_path / "installed.txt").exists() and leases == []
        else:
            assert (tmp_path / "installed.txt").read_text() == "INSTALLED_EFFECT"
            assert len(leases) == 1 and leases[0].status == "released"
            assert leases[0].runtime_task_id == seed["run_id"] and leases[0].principal_id == seed["user_id"]
            assert leases[0].outcome["source"]["boundary_id"] == str(row.id)
