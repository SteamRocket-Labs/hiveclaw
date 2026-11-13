"""R20b: the final empty-search answer follows real durable lane settlement."""

import json
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

# Load the resolver before per-tenant seams so a lazy import cannot retain
# the first case's tenant resolver in a later case's real parent tool read.
import app.tools.resolver  # noqa: F401

from tests.integration.test_r18a_round2_settlement_lanes import (
    SEARCH_TOOL, _execute_claimed_run, _failure_events, _patch_r18a_seams, _run_status,
)
from tests.integration.test_web_chat_worker_restart_kernel_recovery import (
    ScriptedLLMClient, _fill_governance_allow_evidence, _seed_run,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
ANSWER = "多次搜索都没有找到相关资料。"


def _search_rounds():
    return [{"tool_calls": [{"id": f"search-{index}", "type": "function", "function": {
        "name": "search_company_kb", "arguments": json.dumps({"query": f"海外差旅报销标准 第{index}种说法"})}}],
        "finish_reason": "tool_calls"} for index in range(1, 6)]


def _settled_trace(sink, *, tool_name, args, result):
    from app.tools.service import _runtime_hash

    _fill_governance_allow_evidence(sink, tool_name=tool_name, args=args, result=result)
    # Match the real ToolService frame; the older recovery fixture hashes
    # unquoted bytes, which cannot bind an ordinary result's canonical receipt.
    sink["tool_execution_frame"]["output_hash"] = _runtime_hash(result)


def _bind_database(monkeypatch, factory):
    """Use the disposable Postgres for every existing runtime persistence port."""
    import app.database as database

    original = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is original:
                    monkeypatch.setattr(module, name, factory)


def _bind_native_entry(monkeypatch, owner):
    from tests.integration import test_m1g46_native_skill_receipt_postgres as native

    original_seed = native._seed_run

    async def seed_with_admitted_entry(*args, **kwargs):
        from app.models.runtime_task import RuntimeTask
        from app.models.session_v2 import SessionTurnInput

        seed = await original_seed(*args, **kwargs)
        async with owner() as db:
            entry = (await db.scalars(select(SessionTurnInput).where(
                SessionTurnInput.target_run_id == seed["run_id"]))).one()
            run = await db.get(RuntimeTask, seed["run_id"])
            # The recovery seed queues a steer into an existing run. This test
            # also needs the authenticated entry binding of an ordinary turn.
            run.metadata_json = {**run.metadata_json, "session_v2_input_id": str(entry.id),
                "session_v2_command_id": str(entry.command_id), "source": "websocket_message"}
            await db.commit()
        return seed

    monkeypatch.setattr(native, "_seed_run", seed_with_admitted_entry)


def _disable_auxiliary_model(monkeypatch):
    async def no_memory_model(_tenant_id):
        return None

    monkeypatch.setattr("app.services.session_memory._get_session_memory_model_config", no_memory_model)


@pytest.fixture(autouse=True)
def _no_auxiliary_model(monkeypatch):
    _disable_auxiliary_model(monkeypatch)


async def _assert_final(owner, run_id, answer, rounds):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.session_control_plane import _runtime_task_user_blocker

    async with owner() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == "completed", (run.status, run.result_summary, run.metadata_json)
        assert "session_v2_reconciliation" not in run.metadata_json
        assert not run.metadata_json.get("error_code")
        assert not run.metadata_json.get("failure_code")
        assert _runtime_task_user_blocker(run) is None
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run_id)
            .order_by(SessionModelResult.round_id)))
        assert len(models) == rounds and all(row.state == "round_committed" for row in models)
        replies = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.event_type == "assistant_text.snapshot")))
        assert [reply.content for reply in replies] == [answer]
        assert replies[0].result_id == models[-1].id
        if run.task_type == "web_chat_turn":
            finals = list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.event_type == "assistant_final.completed")))
            assert len(finals) == 1 and finals[0].result_id == models[-1].id
        elif run.task_type == "delegation":
            finals = list(await db.scalars(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.event_type == "assistant_message")))
            assert [final.content for final in finals] == [answer]
            assert finals[0].metadata_json["native_final_model_result_id"] == str(models[-1].id)
        assert models[-1].seal_json["continuation"]["verdict"] == "terminal_candidate"
        assert not list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.event_type == "run.needs_reconciliation")))


