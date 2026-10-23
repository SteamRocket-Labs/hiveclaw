"""One-shot local worker for the persistent-compute process recovery test."""

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import UUID


async def main():
    # This child is not a pytest session; disable dotenv before app imports.
    from app.config import Settings

    Settings.model_config = {**Settings.model_config, "env_file": None}
    from pytest import MonkeyPatch
    from app import database
    from app.config import get_settings
    from app.runtime import invoker
    from app.services import agent_context, agent_manager, agent_tools, llm_client, web_chat_run_orchestrator
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.secrets_provider import init_secrets_provider
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_persistent_compute_candidate import synthetic_sdk
    from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

    control, phase = json.loads(Path(sys.argv[1]).read_text()), sys.argv[2]
    root, data = Path(control["resource_root"]), Path(control["data_root"])
    init_secrets_provider(os.environ["SECRETS_MASTER_KEY"])
    patch = MonkeyPatch()
    import httpx

    async def no_async_transport(*args, **kwargs):
        raise AssertionError("HTTP transport is forbidden in the synthetic worker")

    def no_sync_transport(*args, **kwargs):
        raise AssertionError("HTTP transport is forbidden in the synthetic worker")

    patch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_transport)
    patch.setattr(httpx.HTTPTransport, "handle_request", no_sync_transport)
    sdk = synthetic_sdk.__wrapped__(patch, root)
    state_file = root / "process-resource.json"
    if phase == "resume":
        sdk.restore(json.loads(state_file.read_text())["resource"])
    original = llm_client.create_llm_client
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    patch.setattr(module, name, lambda **kw: (_ for _ in ()).throw(AssertionError("real model forbidden")))
    patch.setattr(get_settings(), "AGENT_DATA_DIR", str(data))
    patch.setattr(agent_tools, "WORKSPACE_ROOT", data)
    patch.setattr("app.tools.workspace.WORKSPACE_ROOT", data)
    patch.setattr(agent_context, "PERSISTENT_DATA", data)
    patch.setattr(agent_context, "TOOL_WORKSPACE", data)
    patch.setattr(agent_manager, "settings", get_settings())
    patch.setenv("HIVE_CODE_EXEC_PROVIDER", "opensandbox")

    class Memory:
        async def stream(self, **kw):
            return SimpleNamespace(content='{"summary":"Synthetic compute continuity"}')

        async def close(self):
            pass

    patch.setattr(llm_client, "create_llm_client_from_config", lambda *a, **kw: Memory())
    script = ScriptedLLMClient(control["rounds"])
    patch.setattr(invoker, "create_llm_client", lambda **kw: script)

    def checkpoint():
        state_file.write_text(json.dumps({"resource": sdk.snapshot(), "requests": script.requests}))

    if phase == "first":
        bind = web_chat_run_orchestrator._bind_session_round_inputs

        async def settled_checkpoint(state, round_index):
            if round_index == 2:
                # The first model/tool round is durable. No second model
                # request has been prepared or dispatched at this checkpoint.
                checkpoint()
                print(json.dumps({"ready_before_second_model": True, "pid": os.getpid()}), flush=True)
                await asyncio.Event().wait()
            return await bind(state, round_index)

        patch.setattr(web_chat_run_orchestrator, "_bind_session_round_inputs", settled_checkpoint)
    fence = set_runtime_task_fence(task_id=UUID(control["run_id"]),
        worker_id="kernel-worker-1" if phase == "first" else "compute-worker-2",
        claim_version=1 if phase == "first" else 2)
    try:
        await execute_web_chat_run(UUID(control["run_id"]))
        checkpoint()
    finally:
        reset_runtime_task_fence(fence)
        await database.engine.dispose()
        patch.undo()


if __name__ == "__main__":
    asyncio.run(main())
