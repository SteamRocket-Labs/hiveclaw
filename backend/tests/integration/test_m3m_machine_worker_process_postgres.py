"""Machine admission is consumed by a separate, unmodified runtime worker."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sys
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.database import get_db, tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from tests.integration.test_m3h_machine_identity_postgres import machine_api as _machine_api, _issue
from tests.integration.test_m3i_machine_tasks_postgres import _prepare_native_machine

machine_api = _machine_api
pytestmark = pytest.mark.usefixtures("migrated_pg_url")

_WORKER = """
import asyncio,json,os,signal
from pathlib import Path
from app.models import import_all_models
from app.services.secrets_provider import init_secrets_provider
import_all_models()
init_secrets_provider(os.environ['SECRETS_MASTER_KEY'])
from app.services.runtime_task_worker import start_runtime_task_worker_loop,runtime_task_worker_snapshot
from app.core.events import close_redis
from app.database import engine
async def main():
    stop=asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
    worker=asyncio.create_task(start_runtime_task_worker_loop())
    while not runtime_task_worker_snapshot()['running']:
        if worker.done():await worker
        await asyncio.sleep(0.02)
    Path('worker-ready.json').write_text(json.dumps(runtime_task_worker_snapshot()))
    await stop.wait()
    worker.cancel()
    await asyncio.gather(worker,return_exceptions=True)
    from app.services.web_chat_runtime import _TASKS
    await asyncio.wait_for(asyncio.gather(*list(_TASKS.values()),return_exceptions=True),10)
    Path('worker-final.json').write_text(json.dumps(runtime_task_worker_snapshot()))
    await close_redis()
    await engine.dispose()