async def test_committed_plaza_post_is_in_the_completed_empty_search_answer(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    import app.runtime.invoker as invoker
    from app.models.plaza import PlazaPost
    from app.models.session_v2 import SessionToolInvocation
    from app.services import agent_tools
    from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task

    answer = "帖子已发布到公司广场；关于差旅报销标准，多次搜索都没有找到相关资料。"
    script = [
        {"tool_calls": [{"id": "publish", "type": "function", "function": {
            "name": "plaza_create_post", "arguments": json.dumps({"content": "本周工作交流"})}}],
         "finish_reason": "tool_calls"},
        *_search_rounds(), {"content": answer},
    ]

    async def no_skill(_seed):
        return {}

    async def execute(tool_name, args, *rest, agent_id=None, authority_frame=None,
                      tool_call_id=None, trace_metadata_sink=None,
                      pre_effect_callback=None, **kwargs):
        if tool_name == "plaza_create_post":
            return await agent_tools.execute_tool(
                tool_name, args, *rest, agent_id=agent_id, authority_frame=authority_frame,
                tool_call_id=tool_call_id, trace_metadata_sink=trace_metadata_sink,
                pre_effect_callback=pre_effect_callback, **kwargs)
        assert tool_name == "search_company_kb"
        await pre_effect_callback({"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": dict(args)})
        result = json.dumps({"status": "empty", "results": []})
        _settled_trace(trace_metadata_sink, tool_name=tool_name, args=args, result=result)
        return result

    monkeypatch.setattr(invoker, "execute_tool", execute)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *_a, **_k: [SEARCH_TOOL, {
        "type": "function", "function": {"name": "plaza_create_post", "parameters": {"type": "object",
            "properties": {"content": {"type": "string"}}, "required": ["content"]}}}])
    _bind_native_entry(monkeypatch, owner_sessionmaker)
    seed, client, _bindings, _imported = await _run_native_skill_task(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        skill_setup=no_skill, client_script=script,
        prompt="发一条广场帖子，并查一下海外差旅报销标准。")
    from types import SimpleNamespace
    from app.models.runtime_task import RuntimeTask
    from app.services.runtime_task_claim_service import RuntimeTaskClaimService
    from app.services.runtime_task_fence import run_claimed_runtime_task
    from app.services.web_chat_runtime import execute_web_chat_run
    from tests.integration.test_session_permission_execution_once import _mounted_client

    async def no_wakeup(**_kwargs):
        return None

    monkeypatch.setattr("app.services.runtime_task_worker.notify_runtime_task_worker", no_wakeup)
    async with owner_sessionmaker() as db:
        paused = await db.get(RuntimeTask, seed["run_id"])
        publish = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == seed["run_id"], SessionToolInvocation.tool_name == "plaza_create_post"))
        assert paused.status == "suspended" and publish.effect_state == "prepared_not_started"
        assert publish.permission_state == "waiting"
        assert publish.result_event_id is None and publish.permission_item_id
        permission_id = publish.permission_item_id
        assert list(await db.scalars(select(PlazaPost).where(PlazaPost.author_id == seed["agent_id"]))) == []
    assert len(client.requests) == 1
    path = (f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}"
        f"/permissions/{permission_id}/resolve")
    async with _mounted_client(app_user_sessionmaker, SimpleNamespace(**seed)) as api:
        approval = await api.post(path, json={"action": "allow_once"})
        assert approval.status_code == 200, approval.text
        assert approval.json()["status"] == "resolved" and approval.json()["run_status"] == "resumable"
    async with owner_sessionmaker() as db:
        claimed = await RuntimeTaskClaimService(db=db, worker_id="r20b-publish-resume",
            task_types=("web_chat_turn",)).claim_available()
    assert [task.id for task in claimed] == [seed["run_id"]]
    await run_claimed_runtime_task(execute_web_chat_run(seed["run_id"]), task_id=seed["run_id"],
        claim_version=claimed[0].claim_version, worker_id="r20b-publish-resume", lease_seconds=180)
    # The database and invocation prove the effect, independently of model prose.
    async with owner_sessionmaker() as db:
        posts = list(await db.scalars(select(PlazaPost).where(PlazaPost.author_id == seed["agent_id"])))
        assert len(posts) == 1 and posts[0].content == "本周工作交流"
        publish = await db.scalar(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == seed["run_id"], SessionToolInvocation.tool_name == "plaza_create_post"))
        assert publish.effect_state == "effect_committed" and publish.result_event_id
    await _assert_final(owner_sessionmaker, seed["run_id"], answer, 7)
    assert client.requests[-1]["tools"] is None
    context = json.dumps(client.requests[-1]["messages"], ensure_ascii=False)
    assert "Post published to the company plaza:" in context and "本周工作交流" in context
    assert "committed" in context and "no matching" in context
    assert await _failure_events(owner_sessionmaker, seed["session_id"]) == []


