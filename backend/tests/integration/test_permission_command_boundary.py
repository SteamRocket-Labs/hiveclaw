"""Permission command authority covers only its fenced, approved invocation."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import uuid

import pytest

from app.database import pin_rls_tenant_context
from app.models.session_v2 import SessionToolInvocation
from tests.integration.test_permission_command_environment import _command, _result, _url
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _authority

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@asynccontextmanager
async def _approved_binding(sessionmaker, seed, workspace):
    from app.services.execution_environments import EnvironmentCommandFrame, EnvironmentCommandScope
    from app.services.permission_command_environment import PermissionCommandBinding
    from app.services.permission_execution import permission_execution_lease
    from app.services.session_control_input import accept_tool_permission_response, apply_permission_response_control_input
    from app.services.session_tool_runtime import mark_tool_effect_started
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        _user, authority = await _authority(db, seed)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        control_id = uuid.uuid5(seed.permission_ids[0], "permission-response:allow_once")
        await accept_tool_permission_response(db, authority=authority, control_id=control_id,
            idempotency_key=f"permission:{seed.permission_ids[0]}:allow_once", invocation_id=invocation.id,
            permission_item_id=seed.permission_ids[0], permission_request_version=invocation.permission_request_version,
            permission_authority_snapshot_hash=invocation.permission_authority_snapshot_hash,
            expected_run_id=seed.run_id, decision="allow_once", response_schema="hive.tool_permission_response.v1")
        await apply_permission_response_control_input(db, authority=authority, control_id=control_id)
        await mark_tool_effect_started(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
            session_id=seed.session_id, invocation_id=invocation.id,
            effective_arguments=invocation.effective_arguments_json, permission_control_id=control_id)
        await db.commit()
        async with permission_execution_lease(db, seed.permission_ids[0]) as lease:
            binding = PermissionCommandBinding(authority, invocation.id, invocation.run_id,
                seed.permission_ids[0], invocation.provider_tool_use_id, lease)
            frame = EnvironmentCommandFrame(seed.tenant_id, seed.agent_id, seed.user_id, seed.run_id,
                binding, tool_call_id=invocation.provider_tool_use_id, session_id=seed.session_id)
            request = ToolExecutionRequest(invocation.tool_name, dict(invocation.effective_arguments_json),
                ToolExecutionContext(agent_id=seed.agent_id, tenant_id=str(seed.tenant_id), user_id=seed.user_id,
                    workspace=workspace, session_id=str(seed.session_id), runtime_task_id=str(seed.run_id),
                    tool_call_id=invocation.provider_tool_use_id))
            scope = EnvironmentCommandScope(seed.tenant_id, seed.agent_id, seed.user_id, frame.tool_call_id,
                seed.run_id, source_binding=binding, session_id=seed.session_id, runtime_request=request)
            yield db, authority, invocation, control_id, binding, frame, scope


@pytest.mark.parametrize("mismatch", ["tool_call_id", "arguments"])
@pytest.mark.parametrize("after_contexts_exit", [False, True])
async def test_copied_permission_context_cannot_authorize_another_command(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, mismatch, after_contexts_exit,
):
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.execution_environments import command_environment_context, environment_command_scope
    from app.services.shared_group_authority import group_permission_execution

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    release = asyncio.Event()

    async def operation(label):
        with effect.open("a") as output:
            output.write(label + "\n")
        return CodeExecutionResult(stdout=label, exit_code=0)

    async with _approved_binding(app_user_sessionmaker, seed, effect.parent) as values:
        _db, authority, invocation, control_id, _binding, frame, scope = values
        changed_context = replace(scope.runtime_request.context,
            tool_call_id="another-call" if mismatch == "tool_call_id" else frame.tool_call_id)
        changed_request = replace(scope.runtime_request, context=changed_context,
            arguments={"command": "another command"} if mismatch == "arguments" else scope.runtime_request.arguments)
        changed_frame = replace(frame, tool_call_id=changed_context.tool_call_id)

        async def copied_command():
            await release.wait()
            async with environment_command_scope(changed_frame, "copied-command") as child_scope:
                child_scope.runtime_request = changed_request
                return await child_scope.execute(lambda: operation("unapproved"), provider="local_os_sandbox", timeout=5)

        with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                command_environment_context(frame):
            async with environment_command_scope(frame, frame.tool_call_id) as approved:
                approved.runtime_request = scope.runtime_request
                result = await approved.execute(lambda: operation("approved"), provider="local_os_sandbox", timeout=5)
                assert not result.error, result
            pending = asyncio.create_task(copied_command())
            if not after_contexts_exit:
                release.set()
                result = await pending
        if after_contexts_exit:
            release.set()
            result = await pending
        assert effect.read_text().splitlines() == ["approved"]
        assert "environment_source_authority_revoked" in (result.error or ""), result


@pytest.mark.parametrize("invalid", ["missing_request", "frame_tool_call_id", "prepared_not_started", "result_recorded"])
async def test_permission_binding_requires_current_call_and_durable_unsettled_fence(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, invalid,
):
    from app.services.execution_environments import EnvironmentError, command_environment_context
    from app.services.shared_group_authority import group_permission_execution
    from app.services.session_tool_runtime import complete_tool_invocation

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    async with _approved_binding(app_user_sessionmaker, seed, effect.parent) as values:
        db, authority, invocation, control_id, binding, frame, scope = values
        with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                command_environment_context(frame):
            assert (await binding.authorize(db, scope)).id == seed.user_id
            assert (await binding.revalidate(db, scope)).id == seed.user_id
        if invalid == "missing_request":
            scope.runtime_request = None
        elif invalid == "frame_tool_call_id":
            frame = replace(frame, tool_call_id="another-frame")
        elif invalid == "prepared_not_started":
            invocation.effect_state = "prepared_not_started"
        else:
            await complete_tool_invocation(db, tenant_id=seed.tenant_id, agent_id=seed.agent_id,
                session_id=seed.session_id, invocation_id=invocation.id, provider_result_content="settled",
                execution_evidence={"schema": "hive.tool_execution_evidence.v1", "status": "settled",
                    "tool_decision": {"outcome": "allow", "input_hash": invocation.effective_args_hash},
                    "execution_frame": {"status": "completed"}})
            assert invocation.result_event_id is not None
        await db.commit()
        with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                command_environment_context(frame):
            for validate in (binding.authorize, binding.revalidate):
                with pytest.raises(EnvironmentError, match="environment_source_authority_revoked"):
                    await validate(db, scope)


@pytest.mark.parametrize("failure_mode", ["advisory", "required"])
async def test_pre_tool_command_hook_cannot_repeat_effect_before_durable_fence(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, failure_mode,
):
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookContext, HookEvent
    from app.services.code_execution import service
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.tools.service import ToolRuntimeService

    class SimulatedCrash(BaseException):
        pass

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    hook_effect = effect.with_name("hook-effects.txt")
    observations = []

    async def operation(*_args, **kwargs):
        output = hook_effect if kwargs["runtime"] == "hook_runner" else effect
        with output.open("a") as stream:
            stream.write("effect\n")
        return CodeExecutionResult(stdout="{}" if output == hook_effect else "approved effect", exit_code=0)

    runner = GovernedHookRunner(policy=HookRunnerPolicy(enabled=True, work_dir=effect.parent))
    handler = runner.build_handler(HookSpec(key="pre-command", event=HookEvent.PRE_TOOL_USE,
        type="command", command="printf hook", failure_mode=failure_mode))

    async def pre_hook(_self, _name, _args, context, **_kwargs):
        result = await handler(HookContext(event=HookEvent.PRE_TOOL_USE, agent_id=context.agent_id))
        async with owner_sessionmaker() as db:
            invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
            observations.append((invocation.effect_state, invocation.result_event_id))
        if len(observations) == 1:
            raise SimulatedCrash()
        return result

    monkeypatch.setattr(service, "_execute_selected_command", operation)
    monkeypatch.setattr(ToolRuntimeService, "_emit_pre_tool_hook", pre_hook)
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        with pytest.raises(SimulatedCrash):
            await client.post(_url(seed), json={"action": "allow_once"})
        response = await client.post(_url(seed), json={"action": "allow_once"})
        assert response.status_code == 200, response.text
        replay = await client.post(_url(seed), json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert response.json()["result_event_id"] == replay.json()["result_event_id"]
    assert observations == [("prepared_not_started", None), ("prepared_not_started", None)]
    hook_effects = hook_effect.read_text().splitlines() if hook_effect.exists() else []
    assert hook_effects == []
    invocation, _task, content = await _result(app_user_sessionmaker, seed)
    if failure_mode == "advisory":
        assert effect.read_text().splitlines() == ["effect"]
        assert "approved effect" in content
        assert invocation.effect_state == "effect_committed"
    else:
        assert not effect.exists()
        assert content.startswith("Blocked by hook: Required hook pre-command failed:")
        assert "environment_source_authority_revoked" in content
