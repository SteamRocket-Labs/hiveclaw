"""Input admission cannot borrow another turn's command-resource identity."""

import asyncio
import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.execution_environment import EnvironmentLease
from app.models.tenant_channel_config import TenantChannelConfig  # noqa: F401 - external principal FK metadata
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("identity", ["bound", "unbound", "revoked", "inflight", "nested_revoked"])
async def test_admission_command_uses_current_source_and_restores_previous_turn(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    identity,
):
    import app.database as database
    from app.models.channel_config import ChannelConfig
    from app.models.chat_session import ChatSession
    from app.models.external_principal import ExternalPrincipal
    from app.models.user import User
    from app.runtime.hook_runner import GovernedHookRunner, HookRunnerPolicy, HookSpec
    from app.runtime.hooks import HookContext, HookEvent
    from app.services.execution_environments import (
        EnvironmentCommandFrame,
        command_environment_context,
        environment_command_scope,
    )
    from app.services.external_principal_service import load_external_runtime_actor
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority
    from app.services.code_execution.service import execute_agent_command

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
    config_id, principal_id, session_id, input_id, requester_id = (uuid4() for _ in range(5))
    async with owner_sessionmaker() as db:
        db.add(
            User(
                id=requester_id,
                tenant_id=seed["tenant_id"],
                username=f"admission-{uuid4().hex}",
                email=f"{uuid4().hex}@admission.example.com",
                display_name="Current requester",
                role="org_admin",
                password_hash="x",
            )
        )
        db.add(
            ChannelConfig(
                id=config_id,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                channel_type="feishu",
                app_id="cli_admission",
                app_secret="synthetic-admission",
                is_configured=True,
            )
        )
        await db.flush()
        db.add(
            ExternalPrincipal(
                id=principal_id,
                tenant_id=seed["tenant_id"],
                provider="feishu",
                installation_ref=str(config_id),
                channel_config_id=config_id,
                subject_id="ou_admission",
                status="active",
                linked_user_id=requester_id if identity != "unbound" else None,
                binding_method="feishu_qr",
                linked_at=datetime.now(UTC) if identity != "unbound" else None,
                binding_verified_at=datetime.now(UTC) if identity != "unbound" else None,
            )
        )
        await db.flush()
        db.add(
            ChatSession(
                id=session_id,
                tenant_id=seed["tenant_id"],
                agent_id=seed["agent_id"],
                user_id=requester_id if identity != "unbound" else None,
                external_principal_id=principal_id,
                source_channel="feishu",
                external_conv_id="private-admission",
            )
        )
        await db.commit()
    work, home = tmp_path / "hook-work", tmp_path / "hook-home"
    work.mkdir()
    home.mkdir()
    events = []
    runner = GovernedHookRunner(
        policy=HookRunnerPolicy(enabled=True, work_dir=work, allowed_env_vars={"HOME", "PATH"}),
        transcript_writer=events.append,
    )
    program = "import time; f=open('admission.txt','w');\nfor _ in range(150): f.write('x'); f.flush(); time.sleep(.02)"
    command = (
        f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(program)}"
        if identity == "inflight"
        else "printf ADMISSION_EFFECT > admission.txt"
    )
    handler = runner.build_handler(
        HookSpec(
            key="source-bound-admission",
            event=HookEvent.USER_PROMPT_SUBMIT,
            type="command",
            command=command,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            failure_mode="advisory",
            timeout_seconds=10,
        )
    )

    async def unlink():
        async with owner_sessionmaker() as db:
            (await db.get(ExternalPrincipal, principal_id)).linked_user_id = None
            await db.commit()

    async def hook(**payload):
        if identity == "revoked":
            await unlink()
        context = HookContext(
            event=HookEvent.USER_PROMPT_SUBMIT,
            agent_id=uuid4(),
            metadata={
                "user_id": str(seed["user_id"]),
                "tenant_id": str(uuid4()),
                "runtime_task_id": str(seed["run_id"]),
                "hook_run_id": payload["hook_run_id"],
            },
        )
        if identity == "nested_revoked":
            # A nested runtime/backend frame for the same actor must retain
            # the original input's live identity constraint.
            child = SimpleNamespace(
                tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], user_id=requester_id, runtime_task_id=None
            )
            with command_environment_context(child):
                async with environment_command_scope(child, "nested-admission"):
                    await unlink()
                    return await handler(context)
        if identity != "inflight":
            return await handler(context)
        pending = asyncio.create_task(handler(context))
        try:
            async with asyncio.timeout(5):
                while not (work / "admission.txt").exists():
                    await asyncio.sleep(0.02)
            await unlink()
            return await asyncio.wait_for(pending, 5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        actor = await load_external_runtime_actor(
            db,
            tenant_id=seed["tenant_id"],
            principal_id=principal_id,
            expected_user_id=requester_id if identity != "unbound" else None,
        )
        authority = await resolve_session_mutation_authority(
            db, user=actor, agent_id=seed["agent_id"], session_id=session_id, action="mutate_session_input"
        )
        await accept_human_input(
            db,
            authority=authority,
            intent={
                "kind": "start_turn",
                "input_id": str(input_id),
                "idempotency_key": str(input_id),
                "session_id": str(session_id),
                "content_parts": [{"type": "text", "text": "Keep this original input available."}],
            },
        )
        await db.commit()
        with command_environment_context(
            EnvironmentCommandFrame(seed["tenant_id"], seed["agent_id"], seed["user_id"], seed["run_id"])
        ):
            outcome = await run_user_prompt_admission(
                db, authority=authority, input_id=input_id, worker_id="source-admission", hook_executor=hook
            )
            assert outcome.state == "admitted"
            parent = await execute_agent_command(
                ["bash", "-lc", "printf PARENT_EFFECT > parent.txt"],
                work_dir=work,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                timeout=5,
            )
            assert parent.error is None and (work / "parent.txt").read_text() == "PARENT_EFFECT"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        leases = list(await db.scalars(select(EnvironmentLease)))
        assert all(row.status == "released" for row in leases)
        parent_leases = [row for row in leases if row.runtime_task_id == seed["run_id"]]
        incoming = [row for row in leases if row.runtime_task_id is None]
        assert len(parent_leases) == 1 and parent_leases[0].principal_id == seed["user_id"]
        if identity in {"bound", "inflight"}:
            assert len(incoming) == 1 and incoming[0].principal_id == requester_id, (
                "Admission must retain its actual requester"
            )
            assert incoming[0].outcome["source"]["input_id"] == str(input_id)
            assert (work / "admission.txt").exists()
        else:
            assert incoming == [] and not (work / "admission.txt").exists()
        if identity == "inflight":
            assert incoming[0].outcome["resource_state"] == "stopped"
            before = (work / "admission.txt").read_bytes()
            await asyncio.sleep(0.1)
            assert before == (work / "admission.txt").read_bytes() and len(before) < 150
    (tmp_path / "admission-source-observation.json").write_text(
        json.dumps(
            {
                "identity": identity,
                "input_id": str(input_id),
                "requester_id": str(requester_id),
                "previous_requester": str(seed["user_id"]),
                "events": events,
                "leases": [
                    {
                        "id": str(row.id),
                        "principal_id": str(row.principal_id),
                        "runtime_task_id": str(row.runtime_task_id),
                        "outcome": row.outcome,
                    }
                    for row in leases
                ],
            },
            default=str,
            indent=2,
        )
    )