@pytest.mark.parametrize("error,code,sentence", [
    ("Provider rejected request: HTTP 400 bad request", "provider_bad_request",
     "AI 模型供应商拒绝了当前请求，请检查模型名称、参数或消息格式。"),
    ("Provider stream delivery is unknown: ReadTimeout: timed out", "timeout",
     "AI 模型服务方响应超时，请稍后重试。"),
])
async def test_summary_model_failure_is_visible_without_operator_reconciliation(
    owner_sessionmaker, monkeypatch, tmp_path, error, code, sentence,
):
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult
    from app.services.llm_client import LLMError
    from app.services.session_control_plane import _runtime_task_user_blocker

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="公司知识库有没有海外差旅报销标准？")
    client = ScriptedLLMClient([*_search_rounds(), LLMError(error)])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
    await _execute_claimed_run(owner_sessionmaker, seed)
    status, metadata = await _run_status(owner_sessionmaker, seed["run_id"])
    assert status == "failed" and metadata["error_code"] == code
    assert "session_v2_reconciliation" not in metadata
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert _runtime_task_user_blocker(run) is None
        models = list(await db.scalars(select(SessionModelResult).where(SessionModelResult.run_id == run.id)))
        assert len(models) == 6
        assert sum(row.state == "round_committed" for row in models) == 5
        assert sum(row.state == "failed" for row in models) == 1
    failures = await _failure_events(owner_sessionmaker, seed["session_id"])
    assert len(failures) == 1 and failures[0]["failure_code"] == code
    assert sentence in failures[0]["message"]
    assert client.requests[-1]["tools"] is None


