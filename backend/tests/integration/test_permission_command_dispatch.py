"""One approved invocation admits one command dispatch across copied contexts."""

import asyncio
from dataclasses import replace

import pytest

from tests.integration.test_permission_command_boundary import _approved_binding
from tests.integration.test_permission_command_environment import _command

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("same_command", [False, True])
@pytest.mark.parametrize("after_contexts_exit", [False, True])
async def test_approved_binding_refuses_a_second_dispatch_with_unchanged_request(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, same_command, after_contexts_exit,
):
    from app.services.code_execution import service
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.execution_environments import (
        command_environment_context, current_command_frame, current_environment_scope, environment_command_scope,
    )
    from app.services.shared_group_authority import group_permission_execution

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    release = asyncio.Event()

    async def operation(command, **_kwargs):
        label = "other-operation" if command[-1] == "printf other-operation" else "approved"
        with effect.open("a") as stream:
            stream.write(label + "\n")
        return CodeExecutionResult(stdout=label, exit_code=0)

    monkeypatch.setattr(service, "_execute_selected_command", operation)
    async with _approved_binding(app_user_sessionmaker, seed, effect.parent) as values:
        db, authority, invocation, control_id, binding, frame, scope = values
        request = scope.runtime_request

        async def dispatch(command):
            return await service.execute_agent_command(["bash", "-lc", command], work_dir=effect.parent,
                env={}, timeout=5)

        async def copied_command():
            await release.wait()
            assert current_environment_scope() is approved
            assert current_command_frame() == frame
            assert approved.runtime_request is request
            command = request.arguments["command"] if same_command else "printf other-operation"
            if after_contexts_exit:
                async with environment_command_scope(frame, frame.tool_call_id) as copied:
                    copied.runtime_request = request
                    return await dispatch(command)
            return await dispatch(command)

        with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                command_environment_context(frame):
            async with environment_command_scope(frame, frame.tool_call_id) as approved:
                approved.runtime_request = request
                first = await dispatch(request.arguments["command"])
                assert not first.error, first
                assert effect.read_text().splitlines() == ["approved"]
                assert (await binding.revalidate(db, approved)).id == seed.user_id
                pending = asyncio.create_task(copied_command())
                if not after_contexts_exit:
                    release.set()
                    second = await pending
        if after_contexts_exit:
            release.set()
            second = await pending
        assert "environment_source_authority_revoked" in (second.error or ""), second
        assert effect.read_text().splitlines() == ["approved"]


@pytest.mark.parametrize("validation", ["authorize", "revalidate"])
async def test_request_call_id_must_match_binding_even_when_the_invocation_row_matches(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, validation,
):
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_session import ChatSession
    from app.services.execution_environments import EnvironmentError, command_environment_context
    from app.services.shared_group_authority import _permission_execution_matches, group_permission_execution

    seed, effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    async with _approved_binding(app_user_sessionmaker, seed, effect.parent) as values:
        db, authority, invocation, control_id, binding, frame, scope = values
        invocation.provider_tool_use_id = "changed-call"
        scope.runtime_request = replace(scope.runtime_request,
            context=replace(scope.runtime_request.context, tool_call_id="changed-call"))
        await db.commit()
        with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                command_environment_context(frame):
            task = await db.get(RuntimeTask, seed.run_id)
            session = await db.get(ChatSession, seed.session_id)
            assert await _permission_execution_matches(db, task=task, session=session,
                runtime_request=scope.runtime_request)
            with pytest.raises(EnvironmentError, match="environment_source_authority_revoked"):
                await getattr(binding, validation)(db, scope)