asyncio.run(main())
"""


@asynccontextmanager
async def _serve(web):
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            web, host="127.0.0.1", port=port, ws="none", lifespan="off", log_level="warning", access_log=False
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.02)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@asynccontextmanager
async def _worker_process(directory, env, worker_id):
    directory.mkdir()
    with (directory / "worker.log").open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            "-c",
            _WORKER,
            cwd=directory,
            env={**env, "RUNTIME_TASK_WORKER_ID": worker_id},
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            for _ in range(250):
                if (directory / "worker-ready.json").exists():
                    break
                assert process.returncode is None, (directory / "worker.log").read_text()[-5000:]
                await asyncio.sleep(0.02)
            assert (directory / "worker-ready.json").exists(), (directory / "worker.log").read_text()[-5000:]
            yield process
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()


@pytest.mark.parametrize("restart_after_write", [False, True], ids=["natural-claim", "process-death"])
async def test_sdk_blocking_send_is_claimed_by_real_worker_and_returns_captured_result(
    machine_api,
    owner_sessionmaker,
    app_user_sessionmaker,
    app_user_engine,
    monkeypatch,
    tmp_path,
    restart_after_write,
):
    from a2a.client import ClientConfig, ClientFactory
    from a2a.types import a2a_pb2 as protocol
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse
    from redis.asyncio import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError
    from app.config import get_settings
    from app.core import events
    from app.models.agent import Agent
    from app.models.llm import LLMModel
    from app.models.session_v2 import SessionToolInvocation, SessionTurnInput
    from app.models.token_usage_event import TokenUsageEvent
    from app.services import runtime_task_worker
    from app.services.web_chat_stream_bus import start_web_chat_stream_forwarder

    seed, api = machine_api
    actual_notify = runtime_task_worker.notify_runtime_task_worker
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_task_worker, "notify_runtime_task_worker", actual_notify)
    issued = await api.post(
        f"/agents/{seed['agent']}/machine-identities",
        json={**_issue(seed), "scopes": ["agent.describe", "agent.execute", "task.read"]},
    )
    assert issued.status_code == 201, issued.text
    identity = issued.json()
    content = "NATURAL_WORKER_REPORT\n" + ("Complete captured worker result.\n" * 900)
    final = "DELIVERABLE: workspace/worker-report.md\n[Report](workspace/worker-report.md)"
    provider = FastAPI()
    primary_requests = []
    interrupted_request = asyncio.Event()
    release_interrupted_request = asyncio.Event()
    (tmp_path / "provider-requests.jsonl").touch()

    @provider.post("/v1/chat/completions")
    async def model(request: Request):
        body = await request.json()
        with (tmp_path / "provider-requests.jsonl").open("a") as receipt:
            receipt.write(json.dumps(body) + "\n")
        assert request.headers["authorization"] == "Bearer synthetic-machine-only"
        if body.get("tools"):
            primary_requests.append(body)
            if restart_after_write and len(primary_requests) == 2:
                interrupted_request.set()
                await release_interrupted_request.wait()
            if len(primary_requests) == 1:
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "natural-worker-write",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "workspace/worker-report.md", "content": content}),
                            },
                        }
                    ],
                }
                reason = "tool_calls"
            else:
                message, reason = {"role": "assistant", "content": final}, "stop"
        else:
            message, reason = (
                {"role": "assistant", "content": json.dumps({"summary": "Synthetic auxiliary response."})},
                "stop",
            )
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        if not body.get("stream"):
            return {
                "id": str(uuid4()),
                "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": usage,
            }

        async def stream():
            delta = dict(message)
            for index, tool in enumerate(delta.get("tool_calls", [])):
                tool["index"] = index
            for choice, tokens in [
                ({"index": 0, "delta": delta, "finish_reason": None}, None),
                ({"index": 0, "delta": {}, "finish_reason": reason}, usage),
            ]:
                yield (
                    "data: "
                    + json.dumps(
                        {"id": "local-model-response", "model": body["model"], "choices": [choice], "usage": tokens}
                    )
                    + "\n\n"
                )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        redis_port = probe.getsockname()[1]
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    redis_log = (tmp_path / "redis.log").open("wb")
    redis_process = await asyncio.create_subprocess_exec(
        "redis-server",
        "--bind",
        "127.0.0.1",
        "--port",
        str(redis_port),
        "--save",
        "",
        "--appendonly",
        "no",
        "--dir",
        str(tmp_path),
        stdout=redis_log,
        stderr=asyncio.subprocess.STDOUT,
    )
    redis = Redis.from_url(redis_url, decode_responses=True)
    forwarder = None
    try:
        for _ in range(100):
            try:
                if await redis.ping():
                    break
            except RedisConnectionError:
                pass
            await asyncio.sleep(0.02)
        assert await redis.ping()
        await events.close_redis()
        monkeypatch.setattr(get_settings(), "REDIS_URL", redis_url)
        async with _serve(provider) as model_origin, AsyncExitStack() as workers:
            async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                agent = await db.get(Agent, seed["agent"])
                selected = await db.get(LLMModel, agent.primary_model_id)
                selected.base_url, selected.max_output_tokens = model_origin + "/v1", 16_384
                await db.commit()
            env = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SECRETS_MASTER_KEY")
                if key in os.environ
            }
            env.update(
                PYTHONPATH=str(Path(__file__).resolve().parents[2]),
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONUNBUFFERED="1",
                DATABASE_URL=app_user_engine.url.render_as_string(hide_password=False),
                REDIS_URL=redis_url,
                AGENT_DATA_DIR=str(root.parent),
                AGENT_TEMPLATE_DIR=get_settings().AGENT_TEMPLATE_DIR,
                SECRET_KEY="synthetic-worker-only",
                JWT_SECRET_KEY="synthetic-worker-only",
                NO_PROXY="127.0.0.1,localhost",
                HIVE_PROCESS_ROLE="runtime",
                RUNTIME_TASK_WORKER_ID="natural-machine-worker",
                RUNTIME_TASK_CLAIM_POLL_SECONDS="1",
                RUNTIME_TASK_CLAIM_LEASE_SECONDS="12",
            )
            worker = await workers.enter_async_context(
                _worker_process(tmp_path / "worker-1", env, "natural-machine-worker")
            )
            forwarder = asyncio.create_task(start_web_chat_stream_forwarder())
            from app.main import app as web

            async def database():
                async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                    yield db

            monkeypatch.setitem(web.dependency_overrides, get_db, database)
            async with (
                _serve(web) as origin,
                httpx.AsyncClient(
                    headers={"Authorization": "Bearer " + identity["token"]}, timeout=60, trust_env=False
                ) as wire,
            ):
                client = await ClientFactory(ClientConfig(httpx_client=wire, streaming=False)).create_from_url(
                    f"{origin}/api/v1/agents/{seed['agent']}/a2a"
                )
                request = protocol.SendMessageRequest(
                    message=protocol.Message(
                        message_id=str(uuid4()),
                        role=protocol.ROLE_USER,
                        parts=[
                            protocol.Part(
                                text="Write workspace/worker-report.md and deliver the complete captured report."
                            )
                        ],
                    )
                )

                async def send():
                    return [event async for event in client.send_message(request)]

                pending = asyncio.create_task(send())
                write_snapshot = None
                try:
                    if restart_after_write:
                        await asyncio.wait_for(interrupted_request.wait(), 30)
                        path = root / "workspace/worker-report.md"
                        assert path.read_text() == content
                        file_mtime = path.stat().st_mtime_ns
                        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                            invocation = await db.scalar(
                                select(SessionToolInvocation).where(
                                    SessionToolInvocation.tenant_id == seed["tenant"],
                                    SessionToolInvocation.tool_name == "write_file",
                                )
                            )
                            assert invocation is not None and invocation.effect_state == "effect_committed"
                            write_snapshot = (invocation.id, invocation.version, invocation.result_event_id)
                        worker.kill()
                        await worker.wait()
                        release_interrupted_request.set()
                        await workers.enter_async_context(
                            _worker_process(tmp_path / "worker-2", env, "replacement-machine-worker")
                        )
                    responses = await asyncio.wait_for(pending, 60)
                finally:
                    release_interrupted_request.set()
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
                (task,) = [event.task for event in responses]
                assert task.status.state == protocol.TASK_STATE_COMPLETED
                assert task.history[0].message_id == request.message.message_id
                assert task.artifacts[0].parts[0].text == final
                (file_part,) = [part for artifact in task.artifacts for part in artifact.parts if part.url]
                download = await wire.get(file_part.url)
                assert download.status_code == 200 and download.content == content.encode(), download.text
                assert len(primary_requests) == (3 if restart_after_write else 2)
                assert all(
                    body["model"] == "machine-native-model"
                    and body.get("max_completion_tokens", body.get("max_tokens")) == 16_384
                    for body in primary_requests
                )
                assert {"write_file", "read_file", "ask_user_question"} <= {
                    tool["function"]["name"] for tool in primary_requests[0]["tools"]
                }
                await workers.aclose()
                async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
                    run = await db.get(RuntimeTask, UUID(task.id))
                    assert run.attempt_count == (2 if restart_after_write else 1)
                    assert run.metadata_json["claimed_by"] == (
                        "replacement-machine-worker" if restart_after_write else "natural-machine-worker"
                    )
                    assert run.root_user_id is None and str(run.root_external_principal_id) == identity["id"]
                    invocations = list(
                        await db.scalars(
                            select(SessionToolInvocation).where(
                                SessionToolInvocation.run_id == run.id, SessionToolInvocation.tool_name == "write_file"
                            )
                        )
                    )
                    assert len(invocations) == 1 and invocations[0].effect_state == "effect_committed"
                    inputs = list(
                        await db.scalars(select(SessionTurnInput).where(SessionTurnInput.target_run_id == run.id))
                    )
                    assert len(inputs) == 1
                    settlement = run.metadata_json["session_v2_token_settlement"]
                    assert settlement["pending"] is False and settlement["suppressed"] == 0
                    charges = list(
                        await db.scalars(
                            select(TokenUsageEvent).where(
                                TokenUsageEvent.agent_id == seed["agent"],
                                TokenUsageEvent.details["idempotency_key"].astext.is_not(None),
                            )
                        )
                    )
                    assert len(charges) == 2 and sum(charge.tokens for charge in charges) == 60
                    if restart_after_write:
                        invocation = invocations[0]
                        assert (invocation.id, invocation.version, invocation.result_event_id) == write_snapshot
                        assert path.stat().st_mtime_ns == file_mtime
                        assert any(
                            message.get("role") == "tool" and message.get("tool_call_id") == "natural-worker-write"
                            for message in primary_requests[-1]["messages"]
                        )
                (tmp_path / "model-requests.json").write_text(json.dumps(primary_requests, indent=2))
                await client.close()
    finally:
        if forwarder is not None:
            forwarder.cancel()
            await asyncio.gather(forwarder, return_exceptions=True)
        await events.close_redis()
        await redis.aclose()
        if redis_process.returncode is None:
            redis_process.terminate()
            await asyncio.wait_for(redis_process.wait(), 10)
        redis_log.close()
