"""A delivered snapshot stays cheap after real, context-rich Web model rounds."""

import asyncio
import cProfile
import io
import json
import pstats
import time
import uuid
from collections import Counter
from contextlib import aclosing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient, _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_context_rich_web_rounds_do_not_inflate_delivered_file_reads(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.api import files
    from app.config import get_settings
    from app.core.security import get_current_user
    from app.database import get_db, pin_rls_tenant_context
    from app.memory.write_gate import MemoryWriteDecision
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_session import ChatSession
    from app.models.invocation_span import InvocationSpan
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.models.user import User
    from app.runtime import invoker
    from app.services import agent_context, agent_manager, agent_tools, session_model_inputs, session_model_round
    from app.services.agent_manager import AgentManager
    from app.services.prompt_cache import apply_cache_hints
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import get_secrets_provider
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.source_withholding_helpers import legacy_model_requests
    from tests.services.test_session_input_control_v2 import _accepted_input, _authority

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="Remember the sales reporting convention.")
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    for module, attr in (
        (agent_tools, "WORKSPACE_ROOT"),
        (agent_context, "PERSISTENT_DATA"),
        (agent_context, "TOOL_WORKSPACE"),
    ):
        monkeypatch.setattr(module, attr, seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_manager, "settings", settings)
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        await AgentManager().initialize_agent_files(
            db, agent, personality="Prepare sales reports from authorized inputs.", original_author_id=seed["user_id"]
        )
        (await db.get(User, seed["user_id"])).role = "member"
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-report-model")
        model.max_input_tokens = 256_000
        model.provider = "minimax"
        for capability in ("workspace.file.write", "agent.memory.write", "agent.memory.read"):
            db.add(
                CapabilityPolicy(
                    tenant_id=seed["tenant_id"], capability=capability, allowed=True, requires_approval=False
                )
            )
        await db.commit()

    async def reference_review(content, *, category, **_kwargs):
        return MemoryWriteDecision(
            original_content=content,
            content=content,
            category=category,
            sensitivity="PL1_public",
            metadata={"threat_gate_method": "controlled_reference_reviewer"},
        )

    monkeypatch.setattr("app.memory.explicit_overlay.prepare_memory_write_with_llm", reference_review)

    n = SimpleNamespace(
        tenant=seed["tenant_id"],
        user=seed["user_id"],
        agent=seed["agent_id"],
        session=seed["session_id"],
        run=seed["run_id"],
        root=seed["agent_root"],
        service=agent_tools._get_tool_runtime_service(),
    )
    saved, _ = await memory_call(
        n,
        app_user_sessionmaker,
        "save_memory",
        {"category": "project", "content": "Sales reports use Markdown and report the total sales."},
    )
    assert "Saved memory" in saved, saved
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, n.run)).status = "completed"
        n.session = uuid.uuid4()
        db.add(ChatSession(id=n.session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.user, source_channel="web"))
        await db.commit()

    # Emulate the old compiler only while writing its immutable requests. All
    # runtime, tool, artifact and subsequent read checks remain the real ones.
    prepare = session_model_round.prepare_model_request
    build = session_model_inputs.build_model_input_bindings
    expected_dependencies = {}

    async def old_inventory(*args, **kwargs):
        manifest = await build(*args, **kwargs)
        for wire, entry in zip(kwargs["wire_messages"], manifest["messages"]):
            if wire.get("role") == "assistant" and isinstance(wire.get("content"), list):
                entry.update(status="unresolved", code="source_content_mismatch", dependency_status="incomplete")
                entry.pop("source_ref", None)
                entry.pop("dependencies", None)
                manifest["status"] = "incomplete"
                manifest["dependency_coverage"] = {"complete": False}
        return manifest

    async def old_request(db, **kwargs):
        if kwargs["round_index"] >= 9:
            request = await prepare(db, **kwargs)
            result = await db.scalar(
                select(SessionModelResult).where(SessionModelResult.provider_request_id == request)
            )
            bindings = result.model_request_snapshot_json["input_bindings"]
            assert bindings["dependency_coverage"] == {"complete": True}
            expected_dependencies[result.id] = deepcopy(bindings["dependencies"]), True
            return request
        messages = kwargs["messages"]
        prefix = messages[0]
        # Independent witness uses the real compiler origin, never a fabricated row.
        witness = prefix
        messages[0] = apply_cache_hints([session_model_inputs_message(prefix)], "minimax")[0]
        with legacy_model_requests(monkeypatch), monkeypatch.context() as compiler:
            compiler.setattr(session_model_inputs, "build_model_input_bindings", old_inventory)
            request = await prepare(db, **kwargs)
        await session_model_inputs.capture_context_message_origins(
            db,
            tenant_id=n.tenant,
            agent_id=n.agent,
            session_id=n.session,
            run_id=kwargs["run_id"],
            result_id=uuid.uuid4(),
            messages=[witness],
        )
        result = await db.scalar(select(SessionModelResult).where(SessionModelResult.provider_request_id == request))
        wire = result.model_request_snapshot_json["wire_request"]["messages"]
        # The complete compiler inventory is the oracle, before the historical
        # recovery under test and before any later run exists.
        canonical_messages = []
        for message, source in zip(wire, [witness, *messages[1:]]):
            ref = source.get("source_ref") if isinstance(source, dict) else source.source_ref
            canonical_messages.append({**message, "source_ref": ref})
        expected = await build(
            db,
            tenant_id=n.tenant,
            agent_id=n.agent,
            session_id=n.session,
            messages=canonical_messages,
            wire_messages=wire,
        )
        assert expected["dependency_coverage"] == {"complete": True}, expected
        expected_dependencies[result.id] = deepcopy(expected["dependencies"]), True
        return request

    def session_model_inputs_message(message):
        from app.services.llm_client import LLMMessage

        return LLMMessage(role=message.role, content=message.content)

    async def current_user():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            return await db.get(User, n.user)

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    monkeypatch.setattr(files, "_load_download_user_from_jwt", lambda **_kwargs: current_user())
    body = "# Sales report\n" + "Sales total: 42.\n" * 190
    measurements = {}
    recovered_dependencies = {}
    artifact_id = None

    def call(name, identity, arguments):
        return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://fixture.local",
        headers={"Authorization": "Bearer fixture-identity"},
    ) as client:
        for run_number in range(1, 4):
            run = uuid.uuid4()
            prompt = f"Prepare sales report {run_number} with eight draft steps, then deliver it."
            async with owner_sessionmaker() as db:
                db.add(
                    RuntimeTask(
                        id=run,
                        tenant_id=n.tenant,
                        task_type="web_chat_turn",
                        status="running",
                        parent_agent_id=n.agent,
                        child_agent_id=n.agent,
                        parent_session_id=str(n.session),
                        child_session_id=str(n.session),
                        root_session_id=str(n.session),
                        root_user_id=n.user,
                        root_runtime_task_id=run,
                        prompt=prompt,
                        claimed_by="r13e-shape-worker",
                        claim_version=1,
                        claim_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                        attempt_count=1,
                        metadata_json={
                            "turn_id": f"turn-{run.hex}",
                            "user_id": str(n.user),
                            "runtime_budget": {
                                "schema": "hive.runtime_budget_binding.v1",
                                "status": "active",
                                "interactive": True,
                            },
                        },
                    )
                )
                await db.flush()
                from app.services.runtime_root_ledger import register_runtime_root_item

                await register_runtime_root_item(
                    db,
                    tenant_id=n.tenant,
                    root_runtime_task_id=run,
                    source_agent_id=n.agent,
                    intent_key=f"web-chat-turn:{run.hex}",
                    work_type="web_chat_turn",
                    target_ref=str(n.session),
                    runtime_task_id=run,
                    root_user_id=n.user,
                    root_session_id=str(n.session),
                    state="running",
                )
                authority = await _authority(db, user_id=n.user, agent_id=n.agent, session_id=n.session)
                admitted = await _accepted_input(
                    db, authority=authority, session_id=n.session, run_id=run, kind="steer_current_turn", content=prompt
                )
                await run_user_prompt_admission(
                    db, authority=authority, input_id=admitted.input_id, worker_id="r13e-shape-worker"
                )
                await queue_admitted_human_input(db, authority=authority, input_id=admitted.input_id)
                await db.commit()
            script = [
                {
                    "content": f"Draft step {i} for report {run_number}.",
                    "tool_calls": [
                        call("get_current_time", f"time-{i}", {}),
                        call(
                            "write_file",
                            f"draft-{i}",
                            {"path": f"workspace/draft-{run_number}.md", "content": f"Draft {i}"},
                        ),
                    ],
                    "finish_reason": "tool_calls",
                }
                for i in range(1, 9)
            ]
            path = f"workspace/report-{run_number}.md"
            script.extend(
                [
                    {
                        "content": "Deliver the final report.",
                        "tool_calls": [call("write_file", "report-write", {"path": path, "content": body})],
                        "finish_reason": "tool_calls",
                    },
                    {"content": f"Report ready: [{path}]({path})", "finish_reason": "stop"},
                ]
            )
            model = ScriptedLLMClient(script)
            monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: model)
            fence = set_runtime_task_fence(task_id=run, worker_id="r13e-shape-worker", claim_version=1)
            try:
                with monkeypatch.context() as compiler:
                    if run_number == 1:
                        compiler.setattr(session_model_round, "prepare_model_request", old_request)
                    await asyncio.wait_for(execute_web_chat_run(run), timeout=180)
            finally:
                reset_runtime_task_fence(fence)
            async with owner_sessionmaker() as db:
                task = await db.get(RuntimeTask, run)
                assert task.status == "completed", task.metadata_json
                assert len(model.requests) == 10
                spans = list(
                    await db.scalars(
                        select(InvocationSpan).where(
                            InvocationSpan.runtime_task_id == run, InvocationSpan.span_type == "kernel_context"
                        )
                    )
                )
                rounds = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run)))
                tools = list(await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == run)))
                shape = {
                    "run": run_number,
                    "rounds": len(rounds),
                    "tools": len(tools),
                    "spans": len(spans),
                    "spans_per_round": len(spans) / len(rounds),
                    "kinds": dict(Counter(s.name for s in spans)),
                    "metadata_bytes": sum(len(json.dumps(s.metadata_json).encode()) for s in spans),
                }
                print("R13E_SHAPE", json.dumps(shape), flush=True)
                assert len(spans) >= 18 * len(rounds), shape
                if run_number == 1:
                    artifacts = list(
                        await db.scalars(
                            select(ChatArtifact).where(ChatArtifact.runtime_task_id == run, ChatArtifact.path == path)
                        )
                    )
                    assert len(artifacts) == 1
                    artifact = artifacts[0]
                    artifact_id = artifact.id
                    producing_requests = {
                        tool.provider_request_id
                        for tool in tools
                        if tool.tool_name == "write_file" and tool.provider_arguments_json.get("path") == path
                    }
                    artifact_models = {
                        result.id for result in rounds if result.provider_request_id in producing_requests
                    }
                    selection = artifact.snapshot_json.get("selection_model_result_id")
                    if selection is not None:
                        artifact_models.add(uuid.UUID(selection))
                    assert artifact_models and artifact_models <= expected_dependencies.keys()
            if run_number == 2:
                continue
            measurements[run_number] = {}
            for endpoint in ("download", "content"):
                statements = []

                def executed(_connection, _cursor, statement, _parameters, _context, _many):
                    statements.append(statement)

                profile = cProfile.Profile()
                started = time.perf_counter()
                cpu_started = time.process_time()
                event.listen(Engine, "after_cursor_execute", executed)
                profile.enable()
                try:
                    response = await client.get(f"/agents/{n.agent}/files/artifacts/{artifact_id}/{endpoint}")
                finally:
                    profile.disable()
                    event.remove(Engine, "after_cursor_execute", executed)
                row = {
                    "run": run_number,
                    "endpoint": endpoint,
                    "statements": len(statements),
                    "seconds": round(time.perf_counter() - started, 4),
                    "cpu_seconds": round(time.process_time() - cpu_started, 4),
                    "source_hash_calls": sum(
                        item.callcount
                        for item in profile.getstats()
                        if not isinstance(item.code, str)
                        and item.code.co_name == "_hash"
                        and item.code.co_filename.endswith("session_model_inputs.py")
                    ),
                    "message_snapshots": sum(
                        item.callcount
                        for item in profile.getstats()
                        if not isinstance(item.code, str)
                        and item.code.co_name == "message_snapshot"
                        and item.code.co_filename.endswith("session_model_inputs.py")
                    ),
                }
                measurements[run_number][endpoint] = row
                print("R13E_SHAPED_READ", json.dumps(row), flush=True)
                output = io.StringIO()
                pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(20)
                print(f"R13E_PROFILE run={run_number} endpoint={endpoint}\n{output.getvalue()}", flush=True)
                output = io.StringIO()
                pstats.Stats(profile, stream=output).strip_dirs().sort_stats("cumulative").print_stats(
                    "historical_model_inputs|session_model_inputs|session_semantic_history|encoder.py|decoder.py", 20
                )
                print(f"R13E_SOURCE_PROFILE run={run_number} endpoint={endpoint}\n{output.getvalue()}", flush=True)
                print("R13E_SQL", json.dumps(dict(Counter(statements))), flush=True)
                assert response.status_code == 200, response.text
                assert (
                    response.content if endpoint == "download" else response.json()["content"].encode()
                ) == body.encode()
            from app.database import pin_rls_tenant_context
            from app.services.session_model_inputs import _origin_dependencies

            async with app_user_sessionmaker() as db:
                await pin_rls_tenant_context(db, n.tenant)
                # Include the artifact's producing/selection models and every
                # historical model in their source closure, not just read cost.
                recovered_dependencies[run_number] = {
                    model_id: await _origin_dependencies(
                        db, tenant_id=n.tenant, agent_id=n.agent, ref=f"session-model-result:{model_id}"
                    )
                    for model_id in expected_dependencies
                }
            assert recovered_dependencies[run_number] == expected_dependencies
            print(
                "R13E_DEPENDENCIES",
                json.dumps(
                    {
                        "run": run_number,
                        "expected_equal": True,
                        "models": len(expected_dependencies),
                        "artifact_models": len(artifact_models),
                        "dependency_counts": [len(value[0]) for value in recovered_dependencies[run_number].values()],
                    }
                ),
                flush=True,
            )
    assert recovered_dependencies[1] == recovered_dependencies[3] == expected_dependencies
    print("R13E_DEPENDENCIES_RUN1_RUN3_EQUAL", True, flush=True)
    for endpoint in measurements[1]:
        assert measurements[3][endpoint]["statements"] <= measurements[1][endpoint]["statements"] + 5, measurements
        assert measurements[3][endpoint]["source_hash_calls"] <= measurements[1][endpoint]["source_hash_calls"] + 10, (
            measurements
        )
        assert measurements[3][endpoint]["message_snapshots"] <= measurements[1][endpoint]["message_snapshots"] + 10, (
            measurements
        )


