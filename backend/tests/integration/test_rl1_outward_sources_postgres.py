"""Check the actual recipient boundaries for a member's privately usable report."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.models.capability_policy import CapabilityPolicy
from app.models.security_audit import ResourcePermission
from app.services.owner_action_policy import save_owner_action_policy
from tests.integration.test_artifact_publication_transactions import publish_company_source, stage_report
from tests.integration.test_rl1_member_files_without_replay_postgres import _call, _run, member_web  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
REPORT_PATH = "workspace/company-report.md"


async def _company_report(n, factory, *, revoked):
    source_id, version = await publish_company_source(factory, tenant=n.tenant_id, user=n.user_id, root=n.data_root)
    async with factory() as db:
        await stage_report(db, tenant=n.tenant_id, agent=n.agent_id, user=n.user_id, session=n.session_id,
            root=n.agent_root, path=REPORT_PATH, dependencies=[{"source_kind": "company_knowledge_publication",
                "source_id": str(source_id), "source_version": version}])
        if revoked:
            grant = await db.scalar(select(ResourcePermission).where(ResourcePermission.principal_id == n.user_id,
                ResourcePermission.resource_type == "company_knowledge_scope"))
            await db.delete(grant)
        await db.commit()
    return (n.agent_root / REPORT_PATH).read_text()


@pytest.mark.parametrize("source", ["intact", "revoked", "replaced"])
async def test_channel_send_checks_the_file_without_a_prior_read(member_web, owner_sessionmaker, monkeypatch, source):
    from app.services.agent_tools import channel_file_sender

    n = member_web
    body = await _company_report(n, owner_sessionmaker, revoked=source == "revoked")
    if source == "replaced":
        (n.agent_root / REPORT_PATH).write_text("Replacement without a matching receipt")
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id, capability="channel.file.send",
            allowed=True, requires_approval=False))
        await save_owner_action_policy(db, tenant_id=n.tenant_id, agent_id=n.agent_id,
            actions={"tool.local_read": "full_authority", "tool.local_write": "confirm_first",
                "tool.external_effect": "full_authority"}, changed_by_user_id=n.user_id)
        await db.commit()

    received = []

    async def channel_client(path, message):
        received.append(path.read_bytes())

    token = channel_file_sender.set(channel_client)
    try:
        model = await _run(n, [
            {"tool_calls": [_call("send_channel_file", "send-report", file_path=REPORT_PATH)],
                "finish_reason": "tool_calls"},
            {"content": "Finished."},
        ], monkeypatch)
    finally:
        channel_file_sender.reset(token)

    result = next(message["content"] for message in model.requests[1]["messages"]
        if message.get("tool_call_id") == "send-report")
    assert received == ([body.encode()] if source == "intact" else []), result
    if source == "replaced":
        assert "workspace_content_unverified" in result, result


@pytest.mark.parametrize("revoked", [False, True], ids=["intact", "revoked"])
async def test_colleague_receives_only_shareable_report_text(member_web, owner_sessionmaker, monkeypatch, revoked):
    from app.models.agent import Agent
    from app.models.runtime_task import RuntimeTask
    from app.services.agent_manager import AgentManager

    n, colleague_id = member_web, uuid.uuid4()
    body = await _company_report(n, owner_sessionmaker, revoked=revoked)
    async with owner_sessionmaker() as db:
        parent = await db.get(Agent, n.agent_id)
        colleague = Agent(id=colleague_id, tenant_id=n.tenant_id, name="Fixture colleague",
            creator_id=n.user_id, owner_user_id=n.user_id, primary_model_id=parent.primary_model_id)
        db.add(colleague)
        db.add(CapabilityPolicy(tenant_id=n.tenant_id, agent_id=n.agent_id, capability="agent.message.send",
            allowed=True, requires_approval=False))
        await db.flush()
        await AgentManager().initialize_agent_files(db, colleague, original_author_id=n.user_id)
        await db.commit()

    async def no_wakeup(**kwargs):
        pass

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    model = await _run(n, [
        {"tool_calls": [_call("read_file", "read-report", path=REPORT_PATH)], "finish_reason": "tool_calls"},
        {"tool_calls": [_call("send_message_to_agent", "share-report", target_agent_id=str(colleague_id),
            message=body, msg_type="consult")], "finish_reason": "tool_calls"},
        {"content": "Finished."},
    ], monkeypatch)
    read = next(message["content"] for message in model.requests[1]["messages"]
        if message.get("tool_call_id") == "read-report")
    assert json.loads(read)["content"].startswith(body), read
    async with owner_sessionmaker() as db:
        messages = list(await db.scalars(select(RuntimeTask.prompt).where(
            RuntimeTask.tenant_id == n.tenant_id, RuntimeTask.child_agent_id == colleague_id,
            RuntimeTask.task_type == "delegation")))
    assert messages == ([] if revoked else [body.strip()]), model.requests[-1]["messages"][-1]
