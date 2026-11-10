"""A colleague reached by an inline consult also waits for its own colleague."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.services.agent_manager import AgentManager
from tests.integration.test_r11c_colleague_lifecycle_postgres import RESULT, _child, _execute, _setup, _tool
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


class ConsultReplyClient(ScriptedLLMClient):
    async def stream(self, **kwargs):
        if self.requests:
            # Forward the actual next-hop result, including a queue acknowledgement
            # on the broken path, rather than inventing a successful model answer.
            messages = [message if isinstance(message, dict) else {"role": message.role, "content": message.content}
                for message in kwargs["messages"]]
            result = next(message["content"] for message in reversed(messages)
                if message["role"] == "tool")
            try:
                content = json.loads(result).get("reply", result)
            except json.JSONDecodeError:
                content = result
            self.script = [{"content": content}]
        return await super().stream(**kwargs)


@pytest.mark.parametrize("entry", ["delegated-child", "workflow-node"])
async def test_nested_colleague_consult_returns_the_next_reply_inline(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, entry,
):
    import app.runtime.invoker as invoker
    from app.runtime.a2a_workflow import A2AWorkflowDefinition
    from app.services import a2a_workflow_runtime
    from app.services.workflow_launch import execute_claimed_workflow_run

    seed, _, child_client = await _setup(owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        tmp_path, tool="send_message_to_agent")
    async with owner_sessionmaker() as db:
        child_agent = await db.get(Agent, seed["child_agent_id"])
        child_model = await db.get(LLMModel, child_agent.primary_model_id)
        next_model = LLMModel(id=uuid4(), tenant_id=seed["tenant_id"], provider="openai",
            model="fake-next-colleague", label="Synthetic next colleague", enabled=True,
            api_key_encrypted=child_model.api_key_encrypted)
        db.add(next_model)
        next_agent = Agent(id=uuid4(), tenant_id=seed["tenant_id"], name="核实助手",
            role_description="Return the verified business facts.", creator_id=seed["user_id"],
            owner_user_id=seed["user_id"], primary_model_id=next_model.id)
        db.add(next_agent)
        await db.flush()
        await AgentManager().initialize_agent_files(db, next_agent,
            personality="Use the current employee display name.", original_author_id=seed["user_id"])
        await db.commit()
    if entry == "delegated-child":
        await _execute(seed["run_id"], owner_sessionmaker, worker="r11c-nested-parent")
        root_id = seed["run_id"]
    else:
        root_id = uuid4()
        graph = A2AWorkflowDefinition.model_validate({
            "name": "Nested colleague consultation",
            "participants": {"worker": {"agent_id": str(seed["child_agent_id"])}},
            "nodes": [{"id": "draft", "type": "agent_handoff_step", "agent_ref": "worker",
                "task": "Consult My Assistant before drafting the report.",
                "output_contract": {"artifacts": [{"name": "report", "path": "workspace/report.md"}]}}],
        })
        await a2a_workflow_runtime.start_run(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            requester_user_id=seed["user_id"], session_id=seed["session_id"], run_id=root_id,
            definition=graph, args={}, permission_profile={"mode": "bypassPermissions"})
        result = await execute_claimed_workflow_run(root_id, session_factory=owner_sessionmaker)
        assert result["status"] == "pending", result
    child = await _child(owner_sessionmaker, {**seed, "run_id": root_id})
    assert child is not None
    async with owner_sessionmaker() as db:
        before = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"])))
    colleague = ConsultReplyClient([_tool("send_message_to_agent", {"agent_name": "核实助手",
        "message": "请核实三点业务进展。", "msg_type": "consult"})])
    next_colleague = ScriptedLLMClient([{"content": RESULT}])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **kwargs:
        {"fake-colleague": child_client, "fake-next-colleague": next_colleague}.get(kwargs["model"], colleague))
    child_client.script = [_tool("send_message_to_agent", {"agent_name": "My Assistant",
        "message": "请向核实助手核实三点业务进展。", "msg_type": "consult"}), {"content": f"同事回复：{RESULT}"}]
    child_client.release.set()
    await _execute(child.id, owner_sessionmaker, worker="r11c-nested-child", delegation=True)
    outcome = json.loads(next(message["content"] for message in child_client.requests[1]["messages"]
        if message["role"] == "tool"))
    assert outcome["operation"] == "consult" and outcome["status"] == "completed", outcome
    assert outcome["reply"] == RESULT, "C must receive E's actual reply through D, not D's queue acknowledgement"
    next_outcome = json.loads(next(message["content"] for message in colleague.requests[1]["messages"]
        if message["role"] == "tool"))
    assert next_outcome["operation"] == "consult" and next_outcome["reply"] == RESULT, next_outcome
    assert len(next_colleague.requests) == 1, "E must execute inline before D returns to C"
    async with owner_sessionmaker() as db:
        after = set(await db.scalars(select(RuntimeTask.id).where(RuntimeTask.tenant_id == seed["tenant_id"])))
        assert after == before, "Neither inline consultation may queue a new runtime task"
        assert (await db.get(RuntimeTask, child.id)).status == "completed"
