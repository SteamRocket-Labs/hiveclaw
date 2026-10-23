"""Real original-work/RLS path with a synthetic, local SDK resource boundary."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.database import get_db, pin_rls_tenant_context
from tests.integration import conftest as integration
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(scope="module")
def pg_container():
    yield from integration.pg_container.__wrapped__()


@pytest.fixture(scope="module")
def migrated_pg_url(pg_container):
    return integration.migrated_pg_url.__wrapped__(pg_container)


@pytest.fixture
def synthetic_sdk(monkeypatch, tmp_path):
    """No SDK transport, Docker, browser or provider resource is constructed."""
    calls, boxes, faults = [], {}, set()

    class Files:
        def __init__(self, root):
            self.root = root

        async def write_file(self, path, data, **_kwargs):
            target = self.root / path.removeprefix("/hive/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        async def read_bytes(self, path):
            return (self.root / path.removeprefix("/hive/")).read_bytes()

    class Box:
        def __init__(self, metadata):
            self.id = str(uuid4())
            self.root = tmp_path / self.id
            self.root.mkdir()
            self.metadata, self.state = metadata, "Running"
            self.files, self.commands = Files(self.root), self
            boxes[self.id] = self

        async def run(self, command, *, opts=None):
            calls.append(("command", self.id, command))
            opts = opts or SimpleNamespace(envs=None, working_directory=None)
            argv = [value.replace("/hive/", str(self.root) + "/") for value in command]
            if argv[:2] == ["bash", "-lc"]:
                argv[1] = "-c"  # The synthetic child has no machine login profile.
            child_env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home")}
            child_env.update(getattr(opts, "envs", None) or {})
            child_env["HOME"] = str(self.root / "home")
            child_env["TMPDIR"] = str(self.root / "tmp")
            assert "synthetic-control-key-not-for-workload" not in json.dumps(child_env)
            cwd = (getattr(opts, "working_directory", None) or "/hive/").replace("/hive/", str(self.root) + "/")
            child = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=child_env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(child.communicate(), 10)
            return SimpleNamespace(id=str(uuid4()), exit_code=child.returncode, error=None, complete=object(),
                logs=SimpleNamespace(stdout=[SimpleNamespace(text=stdout.decode())],
                    stderr=[SimpleNamespace(text=stderr.decode())]))

        async def get_info(self):
            return SimpleNamespace(id=self.id, status=SimpleNamespace(state=self.state), metadata=self.metadata)

        async def pause(self):
            calls.append(("pause", self.id))
            self.state = "Paused"
            if "pause_ack_lost" in faults:
                raise RuntimeError("Synthetic pause acknowledgement lost")

        async def close(self):
            calls.append(("close", self.id))

    class Sandbox:
        @staticmethod
        async def create(**kwargs):
            assert kwargs["network_policy"].default_action == "deny"
            assert "env" not in kwargs and "volumes" not in kwargs
            box = Box(kwargs["metadata"])
            calls.append(("create", box.id))
            if "create_ack_lost" in faults:
                raise RuntimeError("Synthetic creation acknowledgement lost")
            return box

        @staticmethod
        async def resume(resource_id, **_kwargs):
            box = boxes[resource_id]
            assert box.state == "Paused"
            box.state = "Running"
            calls.append(("resume", resource_id))
            return box

    class Manager:
        @staticmethod
        async def create(**_kwargs):
            return Manager()

        async def get_sandbox_info(self, resource_id):
            if boxes[resource_id].state == "Terminated":
                error = RuntimeError("Synthetic resource absent")
                error.status_code = 404
                raise error
            return await boxes[resource_id].get_info()

        async def kill_sandbox(self, resource_id):
            calls.append(("kill", resource_id))
            boxes[resource_id].state = "Terminated"

        async def close(self):
            pass

    modules = {"opensandbox": {"Sandbox": Sandbox}, "opensandbox.manager": {"SandboxManager": Manager},
        "opensandbox.config": {"ConnectionConfig": lambda **kw: SimpleNamespace(**kw)},
        "opensandbox.models": {"NetworkPolicy": lambda **kw: SimpleNamespace(**kw)},
        "opensandbox.models.execd": {"RunCommandOpts": lambda **kw: SimpleNamespace(**kw)},
        "opensandbox.transport": {"RetryPolicy": SimpleNamespace(disabled=lambda: None)}}
    for name, values in modules.items():
        module = ModuleType(name)
        vars(module).update(values)
        monkeypatch.setitem(sys.modules, name, module)
    old_version = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "1.1.1" if name == "opensandbox" else old_version(name))
    profile = {"schema": "hive.compute-profile.v1", "server_url": "http://127.0.0.1:1",
        "image": "synthetic/workload@sha256:" + "a" * 64, "sdk_version": "1.1.1",
        "isolation": "synthetic-test-only", "conformance_sha256": "b" * 64,
        "resource": {"cpu": "500m", "memory": "256Mi"}, "ttl_seconds": 120}
    path = tmp_path / "synthetic-profile.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setenv("HIVE_OPENSANDBOX_PROFILE_PATH", str(path))
    monkeypatch.setenv("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setenv("HIVE_OPENSANDBOX_API_KEY", "synthetic-control-key-not-for-workload")
    def snapshot():
        return {"calls": calls, "boxes": [{"id": box.id, "metadata": box.metadata, "state": box.state}
            for box in boxes.values()]}

    def restore(saved):
        calls[:] = [tuple(row) for row in saved["calls"]]
        boxes.clear()
        for row in saved["boxes"]:
            box = Box.__new__(Box)
            box.id, box.metadata, box.state = row["id"], row["metadata"], row["state"]
            box.root = tmp_path / box.id
            assert box.root.is_dir()
            box.files, box.commands = Files(box.root), box
            boxes[box.id] = box

    return SimpleNamespace(calls=calls, boxes=boxes, profile=path, faults=faults, snapshot=snapshot, restore=restore)


async def _process_compute_worker(seed, rounds, tmp_path, app_user_engine, synthetic_sdk, *, phase):
    """Kill/join one real worker at a settled boundary, then start a new process."""
    control = tmp_path / "process-input.json"
    control.write_text(json.dumps({"run_id": str(seed["run_id"]), "data_root": str(seed["data_root"]),
        "resource_root": str(tmp_path), "rounds": rounds}))
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH",
        "SECRET_KEY", "JWT_SECRET_KEY", "SECRETS_MASTER_KEY", "AGENT_TEMPLATE_DIR") if key in os.environ}
    env.update(DATABASE_URL=app_user_engine.url.render_as_string(hide_password=False),
        REDIS_URL="redis://127.0.0.1:1/15", AGENT_DATA_DIR=str(seed["data_root"]),
        PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    output = tmp_path / f"process-{phase}.stdout.log"
    with (tmp_path / f"process-{phase}.stderr.log").open("wb") as errors:
        worker = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m",
            "tests.integration.persistent_compute_worker", str(control), phase,
            cwd=Path(__file__).resolve().parents[2], env=env,
            stdout=asyncio.subprocess.PIPE, stderr=errors)
        observed = b""
        try:
            if phase == "first":
                observed = await asyncio.wait_for(worker.stdout.readline(), 60)
                assert json.loads(observed) == {"ready_before_second_model": True, "pid": worker.pid}
                worker.kill()
                assert await asyncio.wait_for(worker.wait(), 10) == -9
                remainder, _ = await asyncio.wait_for(worker.communicate(), 10)
                observed += remainder
            else:
                observed, _ = await asyncio.wait_for(worker.communicate(), 90)
                assert worker.returncode == 0, (tmp_path / f"process-{phase}.stderr.log").read_text()[-3000:]
        finally:
            if worker.returncode is None:
                worker.kill()
            await asyncio.wait_for(worker.communicate(), 10)
            output.write_bytes(observed)
    saved = json.loads((tmp_path / "process-resource.json").read_text())
    synthetic_sdk.restore(saved["resource"])
    (tmp_path / f"process-{phase}-receipt.json").write_text(json.dumps({"pid": worker.pid,
        "exit_code": worker.returncode, "reaped": True, "phase": phase}))
    return SimpleNamespace(requests=saved["requests"], pid=worker.pid)


@pytest.mark.parametrize("fault", [None, "unapproved", "create_ack_lost", "pause_ack_lost", "worker_restart", "new_work", "process_restart"])
async def test_original_work_reuses_paused_compute_and_consumes_canonical_file(
    owner_sessionmaker, app_user_sessionmaker, app_user_engine, monkeypatch, tmp_path, synthetic_sdk, fault,
):
    import app.database as database
    import app.runtime.invoker as invoker
    import app.services.llm_client as llm
    from app.api.environments import router
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.execution_environment import EnvironmentLease
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.user import User
    from app.services import agent_context, agent_manager, agent_tools
    from app.services.agent_manager import AgentManager
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.web_chat_runtime import execute_web_chat_run

    process_restart = fault == "process_restart"
    restart, new_work = fault == "worker_restart" or process_restart, fault == "new_work"
    if restart or new_work:
        fault = None
    seed = await _seed_run(owner_sessionmaker, tmp_path)
    factory, original = database.async_session, llm.create_llm_client
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
                elif value is original:
                    monkeypatch.setattr(module, name, lambda **kw: (_ for _ in ()).throw(AssertionError("real model forbidden")))
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    monkeypatch.setattr(agent_manager, "settings", get_settings())
    monkeypatch.setenv("HIVE_CODE_EXEC_PROVIDER", "opensandbox")
    if fault == "unapproved":
        monkeypatch.delenv("HIVE_OPENSANDBOX_APPROVED_PROFILE_SHA256")
    elif fault:
        synthetic_sdk.faults.add(fault)
    class Memory:
        async def stream(self, **kw):
            return SimpleNamespace(content='{"summary":"Synthetic compute continuity"}')

        async def close(self):
            pass
    monkeypatch.setattr(llm, "create_llm_client_from_config", lambda *a, **kw: Memory())
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        owner = await db.get(User, seed["user_id"])
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability="workspace.command.execute",
            allowed=True, requires_approval=False))
        await AgentManager().initialize_agent_files(db, agent, personality="Compute continuity", original_author_id=owner.id)
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-model-key")
        model.max_input_tokens = 256_000
        await db.commit()

    def call(key, command):
        return {"tool_calls": [{"id": key, "type": "function", "function": {"name": "run_command",
            "arguments": json.dumps({"command": command, "timeout": 10})}}], "finish_reason": "tool_calls"}

    rounds = [
        call("compute-first", "printf CANONICAL_COMPUTE > saved.txt; printf PRIVATE_CACHE > \"$HOME/cache\"; echo FIRST_COMPUTE"),
        call("compute-continued", "cat saved.txt \"$HOME/cache\"; printf _CONTINUED >> saved.txt"),
        {"content": "DELIVERABLE: workspace/saved.txt", "finish_reason": "stop"},
    ]
    if fault == "pause_ack_lost":
        rounds.pop(1)  # Do not invent a model's recovery decision after an unknown command.
    if fault is None:
        rounds.insert(-1, {"tool_calls": [{"id": "read-compute-deliverable", "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "workspace/saved.txt"})}}],
            "finish_reason": "tool_calls"})
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import SimulatedWorkerCrash

    script = ScriptedLLMClient([rounds[0], SimulatedWorkerCrash()] if restart else rounds)
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kw: script)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        if process_restart:
            script = await _process_compute_worker(seed, rounds, tmp_path, app_user_engine, synthetic_sdk, phase="first")
            first_pid = script.pid
        elif restart:
            with pytest.raises(SimulatedWorkerCrash):
                await execute_web_chat_run(seed["run_id"])
        else:
            await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    if restart:
        from datetime import UTC, datetime, timedelta
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService

        assert (seed["agent_root"] / "workspace/saved.txt").read_text() == "CANONICAL_COMPUTE"
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            task = await db.get(RuntimeTask, seed["run_id"])
            assert task.status == "running" and task.claim_version == 1
            if process_restart:
                from app.models.session_v2 import SessionModelResult, SessionToolInvocation

                models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == task.id)))
                calls = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == task.id)))
                assert len(models) == 1 and models[0].state == "round_committed"
                assert len(calls) == 1 and calls[0].effect_state == "effect_committed"
                first_lease = await db.scalar(select(EnvironmentLease))
                assert first_lease.status == "released" and first_lease.outcome["resource_state"] == "paused"
                assert first_lease.outcome["resource_lifecycle"]["worker"]["pid"] == first_pid
            task.claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
            claims = await RuntimeTaskClaimService(db=db, worker_id="compute-worker-2", lease_seconds=600).claim_available()
            claim = next(row for row in claims if row.id == seed["run_id"])
            assert claim.claim_version == 2
            await db.commit()
        script = ScriptedLLMClient(rounds[1:])
        fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="compute-worker-2", claim_version=2)
        try:
            if process_restart:
                script = await _process_compute_worker(seed, rounds[1:], tmp_path, app_user_engine, synthetic_sdk, phase="resume")
                assert script.pid != first_pid
            else:
                await execute_web_chat_run(seed["run_id"])
        finally:
            reset_runtime_task_fence(fence)
        assert "FIRST_COMPUTE" in json.dumps(script.requests[0]["messages"])
    if fault:
        from app.models.execution_environment import ExecutionEnvironment

        assert not (seed["agent_root"] / "workspace/saved.txt").exists()
        messages = json.dumps(script.requests[-1]["messages"])
        assert "compute_profile_not_approved" in messages if fault == "unapproved" else "compute_execution_unconfirmed" in messages
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            environment = await db.scalar(select(ExecutionEnvironment))
            leases = list(await db.scalars(select(EnvironmentLease)))
            if fault == "create_ack_lost":
                assert environment.resource_binding["state"] == "creation_unknown"
                assert environment.resource_binding["resource_id"] is None
                assert len(leases) == 1 and leases[0].status == "active"
                assert leases[0].outcome["status"] == "unknown"
                assert "environment_reconciliation_required" in messages
                assert [c[0] for c in synthetic_sdk.calls].count("create") == 1
            elif fault == "pause_ack_lost":
                assert environment.resource_binding is None
                assert len(leases) == 1 and leases[0].status == "released"
                assert leases[0].outcome["status"] == "unknown"
                assert leases[0].outcome["resource_state"] == "stopped"
                assert [c[0] for c in synthetic_sdk.calls].count("kill") == 1
            else:
                assert environment.resource_binding is None and not synthetic_sdk.calls
                assert len(leases) == 2 and all(row.status == "released" for row in leases)
        return
    assert (seed["agent_root"] / "workspace/saved.txt").read_text() == "CANONICAL_COMPUTE_CONTINUED"
    assert "PRIVATE_CACHE" in json.dumps(script.requests[-1]["messages"])
    assert [c[0] for c in synthetic_sdk.calls].count("create") == 1
    assert [c[0] for c in synthetic_sdk.calls].count("resume") == 1
    assert [c[0] for c in synthetic_sdk.calls].count("pause") == 2
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="compute-boundary", canonical_validator=processor.validate,
        process_callback=processor, task_types=("web_chat_turn",))
    assert boundary["delivered"] == 1, boundary
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        leases = list(await db.scalars(select(EnvironmentLease)))
        assert len(leases) == 2 and all(row.status == "released" for row in leases)
        assert all(row.outcome["resource_state"] == "paused" for row in leases)
        assert (await db.get(RuntimeTask, seed["run_id"])).status == "completed"
    async def identity():
        return owner

    async def db_dependency():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
    web = FastAPI()
    web.include_router(router, prefix="/api")
    from app.api.files import router as files_router

    web.include_router(files_router, prefix="/api")
    web.dependency_overrides[get_current_user] = identity
    web.dependency_overrides[get_db] = db_dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
        view = await client.get(f"/api/agents/{agent.id}/environment", params={
            "expected_tenant_id": str(seed["tenant_id"]), "expected_user_id": str(owner.id)})
        assert view.status_code == 200 and view.json()["environment"]["active_commands"] == 0
        assert view.json()["environment"]["capabilities"]["persistent_compute"] is True
        from app.models.chat_artifact import ChatArtifact

        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            versions = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == seed["run_id"],
                ChatArtifact.path == "workspace/saved.txt")))
            selected = [row for row in versions if (row.snapshot_json or {}).get("selection_model_result_id")]
            assert len(selected) == 1
            artifact = selected[0]
            assert artifact.snapshot_json["content_input_receipt_id"]
            delivered = await client.get(f"/api/agents/{agent.id}/files/artifacts/{artifact.id}/content")
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["content"] == "CANONICAL_COMPUTE_CONTINUED"
    assert "synthetic-control-key-not-for-workload" not in json.dumps(script.requests)
    if new_work:
        from uuid import UUID
        from app.models.chat_session import ChatSession
        from app.models.execution_environment import ExecutionEnvironment
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.session_live_input import submit_live_human_input

        original_resource = next(iter(synthetic_sdk.boxes))
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            receipt = await submit_live_human_input(db=db, agent=await db.get(Agent, seed["agent_id"]),
                user=await db.get(User, seed["user_id"]), session=await db.get(ChatSession, seed["session_id"]),
                content="Continue the saved report as a new work item, using a fresh private cache.",
                source="web", requested_kind="start_turn")
            assert receipt["admission_state"] == "admitted" and receipt["run"]
            next_run_id = UUID(receipt["run"]["run_id"])
            assert next_run_id != seed["run_id"]
            claims = await RuntimeTaskClaimService(db=db, worker_id="new-work-worker", lease_seconds=600).claim_available()
            claim = next(row for row in claims if row.id == next_run_id)
            assert claim.claim_version == 1
            await db.commit()
        script = ScriptedLLMClient([
            call("fresh-compute", 'test ! -e "$HOME/cache" && printf _NEW_WORK >> saved.txt && cat saved.txt'),
            {"tool_calls": [{"id": "read-new-deliverable", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": "workspace/saved.txt"})}}],
                "finish_reason": "tool_calls"},
            {"content": "DELIVERABLE: workspace/saved.txt", "finish_reason": "stop"},
        ])
        fence = set_runtime_task_fence(task_id=next_run_id, worker_id="new-work-worker", claim_version=1)
        try:
            await execute_web_chat_run(next_run_id)
        finally:
            reset_runtime_task_fence(fence)
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="new-work-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert boundary["delivered"] == 1, boundary
        expected = "CANONICAL_COMPUTE_CONTINUED_NEW_WORK"
        assert (seed["agent_root"] / "workspace/saved.txt").read_text() == expected
        assert synthetic_sdk.boxes[original_resource].state == "Terminated"
        lifecycle = [(row[0], row[1]) for row in synthetic_sdk.calls if row[0] in {"create", "resume", "kill"}]
        fresh_resource = next(key for key in synthetic_sdk.boxes if key != original_resource)
        assert lifecycle == [("create", original_resource), ("resume", original_resource),
            ("kill", original_resource), ("create", fresh_resource)]
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert (await db.get(RuntimeTask, next_run_id)).status == "completed"
            leases = list(await db.scalars(select(EnvironmentLease)))
            assert len(leases) == 3 and all(row.status == "released" for row in leases)
            environment = await db.scalar(select(ExecutionEnvironment))
            assert environment.resource_binding["context"]["runtime_task_id"] == str(next_run_id)
            assert environment.resource_binding["resource_id"] == fresh_resource
            versions = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.runtime_task_id == next_run_id,
                ChatArtifact.path == "workspace/saved.txt")))
            selected = [row for row in versions if (row.snapshot_json or {}).get("selection_model_result_id")]
            assert len(selected) == 1 and selected[0].snapshot_json["content_input_receipt_id"]
            selected_id = selected[0].id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://test") as client:
            delivered = await client.get(f"/api/agents/{agent.id}/files/artifacts/{selected_id}/content")
            assert delivered.status_code == 200 and delivered.json()["content"] == expected
        assert "synthetic-control-key-not-for-workload" not in json.dumps(script.requests)


async def test_known_resource_reconciliation_requires_dead_worker_and_preserves_unknown_command(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, synthetic_sdk,
):
    import subprocess
    import app.database as database
    from app.models.execution_environment import EnvironmentLease, ExecutionEnvironment
    from app.services.execution_environments import EnvironmentCommandScope, EnvironmentError, control_environment, project_environment
    from opensandbox import Sandbox
    from opensandbox.models import NetworkPolicy

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    monkeypatch.setattr(database, "async_session", app_user_sessionmaker)
    scope = EnvironmentCommandScope(seed["tenant_id"], seed["agent_id"], seed["user_id"], "lost-worker", None)
    await scope._acquire("opensandbox", 1)
    box = await Sandbox.create(metadata={"hive.environment": str(scope.environment_id),
        "hive.profile": hashlib.sha256(synthetic_sdk.profile.read_bytes()).hexdigest()},
        network_policy=NetworkPolicy(default_action="deny"))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        environment = await db.get(ExecutionEnvironment, scope.environment_id)
        environment.resource_binding = {"resource_id": box.id, "state": "unknown",
            "profile_sha256": hashlib.sha256(synthetic_sdk.profile.read_bytes()).hexdigest()}
        await db.commit()
    await scope.record_creation_intent()
    scope.resource_state, scope.outcome = "unknown", {"status": "unknown"}
    await scope.release()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        view = await project_environment(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], include_recovery=True)
        assert view["recovery"]["can_reconcile"] is False
        with pytest.raises(EnvironmentError, match="worker_still_present"):
            await control_environment(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], action="reconcile",
                expected_revision=view["revision"], expected_lease_id=scope.lease_id)
        assert box.state == "Running"
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"], env={"PATH": "/usr/bin:/bin"})
    assert child.wait(timeout=10) == 0
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        lease = await db.get(EnvironmentLease, scope.lease_id)
        lifecycle = lease.outcome["resource_lifecycle"]
        lease.outcome = {**lease.outcome, "resource_lifecycle": {**lifecycle,
            "worker": {**lifecycle["worker"], "pid": child.pid}}}
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        view = await project_environment(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], include_recovery=True)
        assert view["recovery"]["can_reconcile"] is True and view["next_action"] == "reconcile"
        result = await control_environment(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], action="reconcile",
            expected_revision=view["revision"], expected_lease_id=scope.lease_id)
        assert result["observed_state"] == "stopped" and result["active_commands"] == 0
        lease = await db.get(EnvironmentLease, scope.lease_id)
        assert lease.status == "released" and lease.outcome["resource_state"] == "stopped"
        assert lease.outcome["compute_reconciliation"]["command_outcome"] == "unknown"
        assert (await db.get(ExecutionEnvironment, scope.environment_id)).resource_binding is None
        assert box.state == "Terminated"
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, uuid4())
        assert list(await db.scalars(select(ExecutionEnvironment))) == []