async def _run_business_empty_search_summary(
    owner_sessionmaker, monkeypatch, tmp_path, *, summary=None, failure_code="repeated_empty_search",
    failure_sentence=ANSWER, failure_reason="loop_guard",
):
    from app.models.runtime_task import RuntimeTask
    from app.models.task import Task
    from app.services import delegation_session_runtime, task_executor  # noqa: F401
    from app.services.business_task_runtime import stage_business_task_runtime
    from app.services.runtime_task_fence import reset_runtime_task_fence, set_runtime_task_fence
    from app.services.runtime_task_worker import _execute_claimed_business_task

    _disable_auxiliary_model(monkeypatch)
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="查公司知识库里的海外差旅报销标准。")
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    client = ScriptedLLMClient([*_search_rounds(), {"content": ANSWER} if summary is None else summary])
    _bind_database(monkeypatch, owner_sessionmaker)
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
    async with owner_sessionmaker() as db:
        parent = await db.get(RuntimeTask, seed["run_id"])
        parent.status = "completed"
        task = Task(id=uuid4(), tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
            created_by=seed["user_id"], title="查公司知识库", description="查海外差旅报销标准。",
            request_id=str(uuid4()), request_hash="a" * 64,
            plan_exempt_reason="Read-only local settlement fixture")
        db.add(task)
        await db.flush()
        run = await stage_business_task_runtime(db=db, task=task, requester_user_id=seed["user_id"],
            agent_name="Knowledge colleague", request_id=task.request_id, root_session_id=seed["session_id"])
        run.status, run.claimed_by, run.claim_version, run.attempt_count = "running", "r20b-business", 1, 1
        run.claim_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        await db.commit()
        run_id, task_id = run.id, task.id
    token = set_runtime_task_fence(task_id=run_id, worker_id="r20b-business", claim_version=1)
    try:
        await _execute_claimed_business_task(run_id)
    finally:
        reset_runtime_task_fence(token)
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService

    processor = DirectInvocationTerminalBoundaryProcessor(
        session_factory=owner_sessionmaker, data_root=seed["data_root"])
    boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=owner_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r20b-business-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("business_task",))
    assert boundary["delivered"] == 1, boundary
    if summary is None:
        await _assert_final(owner_sessionmaker, run_id, ANSWER, 6)
    else:
        await _assert_discarded_summary(owner_sessionmaker, run_id, failure_code=failure_code)
    async with owner_sessionmaker() as db:
        task = await db.get(Task, task_id)
        run = await db.get(RuntimeTask, run_id)
        if summary is None:
            assert task.status == "done" and task.last_execution_status == "succeeded"
            assert task.last_result == ANSWER and task.last_error is None
            assert run.metadata_json["outcome"]["response_complete_payload"]
            assert run.metadata_json["outcome"]["error_code"] is None
        else:
            outcome = run.metadata_json["outcome"]
            assert task.status == "failed" and task.last_execution_status == "failed"
            assert outcome["error_code"] == failure_code
            assert outcome["terminal_reason"] == failure_reason
            if failure_code == "repeated_empty_search":
                assert outcome["summary"] == ANSWER and outcome["retryable"] is False
            else:
                # R20b round 3: preserve the existing R18a business model-error projection.
                assert outcome["summary"] == "Business task ended without a canonical completion (provider_error)."
            assert "empty_model_content" not in str(outcome)
    assert len(client.requests) == 6 and client.requests[-1]["tools"] is None
    return {"seed": seed, "client": client, "run": run, "task": task}


