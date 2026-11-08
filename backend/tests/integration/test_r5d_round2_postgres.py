"""Creation revisions must not inherit D1 undo or claim unrelated work."""

from dataclasses import replace
import json
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.services.hr_creation_service import hr_creation_draft_payload
from app.tools.handlers.hr import preview_agent_blueprint
from app.tools.runtime import ToolExecutionRequest
from tests.integration import test_m1g_originated_creation_postgres as creation
from tests.integration.test_r5d_creation_card_postgres import pending_request

direct_origin = creation.direct_origin
origin = creation.origin
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def provision(confirmed, owner_sessionmaker, monkeypatch):
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    async with owner_sessionmaker() as db:
        job = await db.get(RuntimeTask, uuid.UUID(confirmed["provisioning_task_id"]))
        job.status = "running"
        await db.commit()
    assert await execute_claimed_hr_provisioning(job.id) == "completed"


async def test_hr_revision_cannot_extend_member_undo(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.services import agent_retirement

    user, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, context.agent_id)
        agent.name, agent.agent_class = "__system_hr__", "internal_system"
        await db.commit()
    first = await pending_request(context)
    arguments = {**creation._arguments(), "name": "HR revised colleague", "blueprint_id": first["blueprint_id"]}
    revised = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", context=replace(context, tool_call_id="hr-revision"), arguments=arguments)))
    if revised.get("status") == "error":
        assert revised["error"] == "immutable", revised
        assert revised["message"] == "这张卡片不能修改。请拒绝后重新说明创建需求。"
        async with creation._client(user, app_user_sessionmaker) as client:
            rejected = await client.post(f"/api/agents/{context.agent_id}/hr-creation-drafts/{first['blueprint_id']}/reject")
            assert rejected.status_code == 200, rejected.text
        # A new HR proposal needs a new member confirmation and keeps HR undo rules.
        arguments.pop("blueprint_id")
        revised = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
            tool_name="preview_agent_blueprint", context=context, arguments=arguments)))
        assert revised["blueprint_id"] != first["blueprint_id"]
    assert revised["status"] == "preview", revised
    confirmed = await creation._confirm_creation_preview(user, context, revised, app_user_sessionmaker)
    await provision(confirmed, owner_sessionmaker, monkeypatch)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(revised["blueprint_id"]))
        agent = await db.get(Agent, draft.created_agent_id)
        try:
            await agent_retirement.prepare(db, agent=agent, actor=user)
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            pytest.fail("A revised HR creation must not inherit member D1 undo")
        assert draft.creation_idempotency_key == f"hr-draft:{draft.id}"
        assert agent.deactivated_at is None


async def test_completed_without_first_task_does_not_claim_dispatch(direct_origin, owner_sessionmaker, monkeypatch):
    user, context, _, _ = direct_origin
    first = await creation._direct_call(context)
    await provision(first, owner_sessionmaker, monkeypatch)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(first["blueprint_id"]))
        payload = hr_creation_draft_payload(draft)
        assert payload["message"] == "数字员工已创建，可在卡片中和它说话或撤销。"
        instruction = payload["creation_flow"]["instruction"]
        assert "creation does not authorize unrelated work" in instruction
        assert "Provisioning dispatches" not in instruction
        assert "pending card" not in instruction
        assert not list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == user.tenant_id, RuntimeTask.task_type == "delegation")))


async def test_ordinary_assistant_cannot_call_legacy_preview_handler(direct_origin, owner_sessionmaker):
    user, context, _, _ = direct_origin
    payload = json.loads(await preview_agent_blueprint(ToolExecutionRequest(
        tool_name="preview_agent_blueprint", context=context, arguments=creation._arguments())))
    assert payload.get("status") == "error", payload
    assert payload["error"] == "hr_agent_required"
    assert payload["message"] == "请在当前对话中直接提出创建数字员工的请求。"
    async with owner_sessionmaker() as db:
        assert not list(await db.scalars(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id)))


async def test_pending_reply_stays_true_after_the_card_is_decided(direct_origin):
    _, context, _, _ = direct_origin
    result = await pending_request(context)
    assert result["message"] == "创建方案在上面的卡片里。"


@pytest.mark.parametrize("code,message", [
    ("capability_denied", "公司规则不允许创建数字员工，请联系管理员。"),
    ("approval_required", "创建数字员工需要公司批准，请联系管理员。"),
])
async def test_failed_company_rule_does_not_tell_member_to_retry(direct_origin, owner_sessionmaker, code, message):
    _, context, _, _ = direct_origin
    result = await pending_request(context)
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(result["blueprint_id"]))
        draft.status, draft.failure_code = "failed", code
        payload = hr_creation_draft_payload(draft)
        assert payload["message"] == message
        assert "重试" not in payload["message"]