async def test_missing_frozen_witness_measures_full_session_scan(
    owner_sessionmaker,
    app_user_sessionmaker,
):
    """Measure the known unmatched-witness cost; do not impose a scan or time limit."""
    from app.database import pin_rls_tenant_context
    from app.kernel.engine import _context_message
    from app.services.historical_model_inputs import _complete_origins
    from app.services.session_model_inputs import capture_context_message_origins
    from tests.integration.test_m1g27_model_input_bindings_postgres import _bound_input

    tenant, agent, session, run, _bound = await _bound_input(owner_sessionmaker)
    for run_number in range(1, 4):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            for round_number in range(10):
                message = _context_message(
                    role="system",
                    kind="frozen_prompt",
                    unbound=(),
                    content=f"Captured prefix {run_number}/{round_number}.\n" + "Authorized compiler context.\n" * 500,
                )
                await capture_context_message_origins(
                    db,
                    tenant_id=tenant,
                    agent_id=agent,
                    session_id=session,
                    run_id=run,
                    result_id=uuid.uuid4(),
                    messages=[message],
                )
            await db.commit()
        if run_number == 2:
            continue
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            statements, scanned, matched = [], 0, False

            def executed(_connection, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)

            profile = cProfile.Profile()
            started = time.perf_counter()
            event.listen(db.bind.sync_engine, "after_cursor_execute", executed)
            profile.enable()
            try:
                async with aclosing(
                    _complete_origins(db, tenant_id=tenant, agent_id=agent, session_id=session, kind="frozen_prompt")
                ) as origins:
                    async for _ref, proof in origins:
                        scanned += 1
                        if proof["message"] == {"role": "system", "content": "No captured witness has these bytes."}:
                            matched = True
                            break
            finally:
                profile.disable()
                event.remove(db.bind.sync_engine, "after_cursor_execute", executed)
            row = {
                "run": run_number,
                "statements": len(statements),
                "scanned": scanned,
                "source_hash_calls": sum(
                    item.callcount
                    for item in profile.getstats()
                    if not isinstance(item.code, str)
                    and item.code.co_name == "_hash"
                    and item.code.co_filename.endswith("session_model_inputs.py")
                ),
                "seconds": round(time.perf_counter() - started, 4),
            }
            print("R13E_WITNESS_MISS", json.dumps(row), flush=True)
            assert not matched and scanned == run_number * 10
