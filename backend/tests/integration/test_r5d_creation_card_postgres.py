"""One human creation request retains one card and a terminal next step."""

from dataclasses import replace
import json
import uuid

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.hr_creation import HrCreationDraft
from app.models.runtime_task import RuntimeTask
from app.models.tool import Tool
from app.services.hr_creation_service import (
    HrCreationConflict, reject_hr_creation_draft_record, upsert_hr_creation_draft,
)
from app.tools.handlers.hr import _build_blueprint_preview_payload, create_employee_from_request
from app.tools.runtime import ToolExecutionRequest
from tests.integration import test_m1g_originated_creation_postgres as creation

direct_origin = creation.direct_origin
origin = creation.origin
pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def pending_request(context):
    from app.services.llm_client import LLMMessage

    arguments = creation._direct_arguments(first_task="列出三条周报要点")
    context = await creation._seal_direct_call(context, arguments,
        extra_messages=(LLMMessage(role="assistant", content="你好，我可以帮你整理周报。"),))
    result = json.loads(await create_employee_from_request(ToolExecutionRequest(
        tool_name="create_employee_from_request", context=context, arguments=arguments)))
    assert result.get("draft_status") == "awaiting_confirmation", result
    assert result.get("provisioning_task_id") is None
    return result


async def test_same_input_three_calls_including_rejection(direct_origin, owner_sessionmaker):
    user, context, _, input_id = direct_origin
    first = await pending_request(context)
    second = await creation._direct_call(replace(context, tool_call_id="second-call"))
    async with owner_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, uuid.UUID(first["blueprint_id"]))
        reject_hr_creation_draft_record(draft, rejecting_user_id=user.id)
        await db.commit()
    third = await creation._direct_call(replace(context, tool_call_id="after-reject"), name="Changed name")
    assert [first["blueprint_id"], second["blueprint_id"], third["blueprint_id"]] == [first["blueprint_id"]] * 3
    assert third["draft_status"] == "rejected"
    assert third["message"] == "没有创建数字员工。你想修改什么？"
    assert first["message"] == "创建方案在上面的卡片里。"
    assert "Do not" in first["creation_flow"]["instruction"]
    async with owner_sessionmaker() as db:
        drafts = list(await db.scalars(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id)))
        assert len(drafts) == 1
        assert drafts[0].creation_idempotency_key == f"direct-employee:{input_id}"


async def test_in_place_preview_revision_cannot_erase_request_identity(direct_origin, owner_sessionmaker):
    user, context, _, input_id = direct_origin
    first = await pending_request(context)
    # A revision cannot erase the request identity or change its undo category.
    async with owner_sessionmaker() as db:
        preview = _build_blueprint_preview_payload(creation._arguments())
        with pytest.raises(HrCreationConflict, match="这张卡片不能修改") as refused:
            await upsert_hr_creation_draft(db, tenant_id=user.tenant_id, hr_agent_id=context.agent_id,
                session_id=uuid.UUID(context.session_id), requested_by_user_id=user.id,
                blueprint_id=uuid.UUID(first["blueprint_id"]), preview_payload=preview)
        assert refused.value.code == "immutable"
        draft = await db.get(HrCreationDraft, uuid.UUID(first["blueprint_id"]))
        assert draft.blueprint_hash == first["blueprint_hash"]
        assert draft.blueprint_version == first["blueprint_version"]
        reject_hr_creation_draft_record(draft, rejecting_user_id=user.id)
        await db.commit()
    replay = await creation._direct_call(replace(context, tool_call_id="after-old-preview"))
    assert replay["blueprint_id"] == first["blueprint_id"]
    assert replay["draft_status"] == "rejected"
    async with owner_sessionmaker() as db:
        drafts = list(await db.scalars(select(HrCreationDraft).where(HrCreationDraft.tenant_id == user.tenant_id)))
        assert len(drafts) == 1
        assert drafts[0].creation_idempotency_key == f"direct-employee:{input_id}"


async def test_confirmed_and_created_result_dispatches_first_task_once(
    direct_origin, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.services.hr_provisioning_runtime import execute_claimed_hr_provisioning

    user, context, _, _ = direct_origin
    preview = await pending_request(context)
    confirmed = await creation._confirm_creation_preview(user, context, preview, app_user_sessionmaker)
    replay = await creation._direct_call(replace(context, tool_call_id="after-confirm"))
    assert replay["blueprint_id"] == preview["blueprint_id"]
    assert replay["draft_status"] == "confirmed"
    assert replay["message"] == "正在创建数字员工，完成后可在卡片中和它说话。"

    async def no_refinement(*_args, **_kwargs):
        return None, "synthetic_transport"

    monkeypatch.setattr("app.tools.handlers.hr._resolve_employee_refinement_model", no_refinement)
    async with owner_sessionmaker() as db:
        job = await db.get(RuntimeTask, uuid.UUID(confirmed["provisioning_task_id"]))
        job.status = "running"
        await db.commit()
    assert await execute_claimed_hr_provisioning(job.id) == "completed"
    assert await execute_claimed_hr_provisioning(job.id) == "completed"
    created = await creation._direct_call(replace(context, tool_call_id="after-create"))
    assert created["blueprint_id"] == preview["blueprint_id"]
    assert created["draft_status"] == "completed"
    assert created["message"] == "数字员工已创建，可在卡片中和它说话或撤销。首个任务只会执行一次。"
    async with owner_sessionmaker() as db:
        jobs = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.tenant_id == user.tenant_id, RuntimeTask.task_type == "delegation")))
        assert len(jobs) == 1
        assert jobs[0].id == uuid.uuid5(uuid.UUID(preview["blueprint_id"]), "explicit-first-task.v1")


async def test_real_tool_list_reserves_legacy_creation_for_hr(direct_origin, owner_sessionmaker):
    from app.services.agent_tools import get_agent_tools_for_llm

    user, context, _, _ = direct_origin
    async with owner_sessionmaker() as db:
        for name in ("create_employee_from_request", "preview_agent_blueprint", "create_digital_employee"):
            db.add(Tool(name=name, display_name=name, type="builtin", category="hr", enabled=True,
                is_default=True))
        hr = Agent(tenant_id=user.tenant_id, creator_id=user.id, owner_user_id=user.id,
            name="__system_hr__", agent_type="native", agent_class="internal_system", status="idle")
        db.add(hr)
        await db.commit()
    ordinary = {item["function"]["name"] for item in await get_agent_tools_for_llm(context.agent_id)}
    human_resources = {item["function"]["name"] for item in await get_agent_tools_for_llm(hr.id)}
    assert "create_employee_from_request" in ordinary
    assert ordinary.isdisjoint({"preview_agent_blueprint", "create_digital_employee"})
    assert {"preview_agent_blueprint", "create_digital_employee"} <= human_resources
