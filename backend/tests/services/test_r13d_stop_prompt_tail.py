"""A member stop remains salient after the Web turn's dynamic context."""

from copy import deepcopy
import json
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from app.services.session_semantic_history import _USER_STOP_MARKER
from tests.services.test_session_input_control_v2 import _seed_session


pytestmark = pytest.mark.usefixtures("migrated_pg_url")
QUESTION = "[E2E] 刚才停了吗？用一句话回答"
ANSWER = "刚才已经停止了。"


def _shape(messages, prompt):
    index = next(i for i, message in enumerate(messages) if message["content"] == prompt)
    trailing = messages[index + 1 :]
    shape = {
        "prompt": prompt,
        "prompt_index": index,
        "trailing_count": len(trailing),
        "trailing_bytes": sum(len(str(message["content"]).encode()) for message in trailing),
        "trailing": [
            {
                "role": message["role"],
                "bytes": len(str(message["content"]).encode()),
                "section": message["sections"],
                "first_line": str(message["content"]).splitlines()[0],
            }
            for message in trailing
        ],
    }
    return shape


@pytest.mark.parametrize("followup", ["member", "internal", "goal_pause_resume"])
async def test_web_stop_question_has_bound_tail_until_normal_completion(owner_sessionmaker, monkeypatch, followup):
    from app import database
    from app.api.chat_sessions import StartSessionRunIn, cancel_session_run, start_session_run
    from app.database import tenant_scoped_session
    from app.models.llm import LLMModel
    from app.services import quota_guard, runtime_budget_service, tenant_resolver, web_chat_runtime as runtime
    from app.runtime import invoker
    from app.tools import governance_resolver, workspace
    from app.services.llm_client import LLMResponse
    from app.services.secrets_provider import get_secrets_provider

    factory = owner_sessionmaker
    tenant_id, user_id, agent_id, session_id, _ = await _seed_session(factory)
    for module, name in (
        (database, "async_session"),
        (tenant_resolver, "async_session"),
        (runtime, "_async_session"),
        (invoker, "async_session"),
        (quota_guard, "async_session"),
        (runtime_budget_service, "async_session"),
        (governance_resolver, "async_session"),
        (workspace, "async_session"),
    ):
        monkeypatch.setattr(module, name, factory)

    def scoped(tenant_id, **kwargs):
        return tenant_scoped_session(tenant_id, **{**kwargs, "session_factory": factory})

    monkeypatch.setattr(runtime, "tenant_scoped_session", scoped)
    monkeypatch.setattr(invoker, "tenant_scoped_session", scoped)
    monkeypatch.setattr(database, "tenant_scoped_session", scoped)
    # Maintenance models also stay local; request assembly and the worker's
    # model-round / tool / terminal paths below remain real.
    monkeypatch.setattr("app.services.session_memory._get_session_memory_model_config", AsyncMock(return_value=None))
    model_id = uuid4()
    async with factory() as db:
        db.add(
            LLMModel(
                id=model_id,
                tenant_id=tenant_id,
                provider="openai",
                model="r13d-local-fake",
                label="R13d local fake",
                api_key_encrypted=get_secrets_provider().encrypt("local-fake"),
                max_input_tokens=128000,
                max_output_tokens=2048,
            )
        )
        await db.flush()
        (await db.get(Agent, agent_id)).primary_model_id = model_id
        await db.commit()

    async def start(content, *, permission_mode=None):
        async with factory() as db:
            result = await start_session_run(
                agent_id=agent_id,
                session_id=session_id,
                body=StartSessionRunIn(content=content, input_id=uuid4(), permission_mode=permission_mode),
                current_user=await db.get(User, user_id),
                db=db,
            )
        assert isinstance(result, dict), result.body
        return UUID(result["run_id"])

    async def execute(run_id, expected_status):
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

        await runtime.execute_web_chat_run(run_id)
        async with factory() as db:
            task = await db.get(RuntimeTask, run_id)
            assert task.status == expected_status, (task.result_summary, task.metadata_json)
        service = RuntimeTerminalBoundaryOutboxService(session_factory=factory)
        processor = WebTerminalBoundaryProcessor(
            session_factory=factory, summary_generator=AsyncMock(return_value="Local test conversation summary")
        )
        items = await service.claim_batch(
            tenant_id=tenant_id, worker_id="r13d-boundary", task_types=("web_chat_turn", "goal_continuation")
        )
        assert len(items) == 1 and items[0].runtime_task_id == run_id
        assert await service.process_terminal_boundary(
            item=items[0], worker_id="r13d-boundary", canonical_validator=processor.validate, process_callback=processor
        )

    requests = []
    script = [
        {"content": "你好。"},
        {
            "tool_calls": [
                {
                    "id": f"todo-{i}",
                    "type": "function",
                    "function": {
                        "name": "track_todo",
                        "arguments": json.dumps(
                            {"action": "add", "title": f"Research part {i}", "status": "in_progress"}
                        ),
                    },
                }
                for i in range(3)
            ]
        },
        {"stop": True},
        {
            "tool_calls": [
                {
                    "id": "answer-progress",
                    "type": "function",
                    "function": {"name": "report_progress", "arguments": json.dumps({"message": "确认停止情况。"})},
                }
            ]
        },
        {"content": ANSWER},
        {"content": "你好。"},
    ]
    if followup != "member":
        script.insert(3, {"content": "Internal work completed."})
    current_run = None
    goal_id = uuid4()

    async def transition_goal(action):
        from app.api.session_goals import GoalTransitionIn, transition_goal as transition

        async with factory() as db:
            result = await transition(
                agent_id=agent_id,
                session_id=session_id,
                goal_id=goal_id,
                body=GoalTransitionIn(action=action),
                current_user=await db.get(User, user_id),
                db=db,
            )
            await db.commit()
        return result

    class Client:
        supports_request_idempotency = False

        async def close(self):
            pass

        async def stream(self, *, messages, **kwargs):
            requests.append(
                [
                    {
                        "role": message.role,
                        "content": deepcopy(message.content),
                        "sections": list(message.context_origin.sections) if message.context_origin else [],
                        "source_ref": message.source_ref,
                    }
                    for message in messages
                ]
            )
            assert script, "unexpected model request"
            item = script.pop(0)
            if item.pop("stop", False):
                if followup == "goal_pause_resume":
                    assert (await transition_goal("pause"))["status"] == "paused"
                else:
                    async with factory() as db:
                        receipt = await cancel_session_run(
                            agent_id=agent_id,
                            session_id=session_id,
                            run_id=current_run,
                            idempotency_key=None,
                            current_user=await db.get(User, user_id),
                            db=db,
                        )
                    assert receipt["accepted"]
            return LLMResponse(
                content=item.get("content", ""),
                tool_calls=item.get("tool_calls", []),
                finish_reason="tool_calls" if item.get("tool_calls") else "stop",
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                model="r13d-local-fake",
            )

    client = Client()
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kwargs: client)
    current_run = await start("hello", permission_mode="bypassPermissions")
    await execute(current_run, "completed")
    assert len(requests) == 1
    _shape(requests[0], "hello")
    assert not any("Answer the member's latest message" in str(message["content"]) for message in requests[0])

    if followup == "goal_pause_resume":
        from app.api.session_goals import StartGoalIn, start_session_goal

        async with factory() as db:
            result = await start_session_goal(
                agent_id=agent_id,
                session_id=session_id,
                body=StartGoalIn(
                    objective="Research these three parts and keep track of each todo.",
                    max_continuation_turns=1,
                    start_immediately=True,
                ),
                current_user=await db.get(User, user_id),
                db=db,
            )
            await db.commit()
        goal_id = UUID(result["id"])
        current_run = UUID(result["run"]["run_id"])
    else:
        current_run = await start(
            "Research these three parts and keep track of each todo.", permission_mode="bypassPermissions"
        )
    await execute(current_run, "killed")
    async with factory() as db:
        assert (await db.get(RuntimeTask, current_run)).status == "killed"
    from app.services.agent_work_ledger import load_agent_work_ledger

    ledger = load_agent_work_ledger(agent_id=agent_id, session_id=str(session_id))
    assert len(ledger["todo_items"]) == 3
    assert all(item["status"] == "in_progress" for item in ledger["todo_items"])
    assert len(requests) == 3

    if followup != "member":
        if followup == "goal_pause_resume":
            result = await transition_goal("resume")
            assert result["status"] == "active"
            current_run = UUID(result["continuation"]["run"]["run_id"])
        else:
            from app.models.chat_session import ChatSession
            from app.services.session_live_input import submit_live_human_input

            async with factory() as db:
                result = await submit_live_human_input(
                    db=db,
                    agent=await db.get(Agent, agent_id),
                    user=await db.get(User, user_id),
                    session=await db.get(ChatSession, session_id),
                    content="Integrate the internal result.",
                    source="task_notification",
                    input_id=uuid4(),
                    requested_kind="start_turn",
                    role="system",
                    runtime_metadata={"task_notification": True},
                )
                await db.commit()
            current_run = UUID(result["run"]["run_id"])
        await execute(current_run, "completed")
        assert len(requests) == 4
        assert any(message["content"] == _USER_STOP_MARKER for message in requests[-1])
        assert not any("Answer the member's latest message" in str(message["content"]) for message in requests[-1])

    offset = len(requests)
    current_run = await start(QUESTION, permission_mode="bypassPermissions")
    await execute(current_run, "completed")
    assert len(requests) == offset + 2
    first, second = requests[offset:]
    assert any(message["content"] == _USER_STOP_MARKER for message in first)
    shape = _shape(first, QUESTION)
    assert shape["trailing_count"] > 0
    for request in (first, second):
        assert _USER_STOP_MARKER in request[-1]["content"]
        assert "Answer the member's latest message" in request[-1]["content"]
        assert request[-1]["sections"][0]["name"] == "member_stop"
        assert request[-1]["sections"][0]["source_ref"].startswith("session-run-cancelled:")
    # The dynamic suffix is transient; frozen prompt and the entire history
    # through the latest member input remain byte-identical in later rounds.
    prefix = shape["prompt_index"] + 1
    assert [(message["role"], message["content"]) for message in first[:prefix]] == [
        (message["role"], message["content"]) for message in second[:prefix]
    ]
    async with factory() as db:
        task = await db.get(RuntimeTask, current_run)
        assert task.status == "completed" and task.result_summary == ANSWER
        assert "stop_notice_active" not in json.dumps(task.metadata_json)
        results = list(
            await db.scalars(
                select(SessionModelResult)
                .where(SessionModelResult.run_id == current_run)
                .order_by(SessionModelResult.round_id)
            )
        )
        assert len(results) == 2
        for result in results:
            snapshot = result.model_request_snapshot_json
            binding = snapshot["input_bindings"]["messages"][-1]
            assert binding["status"] == "bound" and binding["dependency_status"] == "resolved"
            from app.services.session_model_inputs import load_context_message_origin

            origin = await load_context_message_origin(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                source_id=UUID(binding["source_ref"].split(":", 1)[1]),
            )
            assert origin["dependency_coverage"] == {"complete": True, "unbound": []}
            assert origin["sections"][0]["source_ref"] == first[-1]["sections"][0]["source_ref"]
            from app.models.chat_transcript_event import ChatTranscriptEvent

            event = await db.get(ChatTranscriptEvent, UUID(origin["sections"][0]["source_ref"].split(":", 1)[1]))
            assert event.item_kind == "run" and event.lifecycle == "cancelled"
            assert event.metadata_json["v2_payload"]["control_id"]

    current_run = await start("hello again")
    await execute(current_run, "completed")
    assert len(requests) == offset + 3 and not script
    # Historical cancellation remains; the extra tail notice expires.
    assert any(message["content"] == _USER_STOP_MARKER for message in requests[-1])
    assert not any("Answer the member's latest message" in str(message["content"]) for message in requests[-1])
