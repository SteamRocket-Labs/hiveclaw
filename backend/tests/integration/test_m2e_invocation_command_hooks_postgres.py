"""Invocation lifecycle commands retain the real worker's resource identity."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "ready"])
async def test_native_invocation_hook_uses_worker_authority_and_environment_state(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    stopped,
):
    from app.database import pin_rls_tenant_context
    from app.models.runtime_task import RuntimeTask
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookEvent, hook_registry
    from app.services.agent_manager import AgentManager
    from app.services import web_chat_runtime
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task

    work, home = tmp_path / "setup-work", tmp_path / "setup-home"
    work.mkdir()
    home.mkdir()
    attachments = []
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=work, allowed_env_vars={"HOME", "PATH"}),
        transcript_writer=attachments.append,
    )
    spec = HookSpec(
        key="native-owned-setup",
        event=HookEvent.SETUP,
        type="command",
        command="printf SETUP_EFFECT > setup.txt",
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        failure_mode="advisory",
    )
    handler = runner.build_handler(spec)

    async def forged_metadata(context):
        return await handler(
            replace(
                context,
                agent_id=uuid4(),
                metadata={"tenant_id": str(uuid4()), "user_id": str(uuid4()), "runtime_task_id": str(uuid4())},
            )
        )

    original_initialize = AgentManager.initialize_agent_files

    async def initialize(self, db, agent, *args, **kwargs):
        result = await original_initialize(self, db, agent, *args, **kwargs)
        db.add(
            ExecutionEnvironment(
                tenant_id=agent.tenant_id,
                agent_id=agent.id,
                provider_key="local_os_sandbox",
                desired_state="stopped" if stopped else "ready",
                observed_state="stopped" if stopped else "ready",
            )
        )
        await db.flush()
        return result

    monkeypatch.setattr(AgentManager, "initialize_agent_files", initialize)
    original_execute = web_chat_runtime.execute_web_chat_run

    async def fenced(run_id):
        token = set_runtime_task_fence(task_id=run_id, worker_id="kernel-worker-1", claim_version=1)
        try:
            return await original_execute(run_id)
        finally:
            reset_runtime_task_fence(token)

    monkeypatch.setattr(web_chat_runtime, "execute_web_chat_run", fenced)
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    hook_registry.register(HookEvent.SETUP, forged_metadata, key="native-owned-setup", failure_mode="advisory")
    try:
        seed, client, _bindings, _imported = await _run_native_skill_task(
            owner_sessionmaker,
            app_user_sessionmaker,
            monkeypatch,
            tmp_path,
            client_script=[{"content": "SAFE_AGENT_RESPONSE", "finish_reason": "stop"}],
        )
    finally:
        hook_registry.unregister(HookEvent.SETUP, forged_metadata)
    assert len(client.requests) == 1
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        task = await db.get(RuntimeTask, seed["run_id"])
        assert task.status == "completed" and "SAFE_AGENT_RESPONSE" in task.result_summary
        leases = list(await db.scalars(select(EnvironmentLease)))
        if stopped:
            assert not (work / "setup.txt").exists(), "A stopped environment cannot run invocation command hooks"
            assert leases == []
            assert any("environment_stopped" in item.get("content", "") for item in attachments)
        else:
            assert (work / "setup.txt").read_text() == "SETUP_EFFECT"
            assert len(leases) == 1 and leases[0].status == "released"
            assert leases[0].runtime_task_id == seed["run_id"] and leases[0].principal_id == seed["user_id"]
            environment = await db.get(ExecutionEnvironment, leases[0].environment_id)
            assert environment.agent_id == seed["agent_id"] and environment.tenant_id == seed["tenant_id"]
    (tmp_path / "invocation-hook-observation.json").write_text(
        json.dumps(
            {
                "stopped": stopped,
                "run_id": str(seed["run_id"]),
                "agent_id": str(seed["agent_id"]),
                "user_id": str(seed["user_id"]),
                "attachments": attachments,
                "leases": [
                    {
                        "id": str(row.id),
                        "runtime_task_id": str(row.runtime_task_id),
                        "principal_id": str(row.principal_id),
                        "status": row.status,
                    }
                    for row in leases
                ],
            },
            default=str,
            indent=2,
        )
    )
