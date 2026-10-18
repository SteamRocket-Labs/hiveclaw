"""Command hooks share employee resource controls without blocking safe tools."""

import json
import sys
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("record_durably", [False, True])
async def test_tool_command_hooks_follow_stop_resume_and_trusted_identity(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    record_durably,
):
    import app.database as database
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookContext, HookEvent
    from app.services.execution_environments import control_environment, project_environment
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    original = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "local_os_sandbox")
    monkeypatch.setenv("HIVE_CODE_SANDBOX_MODE", "auto")
    monkeypatch.setenv("HIVE_ALLOW_UNSANDBOXED_CODE_EXEC", "0")
    work = tmp_path / "hook-work"
    home = tmp_path / "hook-home"
    work.mkdir()
    home.mkdir()
    events, spans = [], []

    async def record_span(fact):
        spans.append(fact)
        if record_durably:
            from app.services.plugin_hook_service import _record_governed_hook_span

            await _record_governed_hook_span(fact)

    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=work, allowed_env_vars={"HOME", "PATH"}),
        transcript_writer=events.append,
        span_recorder=record_span,
    )

    async def command_hook(event):
        name = "pre.txt" if event == HookEvent.PRE_TOOL_USE else "post.txt"
        record = await runner.run(
            HookSpec(
                key=name,
                event=event,
                type="command",
                command=f"printf HOOK_EXECUTED > {name}",
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            ),
            HookContext(
                event=event,
                agent_id=seed["agent_id"] if record_durably else uuid4(),
                metadata={
                    "tenant_id": str(seed["tenant_id"] if record_durably else uuid4()),
                    "user_id": str(seed["user_id"] if record_durably else uuid4()),
                    "runtime_task_id": str(seed["run_id"]),
                },
            ),
        )
        return record.hook_result

    async def pre_hook(*_args, **_kwargs):
        return await command_hook(HookEvent.PRE_TOOL_USE)

    async def post_hook(*_args, **_kwargs):
        return await command_hook(HookEvent.POST_TOOL_USE)

    monkeypatch.setattr(ToolRuntimeService, "_emit_pre_tool_hook", pre_hook)
    monkeypatch.setattr(ToolRuntimeService, "_emit_post_tool_hook", post_hook)
    context = ToolExecutionContext(
        agent_id=seed["agent_id"],
        user_id=seed["user_id"],
        tenant_id=str(seed["tenant_id"]),
        runtime_task_id=str(seed["run_id"]),
        workspace=seed["agent_root"],
        session_id=str(seed["session_id"]),
    )

    class Registry:
        async def try_execute(self, request):
            assert request.tool_name == "get_current_time"
            return "SAFE_TOOL_RESULT"

    async def governance_context(**_kwargs):
        return SimpleNamespace()

    service = ToolRuntimeService(
        runtime_resolver=SimpleNamespace(resolve=lambda **_kwargs: context),
        governance_resolver=SimpleNamespace(
            build_context=governance_context, build_dependencies=lambda: SimpleNamespace()
        ),
        registry=Registry(),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args: "UNEXPECTED_FALLBACK",
        direct_fallback_executor=lambda *_args: "UNEXPECTED_DIRECT",
        asset_ref_resolver=lambda **_kwargs: (),
        capability_group_policy_loader=lambda *_args: {},
    )
    async with owner_sessionmaker() as db:
        db.add(
            ExecutionEnvironment(
                tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], provider_key="local_os_sandbox"
            )
        )
        await db.commit()

    async def run():
        result = await service.execute(
            "get_current_time",
            {},
            agent_id=context.agent_id,
            user_id=context.user_id,
            runtime_task_id=context.runtime_task_id,
            session_id=context.session_id,
        )
        assert result == "SAFE_TOOL_RESULT", result

    async def control(action):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            before = await project_environment(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"])
            result = await control_environment(
                db,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                action=action,
                expected_revision=before["revision"],
            )
            await db.commit()
            return result

    await run()
    for name in ("pre.txt", "post.txt"):
        assert (work / name).read_text() == "HOOK_EXECUTED", events
        (work / name).unlink()
    stopped = await control("stop")
    await run()
    assert not (work / "pre.txt").exists() and not (work / "post.txt").exists(), (
        "Stopped command hooks must not execute"
    )
    assert any("environment_stopped" in event.get("content", "") for event in events)
    resumed = await control("resume")
    await run()
    assert resumed["id"] == stopped["id"] and resumed["generation"] == stopped["generation"] + 1
    assert (work / "pre.txt").read_text() == (work / "post.txt").read_text() == "HOOK_EXECUTED"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        leases = list(await db.scalars(select(EnvironmentLease)))
        assert len(leases) == 4 and all(lease.status == "released" for lease in leases)
        assert {lease.principal_id for lease in leases} == {seed["user_id"]}
        assert {lease.environment_id for lease in leases} == {UUID(resumed["id"])}
        assert {lease.generation for lease in leases} == {1, 2}
        assert {row["environment"]["lease_id"] for row in spans if row["environment"]["lease_id"]} == {
            str(lease.id) for lease in leases
        }
        if record_durably:
            from app.models.invocation_span import InvocationSpan

            stored = list(
                await db.scalars(
                    select(InvocationSpan).where(
                        InvocationSpan.agent_id == seed["agent_id"],
                        InvocationSpan.span_type == "hook",
                        InvocationSpan.name.in_(["hook:pre.txt", "hook:post.txt"]),
                    )
                )
            )
            assert len(stored) == 6 and {row.user_id for row in stored} == {seed["user_id"]}
            assert {
                row.metadata_json["environment"]["lease_id"]
                for row in stored
                if row.metadata_json["environment"]["lease_id"]
            } == {str(lease.id) for lease in leases}
    (tmp_path / "hook-resource-observation.json").write_text(
        json.dumps(
            {
                "stopped": stopped,
                "resumed": resumed,
                "spans": spans,
                "attachments": [
                    event["metadata"]["provider_evidence"]
                    for event in events
                    if event["event_type"] == "hook_attachment"
                ],
            },
            default=str,
            indent=2,
        )
    )