async def test_business_task_empty_search_summary_settles_with_its_real_model_receipt(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    await _run_business_empty_search_summary(owner_sessionmaker, monkeypatch, tmp_path)


async def _run_delegated_empty_search_summary(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, *, summary=None,
    failure_code="repeated_empty_search", failure_sentence=ANSWER, failure_reason="loop_guard",
):
    import app.runtime.invoker as invoker
    from app.models.runtime_task import RuntimeTask
    from app.services.direct_invocation_terminal_boundary_processor import DirectInvocationTerminalBoundaryProcessor
    from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from tests.integration.test_m1g46_native_skill_receipt_postgres import _run_native_skill_task

    _disable_auxiliary_model(monkeypatch)
    async def no_skill(_seed):
        return {}

    async def empty(tool_name, args, *_a, authority_frame=None, tool_call_id=None, pre_effect_callback=None,
                    trace_metadata_sink=None, **_k):
        assert tool_name == "search_company_kb"
        await pre_effect_callback({"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": dict(args)})
        result = json.dumps({"status": "empty", "results": []})
        _settled_trace(trace_metadata_sink, tool_name=tool_name, args=args, result=result)
        return result

    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *_a, **_k: [SEARCH_TOOL])
    monkeypatch.setattr(invoker, "execute_tool", empty)
    seed, client, _bindings, _imported = await _run_native_skill_task(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        delegated=True, settle_delegation=True, skill_setup=no_skill,
        client_script=[*_search_rounds(), {"content": ANSWER} if summary is None else summary],
        prompt="查公司知识库有没有海外差旅报销标准。",
    )
    result = seed["delegation_result"]
    if summary is None:
        assert not result.failed and result.transcript_committed
        assert result.terminal_reason == "turn_stop" and not result.failure_metadata
        assert result.response_complete_payload
    else:
        assert result.failed and result.terminal_reason == failure_reason
        assert result.failure_metadata["error_code"] == failure_code
        if failure_code == "repeated_empty_search":
            assert result.content == ANSWER
        else:
            assert failure_sentence in result.content
    assert len(client.requests) == 6 and client.requests[-1]["tools"] is None

    processor = DirectInvocationTerminalBoundaryProcessor(
        session_factory=app_user_sessionmaker, data_root=seed["data_root"])
    boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="r20b-terminal", canonical_validator=processor.validate,
        process_callback=processor, task_types=("delegation",))
    assert boundary["delivered"] == 1, boundary
    if summary is None:
        await _assert_final(owner_sessionmaker, seed["run_id"], ANSWER, 6)
    else:
        await _assert_discarded_summary(owner_sessionmaker, seed["run_id"], failure_code=failure_code)
    notices = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
    claimed = await notices.claim_batch(worker_id="r20b-parent", limit=10)
    pages = await notices.prepare_integration_pages(worker_id="r20b-parent",
        claimed=[item for item in claimed if item.tenant_id == seed["tenant_id"]])
    assert len(pages) == 1
    delivered = await notices._deliver_page(pages[0])
    assert delivered.get("ok") is True, delivered
    async with owner_sessionmaker() as db:
        continued = list(await db.scalars(select(RuntimeTask).where(
            RuntimeTask.parent_agent_id == seed["delegation_request"].parent_agent_id,
            RuntimeTask.metadata_json["integration_page_id"].astext == str(pages[0].id))))
        assert len(continued) == 1 and continued[0].status == "pending"
        parent_seed = {**seed, "agent_id": continued[0].parent_agent_id,
            "session_id": pages[0].parent_session_id, "run_id": continued[0].id}
    result_ref = pages[0].items[0].result_ref
    parent_answer = (ANSWER if summary is None or failure_code == "repeated_empty_search"
        else "这次调用模型失败，没有完成。")
    parent_client = ScriptedLLMClient([
        {"tool_calls": [{"id": "read-child-answer", "type": "function", "function": {
            "name": "read_runtime_result", "arguments": json.dumps({"result_ref": result_ref})}}],
         "finish_reason": "tool_calls"},
        {"content": parent_answer},
    ])
    _patch_r18a_seams(monkeypatch, parent_seed, owner_sessionmaker, parent_client)
    from app.services.agent_tools import execute_tool

    monkeypatch.setattr(invoker, "execute_tool", execute_tool)
    monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *_a, **_k: [{
        "type": "function", "function": {"name": "read_runtime_result", "parameters": {"type": "object",
            "properties": {"result_ref": {"type": "string"}}, "required": ["result_ref"]}}}])
    await _execute_claimed_run(owner_sessionmaker, parent_seed, claim_version=0)
    assert len(parent_client.requests) == 2
    assert result_ref in json.dumps(parent_client.requests[0]["messages"], ensure_ascii=False)
    parent_context = json.dumps(parent_client.requests[1]["messages"], ensure_ascii=False)
    if summary is None or failure_code == "repeated_empty_search":
        assert parent_answer in parent_context
    else:
        # R20b round 3: preserve R18a's source-neutral parent model-failure notice.
        assert "这次调用模型失败，没有完成。" in parent_context
    await _assert_final(owner_sessionmaker, parent_seed["run_id"], parent_answer, 2)
    return {"seed": seed, "client": client, "result": result,
        "parent_seed": parent_seed, "parent_client": parent_client}


async def test_delegated_empty_search_answer_reaches_the_parent_continuation(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
):
    await _run_delegated_empty_search_summary(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)


