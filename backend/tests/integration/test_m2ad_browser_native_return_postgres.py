"""Native delegation/question/return with HTTP mocks and a non-browser pipe child."""

import asyncio
from copy import deepcopy
import json
import os
import platform
from pathlib import Path
import sys
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m2ad_browser_control_postgres import CHILD, human
from tests.integration.test_m2ad_browser_tools_postgres import operation

pytestmark = [pytest.mark.usefixtures("migrated_pg_url"), pytest.mark.skipif(platform.system() != "Darwin",
    reason="Native macOS work path; Linux delivery is deferred.")]


async def test_native_delegated_browser_question_resumes_once_and_returns_artifact(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    from app.agents import orchestrator
    from app.api import browser_control as api, chat_sessions, files
    from app.core.security import create_access_token
    from app.database import get_db, tenant_scoped_session
    from app.models.agent import Agent
    from app.models.capability_policy import CapabilityPolicy
    from app.models.chat_artifact import ChatArtifact
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.llm import LLMModel
    from app.models.runtime_task import RuntimeTask
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.session_v2 import SessionTurnInput
    from app.models.user import User
    from app.runtime import invoker
    from app.services import agent_tools, audit_logger, browser_control, secrets_provider
    from app.services.action_preflight import CharterZone
    from app.services.agent_manager import AgentManager
    from app.services.browser_control import BrowserControlService, BrowserSource
    from app.services.browser_pipe import BrowserLaunchSpec
    from app.services.llm_client import create_llm_client
    from app.services.owner_action_policy import OWNER_ACTION_IDS, save_owner_action_policy
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.tools.handlers import browser

    from app.services import content_input_receipts

    validate_inputs = content_input_receipts.validate_runtime_input_dependencies

    async def capture_input_failure(*args, **kwargs):
        try:
            return await validate_inputs(*args, **kwargs)
        except content_input_receipts.ContentInputReceiptError as exc:
            (tmp_path / "native-input-failure.json").write_text(json.dumps({
                "error": str(exc), "fields": vars(exc), "dependencies": args[2] if len(args) > 2 else kwargs,
            }, default=str, indent=2))
            raise

    monkeypatch.setattr(content_input_receipts, "validate_runtime_input_dependencies", capture_input_failure)

    async def no_wakeup(**_kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    monkeypatch.setattr(secrets_provider, "_provider", secrets_provider._provider)
    secrets_provider.init_secrets_provider(os.environ["SECRETS_MASTER_KEY"])
    manager = BrowserControlService()
    revalidate_source = BrowserSource.revalidate

    async def capture_source_failure(self, *args, **kwargs):
        try:
            return await revalidate_source(self, *args, **kwargs)
        except Exception as exc:
            chain = []
            current = exc
            while current is not None:
                chain.append({"type": type(current).__name__, "message": str(current)})
                current = current.__cause__
            (tmp_path / "native-browser-source-failure.json").write_text(json.dumps(chain, indent=2))
            raise

    monkeypatch.setattr(BrowserSource, "revalidate", capture_source_failure)
    close_resource = manager._close_resource

    async def capture_close(*args, **kwargs):
        import traceback

        frame = sys._getframe(1)
        (tmp_path / "native-browser-close.json").write_text(json.dumps({
            "caller": frame.f_code.co_name, "traceback": traceback.format_exc(),
            "revoked": frame.f_locals.get("revoked"), "stop": frame.f_locals.get("stop"),
            "kwargs": {key: value for key, value in kwargs.items() if key != "owned_page"},
        }, default=str, indent=2))
        return await close_resource(*args, **kwargs)

    monkeypatch.setattr(manager, "_close_resource", capture_close)
    monkeypatch.setattr(api, "browser_control", manager)
    monkeypatch.setattr(browser_control, "browser_control", manager)
    monkeypatch.setattr(browser, "browser_control", manager)
    monkeypatch.setattr(audit_logger, "async_session", app_user_sessionmaker)
    receiver = uuid4()
    parent_script = []

    async def setup(seed):
        async with owner_sessionmaker() as db:
            parent = await db.get(Agent, seed["agent_id"])
            model = await db.get(LLMModel, parent.primary_model_id)
            model.provider, model.supports_vision = "openai-response", True
            child = Agent(id=receiver, tenant_id=seed["tenant_id"], name="Browser colleague",
                creator_id=seed["user_id"], owner_user_id=seed["user_id"], primary_model_id=model.id)
            db.add(child)
            await db.flush()
            await AgentManager().initialize_agent_files(db, child, personality="Draft a browser report.",
                original_author_id=seed["user_id"])
            for capability in ("agent.message.send", "agent.plan.clarify", "computer.browser.read", "computer.browser.control", "workspace.file.write"):
                db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability=capability,
                    allowed=True, requires_approval=False))
            for agent_id in (seed["agent_id"], receiver):
                await save_owner_action_policy(db, agent_id=agent_id, tenant_id=seed["tenant_id"],
                    changed_by_user_id=seed["user_id"],
                    actions={action: CharterZone.FULL_AUTHORITY for action in OWNER_ACTION_IDS})
            await db.commit()
        parent_script.extend([
            {"tool_calls": [{"id": "native-browser-delegate", "type": "function", "function": {
                "name": "delegate_to_agent", "arguments": json.dumps({"agent_name": "Browser colleague",
                    "target_agent_id": str(receiver), "message": "Inspect the private page, ask for login, then draft workspace/browser-report.md."})}}],
                "finish_reason": "tool_calls"},
            {"content": "The browser report is delegated.", "finish_reason": "stop"},
        ])
        return {}

    seed, parent_client, _, _ = await _run_native_skill_task(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        client_script=parent_script, skill_setup=setup,
        prompt="Delegate the private browser report to Browser colleague; return its file to this work.",
    )
    async with owner_sessionmaker() as db:
        tasks = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.tenant_id == seed["tenant_id"],
            RuntimeTask.task_type == "delegation", RuntimeTask.child_agent_id == receiver)))
        assert len(tasks) == 1, parent_client.requests
        task_id, child_session = tasks[0].id, UUID(tasks[0].child_session_id)
        assert tasks[0].status == "pending", tasks[0].metadata_json
        user = await db.get(User, seed["user_id"])
    child_seed = {**seed, "agent_id": receiver, "session_id": child_session, "run_id": task_id}
    source = BrowserSource(*(child_seed[key] for key in ("tenant_id", "agent_id", "user_id", "session_id")))
    image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="

    def spec(resource_id, tenant_id, agent_id):
        assert tenant_id == seed["tenant_id"] and agent_id == receiver
        root = tmp_path / str(resource_id)
        root.mkdir()
        child = CHILD.replace("base64.b64encode(b'synthetic-image-boundary').decode()", repr(image))
        return BrowserLaunchSpec(Path(sys.executable).resolve(), ("-B", "-c", child, str(root)),
            {"HOME": str(root), "PATH": "/usr/bin:/bin"}, root)

    monkeypatch.setattr(browser_control, "browser_device_spec", spec)
    requests, clients = [], []
    phase = [0]

    def last_view(payload):
        outputs = [item for item in payload["input"] if item.get("type") == "function_call_output"]
        for item in reversed(outputs):
            content = item["output"]
            text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict) and decoded.get("browser"):
                return decoded["browser"]
        raise AssertionError(f"No model-visible browser state: {outputs}")

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        step = phase[0]
        phase[0] += 1
        if step in (1, 4):
            name, args = "browser_observe", {}
        elif step == 0:
            name, args = "browser_manage", operation("start")
        elif step == 2:
            name, args = "browser_manage", operation("request_control", last_view(payload))
        elif step == 3:
            name, args = "ask_user_question", {"questions": [{"question": "Log in and return control, then continue?",
                "header": "Login", "options": [{"label": "Continue"}, {"label": "Cancel"}]}]}
        elif step == 5:
            name, args = "browser_action", operation("type", last_view(payload), text="resumed-agent-effect")
        elif step == 6:
            name, args = "write_file", {"path": "workspace/browser-report.md", "content": "BROWSER_REPORT: inspected after human return"}
        elif step == 7:
            name, args = "browser_manage", operation("stop", last_view(payload))
        else:
            assert step == 8
            return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text",
                "text": "Browser report prepared.\nDELIVERABLE: workspace/browser-report.md"}]}]})
        return httpx.Response(200, json={"output": [{"type": "function_call", "call_id": f"browser-step-{step}",
            "name": name, "arguments": json.dumps(args)}]})

    def client_factory(**kwargs):
        assert kwargs["provider"] == "openai-response" and kwargs["supports_vision"] is True
        client = create_llm_client(**kwargs)
        client._inner._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(invoker, "create_llm_client", client_factory)

    async def dispatch(worker):
        async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
            claimed = await RuntimeTaskClaimService(db=db, worker_id=worker, task_types=("delegation",),
                lease_seconds=300).claim_available(batch_size=10)
            assert [task.id for task in claimed] == [task_id]
            version = claimed[0].claim_version
        fence = set_runtime_task_fence(task_id=task_id, worker_id=worker, claim_version=version)
        try:
            assert await asyncio.wait_for(orchestrator.dispatch_persisted_async_delegation(
                task_id.hex, wait_for_completion=True), 120)
        finally:
            reset_runtime_task_fence(fence)
        return version

    try:
        first_version = await dispatch("browser-before-login")
        async with owner_sessionmaker() as db:
            task = await db.get(RuntimeTask, task_id)
            assert task.status == "suspended", (task.status, task.result_summary, task.metadata_json, requests)
            question = await db.scalar(select(ChatTranscriptEvent).where(ChatTranscriptEvent.run_id == task_id,
                ChatTranscriptEvent.item_kind == "user_question"))
            assert question is not None
            question_id = question.item_id
        assert phase[0] == 4
        assert any(image in json.dumps(payload) for payload in requests)
        assert all({"browser_observe", "browser_manage", "browser_action"} <=
            {tool["name"] for tool in payload["tools"]} for payload in requests)

        async def scoped_db():
            async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
                yield db

        app = FastAPI()
        app.include_router(api.router)
        app.include_router(chat_sessions.router)
        app.include_router(files.router)
        app.dependency_overrides[get_db] = scoped_db
        headers = {"Authorization": "Bearer " + create_access_token(str(user.id), user.role, str(user.tenant_id))}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://native-browser.test",
            headers=headers) as http:
            view = await manager.read(source)
            taken = await human(http, child_seed, "take_control", view)
            assert taken.status_code == 200, taken.text
            view = taken.json()["browser"]
            typed = await human(http, child_seed, "type", view, {"text": "HUMAN_ONLY_LOGIN_SECRET"})
            assert typed.status_code == 200, typed.text
            returned = await human(http, child_seed, "return_control", view)
            assert returned.status_code == 200, returned.text
            assert returned.json()["browser"]["fresh_observation_required"] is True
            answer_id = uuid4()
            body = {"kind": "answer_request", "input_id": str(answer_id), "idempotency_key": str(answer_id),
                "request_item_id": str(question_id), "content_parts": [{"type": "text", "text": "Continue after login."}]}
            url = f"/agents/{receiver}/sessions/{child_session}/inputs"
            answered = await http.post(url, json=body)
            assert answered.status_code == 200 and answered.json()["admission_state"] == "admitted", answered.text
            repeated = await http.post(url, json=body)
            assert repeated.status_code == 200 and repeated.json()["replayed"] is True, repeated.text
            from app.services.session_input_dispatch import recover_admitted_session_inputs_once

            async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
                await recover_admitted_session_inputs_once(db, worker_id="browser-answer", tenant_id=seed["tenant_id"],
                    admission_ids=(uuid5(UUID(answered.json()["command_id"]), "input-admission:revision:1"),))
                assert (await db.get(SessionTurnInput, answer_id)).status == "queued"
            assert await dispatch("browser-after-login") > first_version
            async with owner_sessionmaker() as db:
                task = await db.get(RuntimeTask, task_id)
                assert task.status == "completed", (task.status, task.result_summary, task.metadata_json)
                assert (await db.get(SessionTurnInput, answer_id)).status == "applied"
                artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == task_id,
                    ChatArtifact.path == "workspace/browser-report.md"))
                assert artifact is not None
                artifact_id = artifact.id
            assert phase[0] == 9 and len(requests) == 9
            assert "HUMAN_ONLY_LOGIN_SECRET" not in json.dumps(requests)
            resumed_observation = next(item for item in requests[5]["input"]
                if item.get("type") == "function_call_output" and item["call_id"] == "browser-step-4")
            assert image in json.dumps(resumed_observation)
            assert last_view(requests[5])["control_epoch"] > last_view(requests[2])["control_epoch"]
            effect_file = tmp_path / (await manager.read(source))["id"] / "effects"
            assert effect_file.read_text().splitlines() == ["HUMAN_ONLY_LOGIN_SECRET", "resumed-agent-effect"]

            from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
            from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
            from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService

            processor = DirectInvocationTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
            drained = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
                tenant_id=seed["tenant_id"], worker_id="browser-terminal", canonical_validator=processor.validate,
                process_callback=processor, task_types=("delegation",))
            assert drained["delivered"] == 1, drained
            service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
            claimed = await service.claim_batch(worker_id="browser-return", limit=100)
            pages = await service.prepare_integration_pages(worker_id="browser-return",
                claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
            assert pages
            for page in pages:
                delivered = await service._deliver_page(page)
                replay = await service._deliver_page(page)
                assert delivered["ok"] is True and replay["status"] == "already_delivered"
                assert replay["event_id"] == delivered["event_id"]
            async with tenant_scoped_session(seed["tenant_id"], session_factory=app_user_sessionmaker) as db:
                notification = await db.scalar(select(RuntimeNotificationOutbox).where(
                    RuntimeNotificationOutbox.source_run_id == str(task_id), RuntimeNotificationOutbox.source_kind == "a2a_delegation"))
                assert notification is not None and notification.parent_session_id == seed["session_id"]
                returns = list(await db.scalars(select(RuntimeTask).where(RuntimeTask.parent_agent_id == seed["agent_id"],
                    RuntimeTask.metadata_json["integration_page_id"].astext.in_([str(page.id) for page in pages]))))
                assert len(returns) == 1
                return_id, result_ref = returns[0].id, notification.result_ref
            result = json.loads(str(await agent_tools.execute_tool("read_runtime_result", {"result_ref": result_ref},
                agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
                runtime_task_id=str(return_id), emit_runtime_hooks=False)))
            assert result["status"] == "ok" and result["dependency_coverage"]["complete"] is True, result
            returned_artifact = json.loads(result["content"])["artifacts"][0]
            assert returned_artifact["artifact_id"] == str(artifact_id)
            content = await http.get(f"/agents/{receiver}/files/artifacts/{artifact_id}/content")
            assert content.status_code == 200, content.text
            assert content.json()["content"] == "BROWSER_REPORT: inspected after human return", content.text
            assert content.json()["uses_snapshot"] is True and content.json()["legacy_current_file_fallback"] is False
            from app.models.session_v2 import SessionToolInvocation

            async with owner_sessionmaker() as db:
                observed_call = await db.scalar(select(SessionToolInvocation).where(
                    SessionToolInvocation.run_id == task_id, SessionToolInvocation.provider_tool_use_id == "browser-step-1"))
                event_id = observed_call.result_event_id
                event = await db.get(ChatTranscriptEvent, event_id)
                original_metadata = deepcopy(event.metadata_json)
                changed = deepcopy(original_metadata)
                changed["v2_payload"]["model_visible_content"][1]["data"] = "Qg=="
                event.metadata_json = changed
                await db.commit()
            try:
                denied = json.loads(str(await agent_tools.execute_tool("read_runtime_result", {"result_ref": result_ref},
                    agent_id=seed["agent_id"], user_id=seed["user_id"], session_id=str(seed["session_id"]),
                    runtime_task_id=str(return_id), emit_runtime_hooks=False)))
                assert denied["status"] == "source_access_denied" and denied["reason_code"] == "content_source_changed", denied
            finally:
                async with owner_sessionmaker() as db:
                    event = await db.get(ChatTranscriptEvent, event_id)
                    event.metadata_json = original_metadata
                    await db.commit()
        (tmp_path / "native-browser-provider-wire.json").write_text(json.dumps(requests, indent=2))
    finally:
        (tmp_path / "native-browser-provider-wire.json").write_text(json.dumps(requests, indent=2))
        await manager.close()
        for client in clients:
            await client.close()
        for mapping in (orchestrator._async_tasks, orchestrator._async_task_fallback_records, orchestrator._async_task_parent_ids):
            state = mapping.pop(task_id.hex, None)
            if getattr(state, "task", None) is not None:
                await asyncio.gather(state.task, return_exceptions=True)
