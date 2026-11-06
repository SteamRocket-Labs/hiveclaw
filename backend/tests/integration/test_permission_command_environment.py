"""Approved commands retain their suspended run and exclusive permission owner."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import uuid

import pytest

from app.database import pin_rls_tenant_context
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from tests.integration.test_session_permission_execution_once import _mounted_client
from tests.services.test_session_permission_runtime import _authority, _seed_waiting_batch

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _command(owner_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.services import agent_tools, runtime_task_worker

    seed = await _seed_waiting_batch(owner_sessionmaker, tool_name="run_command",
        tool_arguments={"command": "rm workspace/approved-disposable.txt"})
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, seed.run_id)
        task.metadata_json = {**task.metadata_json, "interactive_pause": "awaiting_session_permission"}
        await db.commit()
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    workspace = tmp_path / str(seed.agent_id) / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "approved-disposable.txt").write_text("Disposable test input.\n")
    notifications = []

    async def notify(**kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", notify)
    return seed, workspace / "effects.txt", notifications


def _url(seed):
    return f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/permissions/{seed.permission_ids[0]}/resolve"


async def _result(sessionmaker, seed):
    async with sessionmaker() as db:
        await pin_rls_tenant_context(db, seed.tenant_id)
        invocation = await db.get(SessionToolInvocation, seed.invocation_ids[0])
        task = await db.get(RuntimeTask, seed.run_id)
        event = await db.get(ChatTranscriptEvent, invocation.result_event_id)
        return invocation, task, event.metadata_json["v2_payload"]["content"]


async def test_approved_command_executes_once_in_its_suspended_run(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.services.code_execution import service
    from app.services.code_execution.contracts import CodeExecutionResult
    from app.services.execution_environments import current_environment_scope

    seed, effect, notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    sources = []

    async def operation(*_args, **_kwargs):
        async with owner_sessionmaker() as db:
            assert (await db.get(RuntimeTask, seed.run_id)).status == "suspended"
        sources.append(current_environment_scope().receipt()["source"])
        with effect.open("a") as out:
            out.write("approved effect\n")
        return CodeExecutionResult(stdout="approved effect", exit_code=0)

    monkeypatch.setattr(service, "_execute_selected_command", operation)
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        first = await client.post(_url(seed), json={"action": "allow_once"})
        assert first.status_code == 200, first.text
        replay = await client.post(_url(seed), json={"action": "allow_once"})
        assert replay.status_code == 200, replay.text
        assert first.json()["run_id"] == replay.json()["run_id"] == str(seed.run_id)
        assert first.json()["result_event_id"] == replay.json()["result_event_id"]
    invocation, task, content = await _result(app_user_sessionmaker, seed)
    assert effect.exists(), content
    assert effect.read_text().splitlines() == ["approved effect"]
    assert content.endswith("approved effect"), content
    assert invocation.effect_state == "effect_committed" and task.status == "resumable"
    assert sources == [{"kind": "session_permission", "permission_request_id": str(seed.permission_ids[0]),
        "invocation_id": str(seed.invocation_ids[0]), "runtime_task_id": str(seed.run_id)}]
    assert notifications


@pytest.mark.parametrize("revocation", ["stop", "stop_before_start", "lost_lease"])
async def test_running_approved_command_stops_when_its_authority_is_revoked(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revocation,
):
    from datetime import timedelta
    from app.services import agent_tools, session_permission_runtime
    from app.services.code_execution import service
    from app.services.session_control_input import recover_stale_cancel_control_inputs_once

    seed, effect, notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    started, stopped, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    leases = []
    original = session_permission_runtime.permission_execution_lease
    original_execute = agent_tools.execute_session_permission_tool

    async def before_start(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_execute(*args, **kwargs)

    @asynccontextmanager
    async def capture(*args, **kwargs):
        async with original(*args, **kwargs) as lease:
            leases.append(lease)
            yield lease

    async def operation(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        effect.write_text("must never run\n")

    monkeypatch.setattr(session_permission_runtime, "permission_execution_lease", capture)
    monkeypatch.setattr(service, "_execute_selected_command", operation)
    if revocation == "stop_before_start":
        monkeypatch.setattr(agent_tools, "execute_session_permission_tool", before_start)
    async with _mounted_client(app_user_sessionmaker, seed) as client:
        approval = asyncio.create_task(client.post(_url(seed), json={"action": "allow_once"}))
        try:
            await asyncio.wait_for(started.wait(), 15)
            if revocation.startswith("stop"):
                response = await client.post(
                    f"/api/agents/{seed.agent_id}/sessions/{seed.session_id}/runs/{seed.run_id}/cancel",
                    json={"idempotency_key": f"stop-{uuid.uuid4()}"},
                )
                assert response.status_code == 200, response.text
                async with app_user_sessionmaker() as db:
                    await pin_rls_tenant_context(db, seed.tenant_id)
                    await recover_stale_cancel_control_inputs_once(db, worker_id="test-cancel-recovery",
                        stale_after=timedelta(0), tenant_id=seed.tenant_id)
                    await db.commit()
            else:
                await leases[0].connection.close()
            release.set()
            response = await asyncio.wait_for(approval, 15)
            assert response.status_code == 200, response.text
        finally:
            if not approval.done():
                approval.cancel()
            await asyncio.gather(approval, return_exceptions=True)
    invocation, task, content = await _result(app_user_sessionmaker, seed)
    assert not effect.exists()
    if revocation != "stop_before_start":
        assert stopped.is_set() and "environment_source_authority_" in content
    else:
        assert "approved_tool_execution_failed" in content
    if revocation.startswith("stop"):
        assert task.status == "killed" and not notifications
    else:
        assert "environment_source_authority_unavailable" in content
    assert invocation.result_event_id is not None


@pytest.mark.parametrize("invalid", ["different_lease", "lost_lease", "requester", "permission", "pause", "running", "completed"])
async def test_permission_binding_rejects_another_owner_or_changed_run(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, invalid,
):
    from app.services.permission_command_environment import PermissionCommandBinding
    from app.services.permission_execution import permission_execution_lease
    from app.services.execution_environments import EnvironmentCommandScope, EnvironmentError
    from app.services.session_control_input import accept_tool_permission_response, apply_permission_response_control_input
    from app.services.shared_group_authority import group_permission_execution

    seed, _effect, _notifications = await _command(owner_sessionmaker, monkeypatch, tmp_path)
    async with app_user_sessionmaker() as db:
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
        await db.commit()
        async with permission_execution_lease(db, seed.permission_ids[0]) as lease:
            binding = PermissionCommandBinding(authority, invocation.id, invocation.run_id,
                seed.permission_ids[0], invocation.provider_tool_use_id, lease)
            scope = EnvironmentCommandScope(seed.tenant_id, seed.agent_id, seed.user_id, "test", seed.run_id,
                source_binding=binding, session_id=seed.session_id)
            task = await db.get(RuntimeTask, seed.run_id)
            with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id):
                assert (await binding.authorize(db, scope)).id == seed.user_id
                assert (await binding.revalidate(db, scope)).id == seed.user_id
            if invalid == "different_lease":
                async with permission_execution_lease(db, uuid.uuid4()) as other:
                    binding = replace(binding, lease=other)
                    for validate in (binding.authorize, binding.revalidate):
                        with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                                pytest.raises(EnvironmentError, match="environment_source_authority_revoked"):
                            await validate(db, scope)
                    assert not binding.owns_terminal_task(task)
                return
            if invalid == "lost_lease":
                await lease.connection.close()
            elif invalid == "requester":
                task.metadata_json = {**task.metadata_json, "root_user_id": str(uuid.uuid4())}
            elif invalid == "permission":
                binding = replace(binding, permission_request_id=uuid.uuid4())
            elif invalid == "pause":
                task.metadata_json = {**task.metadata_json, "interactive_pause": {
                    "schema": "hive.tool_permission_wait.v1", "invocation_ids": [str(uuid.uuid4())]}}
            else:
                task.status = invalid
            await db.commit()
            for validate in (binding.authorize, binding.revalidate):
                with group_permission_execution(authority=authority, invocation=invocation, control_id=control_id), \
                        pytest.raises(EnvironmentError, match="environment_source_authority_"):
                    await validate(db, scope)
            assert not binding.owns_terminal_task(task)