async def _assert_discarded_summary(owner_sessionmaker, run_id, *, failure_code="repeated_empty_search", summary_requests=1):
    from app.models.plaza import PlazaPost
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.session_control_plane import _runtime_task_user_blocker

    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, run_id)
        assert run.status == "failed"
        if run.task_type == "business_task":
            assert run.metadata_json["outcome"]["error_code"] == failure_code
        else:
            assert run.metadata_json["error_code"] == failure_code
        assert _runtime_task_user_blocker(run) is None
        assert "session_v2_reconciliation" not in run.metadata_json
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == run_id).order_by(SessionModelResult.round_id)))
        # R20b round 4/I1': a summary can contain physical output-cap continuation requests.
        assert len(models) == 5 + summary_requests and all(row.state == "round_committed" for row in models[:5])
        for summary in models[5:]:
            assert summary.state == "failed" and summary.reconciliation_owner is None
            if failure_code == "repeated_empty_search":
                assert summary.seal_json == {"delivery_state": "response_received",
                    "error_class": "repeated_empty_search", "retry_safe": True}
            else:
                assert summary.seal_json["error_class"] == failure_code
                assert summary.seal_json["delivery_state"] == ("unknown" if failure_code == "timeout" else "rejected")
        invocations = list(await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == run_id)))
        assert len(invocations) == 5
        assert not any(row.provider_request_id in {model.provider_request_id for model in models[5:]}
            for row in invocations)
        assert list(await db.scalars(select(PlazaPost).where(PlazaPost.tenant_id == run.tenant_id))) == []


@pytest.mark.parametrize("lane", ["web", "delegation", "business"])
@pytest.mark.parametrize("kind", ["empty", "tool", "text_and_tool"])
async def test_discarded_summary_settles_the_actual_provider_request_without_tools(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, lane, kind,
):
    summary = {} if kind == "empty" else {"tool_calls": [{"id": "must-not-publish",
        "type": "function", "function": {"name": "plaza_create_post",
            "arguments": json.dumps({"content": "This must never be published"})}}],
        "finish_reason": "tool_calls"}
    if kind == "text_and_tool":
        summary["content"] = "I will publish it again"
    if lane == "business":
        await _run_business_empty_search_summary(owner_sessionmaker, monkeypatch, tmp_path, summary=summary)
    elif lane == "delegation":
        await _run_delegated_empty_search_summary(
            owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, summary=summary)
    else:
        import app.runtime.invoker as invoker
        from app.models.plaza import PlazaPost
        from app.services.agent_tool_domains.plaza import _plaza_create_post

        seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="公司知识库有没有海外差旅报销标准？")
        client = ScriptedLLMClient([*_search_rounds(), summary, {"content": "Unexpected extra model round"}])
        _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
        search = invoker.execute_tool

        async def execute(tool_name, args, *rest, agent_id=None, tool_call_id=None,
                          pre_effect_callback=None, trace_metadata_sink=None, **kwargs):
            if tool_name == "plaza_create_post":
                await pre_effect_callback({"tool_call_id": tool_call_id, "tool_name": tool_name,
                    "arguments": dict(args)})
                result = await _plaza_create_post(agent_id, args)
                _settled_trace(trace_metadata_sink, tool_name=tool_name, args=args, result=result)
                return result
            return await search(tool_name, args, *rest, agent_id=agent_id, tool_call_id=tool_call_id,
                pre_effect_callback=pre_effect_callback, trace_metadata_sink=trace_metadata_sink, **kwargs)

        monkeypatch.setattr(invoker, "execute_tool", execute)
        monkeypatch.setattr(invoker, "get_agent_tools_for_llm", lambda *_a, **_k: [SEARCH_TOOL, {
            "type": "function", "function": {"name": "plaza_create_post", "parameters": {
                "type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}}])
        await _execute_claimed_run(owner_sessionmaker, seed)
        async with owner_sessionmaker() as db:
            assert list(await db.scalars(select(PlazaPost).where(PlazaPost.author_id == seed["agent_id"]))) == []
        await _assert_discarded_summary(owner_sessionmaker, seed["run_id"])
        failures = await _failure_events(owner_sessionmaker, seed["session_id"])
        assert len(failures) == 1 and failures[0]["failure_code"] == "repeated_empty_search"
        assert failures[0]["message"] == ANSWER and failures[0]["retryable"] is False
        assert len(client.requests) == 6


async def test_summary_output_cap_tool_call_is_discarded_and_all_requests_settle(
    owner_sessionmaker, monkeypatch, tmp_path,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="公司知识库有没有报销标准？")
    client = ScriptedLLMClient([*_search_rounds(), {"content": "Partial summary", "finish_reason": "length"},
        {"tool_calls": [{"id": "must-not-publish-after-cap", "type": "function", "function": {
            "name": "plaza_create_post", "arguments": '{"content":"Duplicate"}'}}], "finish_reason": "tool_calls"}])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
    await _execute_claimed_run(owner_sessionmaker, seed)
    assert len(client.requests) == 7 and all(request["tools"] is None for request in client.requests[5:])
    await _assert_discarded_summary(owner_sessionmaker, seed["run_id"], summary_requests=2)
    failures = await _failure_events(owner_sessionmaker, seed["session_id"])
    assert len(failures) == 1 and failures[0]["failure_code"] == "repeated_empty_search"
    assert failures[0]["message"] == ANSWER and failures[0]["retryable"] is False


@pytest.mark.parametrize("max_rounds", [30, 6])
async def test_member_input_arriving_at_the_guard_is_answered_by_the_normal_continuation(
    owner_sessionmaker, monkeypatch, tmp_path, max_rounds,
):
    import app.runtime.invoker as invoker
    from app.kernel import RuntimeConfig
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.session_v2 import SessionModelResult, SessionTurnInput
    from app.services.session_control_plane import _runtime_task_user_blocker
    from app.models.user import User
    from app.services.session_human_input import queue_admitted_human_input
    from app.services.session_input_admission import run_user_prompt_admission
    from app.services.session_v2_persistence import accept_human_input, resolve_session_mutation_authority

    seed = await _seed_run(owner_sessionmaker, tmp_path, prompt="查公司知识库里的报销标准。")
    next_answer = "新问题已收到，现在为你答复。"
    client = ScriptedLLMClient([*_search_rounds(), {"content": ANSWER}, {"content": next_answer}])
    _patch_r18a_seams(monkeypatch, seed, owner_sessionmaker, client)
    async def runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=seed["tenant_id"], max_tool_rounds=max_rounds)

    monkeypatch.setattr(invoker, "_resolve_runtime_config", runtime_config)
    execute = invoker.execute_tool
    input_id = uuid4()
    new_request = "请处理我刚发来的新问题。"

    async def search(*args, tool_call_id=None, trace_metadata_sink=None, pre_effect_callback=None, **kwargs):
        result = await execute(*args, tool_call_id=tool_call_id, trace_metadata_sink=trace_metadata_sink,
            pre_effect_callback=pre_effect_callback, **kwargs)
        if len(client.requests) == 5:
            async with owner_sessionmaker() as db:
                run = await db.get(RuntimeTask, seed["run_id"])
                user = await db.get(User, seed["user_id"])
                authority = await resolve_session_mutation_authority(db, user=user,
                    agent_id=seed["agent_id"], session_id=seed["session_id"], action="mutate_session_input")
                await accept_human_input(db, authority=authority, intent={
                    "kind": "steer_current_turn", "input_id": str(input_id),
                    "idempotency_key": f"r20b-arrival:{input_id}", "session_id": str(seed["session_id"]),
                    "content_parts": [{"type": "text", "text": new_request}],
                    "expected_turn_id": run.metadata_json["turn_id"], "expected_run_id": str(run.id),
                    "terminal_fallback": "queue_next_turn"})
                admitted = await run_user_prompt_admission(db, authority=authority, input_id=input_id,
                    worker_id="r20b-arrival")
                assert admitted.state == "admitted"
                await queue_admitted_human_input(db, authority=authority, input_id=input_id)
                await db.commit()
        return result

    monkeypatch.setattr(invoker, "execute_tool", search)
    await _execute_claimed_run(owner_sessionmaker, seed)
    status, metadata = await _run_status(owner_sessionmaker, seed["run_id"])
    assert status == "completed"
    assert not metadata.get("error_code") and not metadata.get("failure_code")
    assert "session_v2_reconciliation" not in metadata
    # R20b round 4/I3': one summary round is followed by a normal input-answering round.
    assert len(client.requests) == 7
    assert client.requests[5]["tools"] is None
    assert client.requests[6]["tools"] == client.requests[0]["tools"]
    summary_wire = json.dumps(client.requests[5]["messages"], ensure_ascii=False)
    continuation_wire = json.dumps(client.requests[6]["messages"], ensure_ascii=False)
    assert "This is the final reply for this turn." in summary_wire
    assert "Any tool call you return will not run." in summary_wire
    # R20b round 5/SF2A: the compiled normal request contains facts, without summary-only instructions.
    assert "This is the final reply for this turn." not in continuation_wire
    assert "Any tool call you return will not run." not in continuation_wire
    assert "The searches returned no matching material." in continuation_wire
    assert new_request not in summary_wire
    assert new_request in continuation_wire
    assert any(m["role"] == "assistant" and m["content"] == ANSWER for m in client.requests[6]["messages"])
    async with owner_sessionmaker() as db:
        run = await db.get(RuntimeTask, seed["run_id"])
        assert _runtime_task_user_blocker(run) is None
        pending = await db.get(SessionTurnInput, input_id)
        assert pending.status == "applied"
        assert pending.bound_round_id == f"{seed['run_id']}:round:7"
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == run.id).order_by(SessionModelResult.round_id)))
        assert len(models) == 7 and all(m.state == "round_committed" for m in models)
        summary = models[5]
        assert summary.seal_json["continuation"]["verdict"] == "continue"
        prepared_messages = summary.model_request_snapshot_json["wire_request"]["messages"]
        def normalize(messages):
            return [{k: v for k, v in m.items() if v is not None} for m in messages]
        assert normalize(prepared_messages) == normalize(client.requests[5]["messages"])
        reminders = [m for m in prepared_messages if "Any tool call you return will not run." in str(m.get("content"))]
        assert len(reminders) == 1
        assert "有一项上下文暂时无法核实" not in reminders[0]["content"]
        assert any(o["kind"] == "pending_input" for o in summary.seal_json["continuation"]["pending_obligations"])
        assert models[6].seal_json["continuation"]["verdict"] == "terminal_candidate"
        replies = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run.id, ChatTranscriptEvent.event_type == "assistant_text.snapshot")
            .order_by(ChatTranscriptEvent.sequence)))
        # R20b round 4/P2: persist both the search answer and the new member's answer.
        assert [reply.content for reply in replies] == [ANSWER, next_answer]
        assert [reply.result_id for reply in replies] == [summary.id, models[6].id]
        finals = list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run.id, ChatTranscriptEvent.event_type == "assistant_final.completed")))
        assert len(finals) == 1 and finals[0].result_id == models[6].id
        assert not list(await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.run_id == run.id, ChatTranscriptEvent.event_type == "run.needs_reconciliation")))
    assert await _failure_events(owner_sessionmaker, seed["session_id"]) == []


@pytest.mark.parametrize("lane", ["business", "delegation"])
@pytest.mark.parametrize("kind,code,sentence", [
    ("error", "provider_bad_request", "AI 模型供应商拒绝了当前请求，请检查模型名称、参数或消息格式。"),
    ("timeout", "timeout", "AI 模型服务方响应超时，请稍后重试。"),
])
async def test_summary_model_failure_settles_non_web_lanes_and_the_parent(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, lane, kind, code, sentence,
):
    from app.services.llm_client import LLMError

    error = (LLMError("Provider rejected request: HTTP 400 bad request", delivery_state="rejected")
        if kind == "error" else LLMError("Provider stream delivery is unknown: ReadTimeout: timed out"))
    kwargs = {"summary": error, "failure_code": code, "failure_sentence": sentence,
        "failure_reason": "provider_error"}
    if lane == "business":
        await _run_business_empty_search_summary(owner_sessionmaker, monkeypatch, tmp_path, **kwargs)
    else:
        await _run_delegated_empty_search_summary(
            owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, **kwargs)
